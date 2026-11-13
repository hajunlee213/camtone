from dataclasses import dataclass
from typing import Optional
from .sensor import SensorReading


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _lerp_map(val: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    if in_max <= in_min:
        return out_min
    t = _clamp((val - in_min) / (in_max - in_min), 0.0, 1.0)
    return out_min + t * (out_max - out_min)


@dataclass
class ControlTarget:
    brightness_pct: int
    cct: int
    apply_brightness: bool
    apply_cct: bool
    smoothed_ev_log: float
    smoothed_cct: float


class AdaptiveController:
    def __init__(
        self,
        # TODO(calibration): estimated values after ADD-01 gamma correction; tune with --dry-run
        ev_log_min: float = -10.0,
        ev_log_max: float = -3.5,
        brightness_min_pct: int = 15,
        brightness_max_pct: int = 95,
        ambient_cct_min: float = 2800.0,
        ambient_cct_max: float = 6500.0,
        display_cct_min: int = 3400,
        display_cct_max: int = 6200,
        cct_offset: int = 0,
        ema_alpha: float = 0.35,
        brightness_deadband_pct: int = 2,
        cct_deadband_k: int = 80,
    ):
        self.ev_log_min = ev_log_min
        self.ev_log_max = ev_log_max
        self.brightness_min_pct = brightness_min_pct
        self.brightness_max_pct = brightness_max_pct
        self.ambient_cct_min = ambient_cct_min
        self.ambient_cct_max = ambient_cct_max
        self.display_cct_min = display_cct_min
        self.display_cct_max = display_cct_max
        self.cct_offset = cct_offset
        self.ema_alpha = ema_alpha
        self.brightness_deadband_pct = brightness_deadband_pct
        self.cct_deadband_k = cct_deadband_k

        self._ema_ev_log: Optional[float] = None
        self._ema_cct: Optional[float] = None
        self._last_applied_brightness: Optional[int] = None
        self._last_applied_cct: Optional[int] = None

    def update(self, reading: SensorReading) -> ControlTarget:
        if self._ema_ev_log is None:
            self._ema_ev_log = reading.ev_log
            self._ema_cct = reading.cct
        else:
            a = self.ema_alpha
            self._ema_ev_log = a * reading.ev_log + (1.0 - a) * self._ema_ev_log
            self._ema_cct = a * reading.cct + (1.0 - a) * self._ema_cct

        raw_brightness = _lerp_map(
            self._ema_ev_log,
            self.ev_log_min,
            self.ev_log_max,
            float(self.brightness_min_pct),
            float(self.brightness_max_pct),
        )
        target_brightness = int(round(_clamp(raw_brightness, self.brightness_min_pct, self.brightness_max_pct)))

        raw_cct = _lerp_map(
            self._ema_cct,
            self.ambient_cct_min,
            self.ambient_cct_max,
            float(self.display_cct_min),
            float(self.display_cct_max),
        ) + self.cct_offset
        target_cct = int(round(_clamp(raw_cct, 2500.0, 6500.0)))

        apply_brightness = (
            self._last_applied_brightness is None
            or abs(target_brightness - self._last_applied_brightness) >= self.brightness_deadband_pct
        )
        apply_cct = (
            self._last_applied_cct is None
            or abs(target_cct - self._last_applied_cct) >= self.cct_deadband_k
        )

        if apply_brightness:
            self._last_applied_brightness = target_brightness
        if apply_cct:
            self._last_applied_cct = target_cct

        return ControlTarget(
            brightness_pct=target_brightness,
            cct=target_cct,
            apply_brightness=apply_brightness,
            apply_cct=apply_cct,
            smoothed_ev_log=self._ema_ev_log,
            smoothed_cct=self._ema_cct,
        )
