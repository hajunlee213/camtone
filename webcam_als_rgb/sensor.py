from dataclasses import dataclass
import math
import subprocess
from typing import Optional
import numpy as np

# sRGB (D65) -> CIE XYZ conversion matrix
_SRGB_TO_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)


@dataclass
class SensorReading:
    luma: float
    luma_effective: float
    exposure: int
    ev_index: float
    ev_log: float
    cct: float
    sat_pct: float
    rgb_mean: tuple[float, float, float]


def is_device_busy(device: str) -> bool:
    """Return True if another process currently holds the V4L2 device."""
    res = subprocess.run(["fuser", device], capture_output=True)
    return res.returncode == 0


def _v4l2(device: str, *args: str) -> str:
    res = subprocess.run(
        ["v4l2-ctl", "-d", device, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


_SAVED_CTRL_NAMES = (
    "auto_exposure",
    "white_balance_automatic",
    "white_balance_temperature",
    "exposure_time_absolute",
    "exposure_dynamic_framerate",
)


def _get_ctrls(device: str, names: tuple[str, ...] = _SAVED_CTRL_NAMES) -> dict[str, int]:
    """Read current integer values for V4L2 controls, ignoring unsupported ones."""
    res = subprocess.run(
        ["v4l2-ctl", "-d", device, "-C", ",".join(names)],
        capture_output=True,
        text=True,
        check=False,
    )
    ctrls: dict[str, int] = {}
    for line in res.stdout.splitlines():
        if ":" not in line:
            continue
        key, val_part = line.split(":", 1)
        key = key.strip()
        tokens = val_part.strip().split()
        if key in names and tokens:
            try:
                ctrls[key] = int(tokens[0])
            except ValueError:
                pass
    return ctrls


def _restore_ctrls(device: str, saved: dict[str, int]) -> None:
    """Restore previously saved V4L2 controls (falling back to AE=3, AWB=1)."""
    if not saved:
        _v4l2(device, "--set-ctrl=auto_exposure=3,white_balance_automatic=1")
        return

    restore_parts: list[str] = []
    if "exposure_dynamic_framerate" in saved:
        restore_parts.append(f"exposure_dynamic_framerate={saved['exposure_dynamic_framerate']}")
    if "auto_exposure" in saved:
        restore_parts.append(f"auto_exposure={saved['auto_exposure']}")
    if saved.get("auto_exposure") == 1 and "exposure_time_absolute" in saved:
        restore_parts.append(f"exposure_time_absolute={saved['exposure_time_absolute']}")
    if "white_balance_automatic" in saved:
        restore_parts.append(f"white_balance_automatic={saved['white_balance_automatic']}")
    if saved.get("white_balance_automatic") == 0 and "white_balance_temperature" in saved:
        restore_parts.append(f"white_balance_temperature={saved['white_balance_temperature']}")

    if restore_parts:
        _v4l2(device, f"--set-ctrl={','.join(restore_parts)}")


def _validate_yuyv_format(device: str, width: int, height: int) -> None:
    """Verify via --get-fmt-video that the driver negotiated the expected YUYV format."""
    out = _v4l2(device, "--get-fmt-video")
    actual_w: Optional[int] = None
    actual_h: Optional[int] = None
    pixel_fmt: Optional[str] = None
    bytes_per_line: Optional[int] = None

    for line in out.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if key == "Width/Height" and "/" in val:
            w_str, h_str = val.split("/", 1)
            actual_w, actual_h = int(w_str), int(h_str)
        elif key == "Pixel Format":
            pixel_fmt = val.split()[0].strip("'")
        elif key == "Bytes per Line":
            bytes_per_line = int(val)

    expected_bpl = width * 2
    if (
        actual_w != width
        or actual_h != height
        or pixel_fmt != "YUYV"
        or bytes_per_line != expected_bpl
    ):
        raise RuntimeError(
            f"Unexpected V4L2 format on {device}: "
            f"got {actual_w}x{actual_h} {pixel_fmt} bpl={bytes_per_line}, "
            f"expected {width}x{height} YUYV bpl={expected_bpl}"
        )


def _capture_yuyv(
    device: str,
    width: int,
    height: int,
    count: int,
    extra_args: tuple[str, ...] = (),
) -> np.ndarray:
    frame_bytes = width * height * 2
    res = subprocess.run(
        [
            "v4l2-ctl",
            "-d",
            device,
            *extra_args,
            "--stream-mmap",
            f"--stream-count={count}",
            "--stream-to=-",
        ],
        capture_output=True,
        check=True,
    )
    raw = res.stdout
    if len(raw) % frame_bytes != 0:
        raise RuntimeError(
            f"YUYV buffer size mismatch on {device}: {len(raw)} bytes is not a multiple of {frame_bytes}"
        )
    actual = len(raw) // frame_bytes
    if actual == 0:
        raise RuntimeError(f"Captured 0 frames from {device}")
    arr = np.frombuffer(raw, dtype=np.uint8)
    return arr.reshape((actual, height, width, 2))


def _analyze_frame(
    frame: np.ndarray,
    black_level: float = 16.0,
    roi_mode: str = "full",
) -> tuple[float, float, float, float, tuple[float, float, float]]:
    """Compute luma, effective luma, saturation %, CCT (McCamy), and mean RGB."""
    h, w, _ = frame.shape
    y = frame[:, :, 0].astype(np.float32)
    u = np.repeat(frame[:, 0::2, 1].astype(np.float32), 2, axis=1) - 128.0
    v = np.repeat(frame[:, 1::2, 1].astype(np.float32), 2, axis=1) - 128.0

    # Optional ROI masking to reduce direct face/screen reflection bias
    if roi_mode == "border":
        mask = np.zeros((h, w), dtype=bool)
        mask[: h // 3, :] = True
        mask[:, : w // 5] = True
        mask[:, -w // 5 :] = True
    else:
        mask = np.ones((h, w), dtype=bool)

    y_roi = y[mask]
    u_roi = u[mask]
    v_roi = v[mask]

    # Compensate YUV limited-range black floor before RGB conversion for cleaner CCT
    y_comp = np.clip(y_roi - black_level, 0.0, 255.0 - black_level) * (255.0 / (255.0 - black_level))
    r = np.clip(y_comp + 1.402 * v_roi, 0.0, 255.0)
    g = np.clip(y_comp - 0.344136 * u_roi - 0.714136 * v_roi, 0.0, 255.0)
    b = np.clip(y_comp + 1.772 * u_roi, 0.0, 255.0)

    sat_pct = float(np.mean((y_roi >= 250.0) | (r >= 253.0) | (g >= 253.0) | (b >= 253.0)) * 100.0)

    rgb_norm = np.stack([r, g, b], axis=-1) / 255.0
    lin = np.where(rgb_norm <= 0.04045, rgb_norm / 12.92, ((rgb_norm + 0.055) / 1.055) ** 2.4)
    mean_lin = lin.mean(axis=0)

    X, Y_xyz, Z = _SRGB_TO_XYZ @ mean_lin
    denom = X + Y_xyz + Z
    if denom > 1e-6:
        x = X / denom
        y_chrom = Y_xyz / denom
        if abs(0.1858 - y_chrom) > 1e-5:
            n = (x - 0.3320) / (0.1858 - y_chrom)
            cct = float(449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33)
            cct = max(2000.0, min(10000.0, cct))
        else:
            cct = 6500.0
    else:
        cct = 6500.0

    luma_mean = float(y_roi.mean())
    luma_eff = max(luma_mean - black_level, 0.5)
    rgb_mean = (float(r.mean()), float(g.mean()), float(b.mean()))
    return luma_mean, luma_eff, sat_pct, cct, rgb_mean


class WebcamSensor:
    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 320,
        height: int = 240,
        wb_ref_temp: int = 4600,
        min_exposure: int = 50,
        max_exposure: int = 10000,
        initial_exposure: int = 350,
        black_level: float = 16.0,
        gamma: float = 2.2,
        target_luma_min: float = 65.0,
        target_luma_max: float = 155.0,
        roi_mode: str = "full",
    ):
        self.device = device
        self.width = width
        self.height = height
        self.wb_ref_temp = wb_ref_temp
        self.min_exposure = min_exposure
        self.max_exposure = max_exposure
        self.last_exposure = max(min_exposure, min(max_exposure, initial_exposure))
        self.black_level = black_level
        # TODO(calibration): verify effective camera gamma (2.2 sRGB assumption vs V4L2 gamma control)
        self.gamma = gamma
        self.target_luma_min = target_luma_min
        self.target_luma_max = target_luma_max
        self.roi_mode = roi_mode

    def sample(self) -> Optional[SensorReading]:
        """Capture frames using manual AE/AWB with auto-ranging exposure.

        Returns None if the camera is currently busy (e.g., video call).
        Always restores auto_exposure=3 and white_balance_automatic=1 afterward.
        """
        if is_device_busy(self.device):
            return None

        saved_ctrls = _get_ctrls(self.device)
        exp = self.last_exposure

        try:
            ctrl_str = f"auto_exposure=1,white_balance_automatic=0,white_balance_temperature={self.wb_ref_temp}"
            if "exposure_dynamic_framerate" in saved_ctrls:
                ctrl_str += ",exposure_dynamic_framerate=1"
            fmt_arg = f"--set-fmt-video=width={self.width},height={self.height},pixelformat=YUYV"

            best_metrics = None
            for step_idx in range(3):
                if step_idx == 0:
                    extra_args: tuple[str, ...] = (
                        fmt_arg,
                        f"--set-ctrl={ctrl_str},exposure_time_absolute={exp}",
                    )
                else:
                    extra_args = (f"--set-ctrl=exposure_time_absolute={exp}",)

                frames = _capture_yuyv(self.device, self.width, self.height, count=6, extra_args=extra_args)
                if step_idx == 0:
                    _validate_yuyv_format(self.device, self.width, self.height)
                usable = frames[2:] if len(frames) > 2 else frames

                analyzed = [_analyze_frame(f, self.black_level, self.roi_mode) for f in usable]
                luma = float(np.mean([a[0] for a in analyzed]))
                luma_eff = float(np.mean([a[1] for a in analyzed]))
                sat_pct = float(np.mean([a[2] for a in analyzed]))
                cct = float(np.mean([a[3] for a in analyzed]))
                rgb_mean = (
                    float(np.mean([a[4][0] for a in analyzed])),
                    float(np.mean([a[4][1] for a in analyzed])),
                    float(np.mean([a[4][2] for a in analyzed])),
                )
                best_metrics = (luma, luma_eff, sat_pct, cct, rgb_mean, exp)

                in_range = self.target_luma_min <= luma <= self.target_luma_max and sat_pct <= 2.0
                at_low_limit = exp <= self.min_exposure and (luma > self.target_luma_max or sat_pct > 2.0)
                at_high_limit = exp >= self.max_exposure and luma < self.target_luma_min
                if in_range or at_low_limit or at_high_limit:
                    break

                # Proportional exposure step toward target luma (~105) in linear light space
                y_span = max(235.0 - self.black_level, 1.0)
                cur_norm = max(1e-4, min(1.0, luma_eff / y_span))
                cur_lin = cur_norm ** self.gamma
                target_norm = max(1e-4, min(1.0, (105.0 - self.black_level) / y_span))
                target_lin = target_norm ** self.gamma
                ratio = target_lin / max(cur_lin, 1e-6)
                ratio = max(0.25, min(4.0, ratio))
                next_exp = int(round(exp * ratio))
                next_exp = max(self.min_exposure, min(self.max_exposure, next_exp))
                if next_exp == exp:
                    break
                exp = next_exp

            assert best_metrics is not None
            luma, luma_eff, sat_pct, cct, rgb_mean, final_exp = best_metrics
            self.last_exposure = final_exp
            y_span = max(235.0 - self.black_level, 1.0)
            luma_lin = max(1e-4, min(1.0, luma_eff / y_span)) ** self.gamma
            ev_index = luma_lin / (final_exp / 100.0)
            ev_log = math.log2(max(ev_index, 1e-6))
            return SensorReading(
                luma=luma,
                luma_effective=luma_eff,
                exposure=final_exp,
                ev_index=ev_index,
                ev_log=ev_log,
                cct=cct,
                sat_pct=sat_pct,
                rgb_mean=rgb_mean,
            )
        finally:
            try:
                _restore_ctrls(self.device, saved_ctrls)
            except subprocess.CalledProcessError:
                pass
