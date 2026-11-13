from dataclasses import dataclass
import math
import re
import subprocess
from typing import Callable, Optional, Sequence, TypeVar
import numpy as np

_T = TypeVar("_T")

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
    ev_log_gamma1: float = 0.0


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


def get_exposure_range(device: str) -> Optional[tuple[int, int]]:
    """Return (min, max) for exposure_time_absolute on the device, or None if unavailable."""
    try:
        out = _v4l2(device, "-l")
    except (subprocess.CalledProcessError, OSError):
        return None
    for line in out.splitlines():
        if "exposure_time_absolute" not in line:
            continue
        m_min = re.search(r"\bmin=(-?\d+)\b", line)
        m_max = re.search(r"\bmax=(-?\d+)\b", line)
        if m_min and m_max:
            return int(m_min.group(1)), int(m_max.group(1))
    return None


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


def _analyze_frames(
    frames: np.ndarray,
    black_level: float = 16.0,
    roi_mode: str = "full",
    yuv_range: str = "limited",
    gamma: float = 1.0,
) -> tuple[float, float, float, float, float, float, tuple[float, float, float]]:
    """Batch-compute luma, effective luma, linear luma means (gamma and gamma=1), saturation %, CCT, and mean RGB."""
    _, h, w, _ = frames.shape
    y = frames[:, :, :, 0].astype(np.float32)
    u = np.repeat(frames[:, :, 0::2, 1].astype(np.float32), 2, axis=2) - 128.0
    v = np.repeat(frames[:, :, 1::2, 1].astype(np.float32), 2, axis=2) - 128.0

    # Optional ROI masking to reduce direct face/screen reflection bias
    if roi_mode == "border":
        mask = np.zeros((h, w), dtype=bool)
        mask[: h // 3, :] = True
        mask[:, : w // 5] = True
        mask[:, -w // 5 :] = True
    else:
        mask = np.ones((h, w), dtype=bool)

    y_roi = y[:, mask]
    u_roi = u[:, mask]
    v_roi = v[:, mask]

    # TODO(calibration): verify camera Y/UV range (strict limited 16..235 / 16..240 vs full 0..255)
    if yuv_range == "full":
        eff_black = 0.0
        y_span = 255.0
        y_comp = y_roi
        u_scaled = u_roi
        v_scaled = v_roi
    else:
        eff_black = black_level
        y_span = max(235.0 - eff_black, 1.0)
        y_comp = np.clip(y_roi - eff_black, 0.0, y_span) * (255.0 / y_span)
        # TODO(calibration): verify chroma (U/V) range scaling (255/224 for 16..240) on target webcam
        u_scaled = u_roi
        v_scaled = v_roi

    r = np.clip(y_comp + 1.402 * v_scaled, 0.0, 255.0)
    g = np.clip(y_comp - 0.344136 * u_scaled - 0.714136 * v_scaled, 0.0, 255.0)
    b = np.clip(y_comp + 1.772 * u_scaled, 0.0, 255.0)

    # Judge saturation on luminance (y_comp) only so saturated colored objects do not lower exposure
    sat_pct = float(np.mean(y_comp >= 250.0) * 100.0)

    rgb_norm = np.stack([r, g, b], axis=-1) / 255.0
    lin = np.where(rgb_norm <= 0.04045, rgb_norm / 12.92, ((rgb_norm + 0.055) / 1.055) ** 2.4)
    mean_lin = lin.mean(axis=(0, 1))

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

    y_norm = np.clip((y_roi - eff_black) / y_span, 0.0, 1.0)
    luma_lin_gamma1 = float(y_norm.mean())
    luma_lin_mean = luma_lin_gamma1 if gamma == 1.0 else float((y_norm ** gamma).mean())
    luma_mean = float(y_roi.mean())
    luma_eff = max(luma_mean - eff_black, 0.5)
    rgb_mean = (float(r.mean()), float(g.mean()), float(b.mean()))
    return luma_mean, luma_eff, luma_lin_mean, luma_lin_gamma1, sat_pct, cct, rgb_mean


class WebcamSensor:
    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 320,
        height: int = 240,
        wb_ref_temp: int = 4600,
        min_exposure: int = 50,
        # 2026-11-13 재실측: 노출 10000까지 luma가 계속 증가하나 캡처 시간(LED 점등)과의 균형을 위해 8000으로 설정 (v0.1.3 계획서 2절)
        max_exposure: int = 8000,
        initial_exposure: int = 350,
        black_level: float = 16.0,
        # 2026-11-13 실측: luma<=80에서 노출에 정비례, v0.1.2 계획서 2절
        gamma: float = 1.0,
        yuv_range: str = "limited",
        target_luma_min: float = 50.0,
        target_luma_max: float = 80.0,
        target_luma_setpoint: float = 65.0,
        roi_mode: str = "full",
        exposure_blacklist: Sequence[int] = (1250, 2500),
    ):
        if yuv_range not in ("limited", "full"):
            raise ValueError(f"yuv_range must be 'limited' or 'full', got {yuv_range!r}")
        if not (target_luma_min <= target_luma_setpoint <= target_luma_max):
            raise ValueError(
                f"target_luma_setpoint ({target_luma_setpoint}) must satisfy "
                f"target_luma_min ({target_luma_min}) <= setpoint <= target_luma_max ({target_luma_max})"
            )
        self.device = device
        self.width = width
        self.height = height
        self.wb_ref_temp = wb_ref_temp
        self.min_exposure = min_exposure
        self.max_exposure = max_exposure
        self.exposure_blacklist: set[int] = {int(x) for x in exposure_blacklist}
        self.last_exposure = self._avoid_blacklisted_exposure(initial_exposure)
        self.black_level = black_level
        self.gamma = gamma
        self.yuv_range = yuv_range
        self.target_luma_min = target_luma_min
        self.target_luma_max = target_luma_max
        self.target_luma_setpoint = target_luma_setpoint
        self.roi_mode = roi_mode

    def _avoid_blacklisted_exposure(self, exp: int, extra_blacklist: Optional[set[int]] = None) -> int:
        exp = max(self.min_exposure, min(self.max_exposure, int(exp)))
        blocked = self.exposure_blacklist if extra_blacklist is None else (self.exposure_blacklist | extra_blacklist)
        if exp not in blocked:
            return exp
        # 우선 +1씩 올려 회피하고, max_exposure에 막히면 -1씩 내려 회피
        up = exp
        while up <= self.max_exposure and up in blocked:
            up += 1
        if up <= self.max_exposure:
            return up
        down = exp
        while down >= self.min_exposure and down in blocked:
            down -= 1
        return max(self.min_exposure, down)

    def _candidate_score(self, luma: float, sat_pct: float) -> tuple[int, float, int, float]:
        """Lower score is closer to the linear target range [target_luma_min, target_luma_max]."""
        dist_to_range = max(0.0, self.target_luma_min - luma, luma - self.target_luma_max)
        return (
            1 if sat_pct > 2.0 else 0,
            dist_to_range,
            1 if luma > self.target_luma_max else 0,
            abs(luma - self.target_luma_setpoint),
        )

    def _run_with_saved_ctrls(self, fn: Callable[[dict[str, int]], _T]) -> Optional[_T]:
        """Run a capture function with device-busy check and V4L2 control save/restore."""
        if is_device_busy(self.device):
            return None
        saved_ctrls = _get_ctrls(self.device)
        try:
            return fn(saved_ctrls)
        finally:
            try:
                _restore_ctrls(self.device, saved_ctrls)
            except subprocess.CalledProcessError:
                pass

    def _capture_at_exposure(
        self,
        exp: int,
        saved_ctrls: dict[str, int],
        is_first_step: bool,
    ) -> tuple[float, float, float, float, float, float, tuple[float, float, float]]:
        if is_first_step:
            ctrl_str = f"auto_exposure=1,white_balance_automatic=0,white_balance_temperature={self.wb_ref_temp}"
            if "exposure_dynamic_framerate" in saved_ctrls:
                ctrl_str += ",exposure_dynamic_framerate=1"
            fmt_arg = f"--set-fmt-video=width={self.width},height={self.height},pixelformat=YUYV"
            extra_args: tuple[str, ...] = (
                fmt_arg,
                f"--set-ctrl={ctrl_str},exposure_time_absolute={exp}",
            )
        else:
            extra_args = (f"--set-ctrl=exposure_time_absolute={exp}",)

        frame_count = 3 if exp >= 1000 else 5
        frames = _capture_yuyv(
            self.device,
            self.width,
            self.height,
            count=frame_count,
            extra_args=extra_args,
        )
        if is_first_step:
            _validate_yuyv_format(self.device, self.width, self.height)
        warmup = 1 if len(frames) <= 3 else 2
        usable = frames[warmup:] if len(frames) > warmup else frames
        return _analyze_frames(usable, self.black_level, self.roi_mode, self.yuv_range, self.gamma)

    def _build_reading(
        self,
        metrics: tuple[float, float, float, float, float, float, tuple[float, float, float]],
        final_exp: int,
    ) -> SensorReading:
        luma, luma_eff, luma_lin_mean, luma_lin_gamma1, sat_pct, cct, rgb_mean = metrics
        luma_lin = max(1e-4, min(1.0, luma_lin_mean))
        ev_index = luma_lin / (final_exp / 100.0)
        ev_log = math.log2(max(ev_index, 1e-6))
        ev_index_g1 = max(1e-4, min(1.0, luma_lin_gamma1)) / (final_exp / 100.0)
        ev_log_gamma1 = math.log2(max(ev_index_g1, 1e-6))
        return SensorReading(
            luma=luma,
            luma_effective=luma_eff,
            exposure=final_exp,
            ev_index=ev_index,
            ev_log=ev_log,
            cct=cct,
            sat_pct=sat_pct,
            rgb_mean=rgb_mean,
            ev_log_gamma1=ev_log_gamma1,
        )

    def sample_fixed(self, exp: int) -> Optional[SensorReading]:
        """Capture frames once at a fixed exposure without auto-ranging."""
        def _measure(saved_ctrls: dict[str, int]) -> SensorReading:
            metrics = self._capture_at_exposure(exp, saved_ctrls, is_first_step=True)
            return self._build_reading(metrics, exp)

        return self._run_with_saved_ctrls(_measure)

    def sample(self) -> Optional[SensorReading]:
        """Capture frames using manual AE/AWB with auto-ranging exposure.

        Returns None if the camera is currently busy (e.g., video call).
        Restores the previously saved V4L2 controls afterward.
        """
        def _measure(saved_ctrls: dict[str, int]) -> SensorReading:
            runtime_blacklist: set[int] = set()
            prev_normal: Optional[tuple[int, float, float]] = None
            exp = self._avoid_blacklisted_exposure(self.last_exposure, runtime_blacklist)
            best_metrics = None
            best_score = None
            best_exp = exp
            for step_idx in range(3):
                metrics = self._capture_at_exposure(exp, saved_ctrls, is_first_step=(step_idx == 0))
                luma, _luma_eff, luma_lin_mean, _luma_lin_g1, sat_pct, _cct, _rgb = metrics
                cur_lin = max(1e-4, min(1.0, luma_lin_mean))

                if prev_normal is not None:
                    prev_exp, prev_lin, prev_sat = prev_normal
                    if prev_sat <= 2.0 and prev_exp > 0:
                        expected_lin = prev_lin * (exp / prev_exp)
                        if cur_lin < expected_lin * 0.60:
                            runtime_blacklist.add(exp)
                            retry_exp = self._avoid_blacklisted_exposure(
                                exp + max(1, int(round(exp * 0.01))),
                                runtime_blacklist,
                            )
                            if retry_exp != exp:
                                exp = retry_exp
                                metrics = self._capture_at_exposure(exp, saved_ctrls, is_first_step=False)
                                luma, _luma_eff, luma_lin_mean, _luma_lin_g1, sat_pct, _cct, _rgb = metrics
                                cur_lin = max(1e-4, min(1.0, luma_lin_mean))

                prev_normal = (exp, cur_lin, sat_pct)
                score = self._candidate_score(luma, sat_pct)
                if best_score is None or score < best_score:
                    best_score = score
                    best_metrics = metrics
                    best_exp = exp

                in_range = self.target_luma_min <= luma <= self.target_luma_max and sat_pct <= 2.0
                at_low_limit = exp <= self.min_exposure and (luma > self.target_luma_max or sat_pct > 2.0)
                at_high_limit = exp >= self.max_exposure and luma < self.target_luma_min
                if in_range or at_low_limit or at_high_limit:
                    best_metrics = metrics
                    best_exp = exp
                    break

                # Proportional exposure step toward target_luma_setpoint in linear light space
                eff_black = 0.0 if self.yuv_range == "full" else self.black_level
                y_max = 255.0 if self.yuv_range == "full" else 235.0
                y_span = max(y_max - eff_black, 1.0)
                target_norm = max(1e-4, min(1.0, (self.target_luma_setpoint - eff_black) / y_span))
                target_lin = target_norm ** self.gamma
                ratio = target_lin / max(cur_lin, 1e-6)
                if luma > self.target_luma_max and ratio < 1.0:
                    # Knee region (luma > target_luma_max) compresses luma; over-correct to converge within 2-3 steps
                    ratio = ratio ** 1.5
                    ratio = max(0.15, min(4.0, ratio))
                else:
                    ratio = max(0.25, min(4.0, ratio))
                next_exp = int(round(exp * ratio))
                next_exp = self._avoid_blacklisted_exposure(next_exp, runtime_blacklist)
                if next_exp == exp:
                    break
                exp = next_exp

            assert best_metrics is not None
            self.last_exposure = best_exp
            return self._build_reading(best_metrics, best_exp)

        return self._run_with_saved_ctrls(_measure)
