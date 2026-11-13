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


def _capture_yuyv(device: str, width: int, height: int, count: int) -> np.ndarray:
    frame_bytes = width * height * 2
    res = subprocess.run(
        [
            "v4l2-ctl",
            "-d",
            device,
            "--stream-mmap",
            f"--stream-count={count}",
            "--stream-to=-",
        ],
        capture_output=True,
        check=True,
    )
    raw = res.stdout
    actual = len(raw) // frame_bytes
    if actual == 0:
        raise RuntimeError(f"Captured 0 frames from {device}")
    arr = np.frombuffer(raw[: actual * frame_bytes], dtype=np.uint8)
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

        _v4l2(self.device, f"--set-fmt-video=width={self.width},height={self.height},pixelformat=YUYV")
        exp = self.last_exposure

        try:
            _v4l2(
                self.device,
                f"--set-ctrl=auto_exposure=1,white_balance_automatic=0,white_balance_temperature={self.wb_ref_temp}",
            )

            best_metrics = None
            for _ in range(3):
                _v4l2(self.device, f"--set-ctrl=exposure_time_absolute={exp}")
                frames = _capture_yuyv(self.device, self.width, self.height, count=6)
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

                # Proportional exposure step toward target luma (~105)
                target_eff = 105.0 - self.black_level
                ratio = target_eff / max(luma_eff, 1.0)
                ratio = max(0.25, min(4.0, ratio))
                next_exp = int(round(exp * ratio))
                next_exp = max(self.min_exposure, min(self.max_exposure, next_exp))
                if next_exp == exp:
                    break
                exp = next_exp

            assert best_metrics is not None
            luma, luma_eff, sat_pct, cct, rgb_mean, final_exp = best_metrics
            self.last_exposure = final_exp
            ev_index = luma_eff / (final_exp / 100.0)
            ev_log = math.log2(max(ev_index, 1e-3))
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
                _v4l2(self.device, "--set-ctrl=auto_exposure=3,white_balance_automatic=1")
            except subprocess.CalledProcessError:
                pass
