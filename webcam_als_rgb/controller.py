from dataclasses import dataclass
from typing import Optional, Sequence
from .sensor import SensorReading


# Output CCT limits. 1700K is the warm end of GNOME's own Night Light slider.
_CCT_FLOOR_K = 1700.0
_CCT_CEIL_K = 10000.0


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _lerp_map(val: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    if in_max <= in_min:
        return out_min
    t = _clamp((val - in_min) / (in_max - in_min), 0.0, 1.0)
    return out_min + t * (out_max - out_min)


def _calibrate_cct(measured_cct: float, points: Sequence[Sequence[float]]) -> float:
    """Map relative McCamy CCT to actual ambient CCT using piecewise linear calibration points."""
    if not points:
        return measured_cct
    sorted_pts = sorted((float(m), float(a)) for m, a in points)
    if len(sorted_pts) == 1:
        m0, a0 = sorted_pts[0]
        return measured_cct + (a0 - m0)
    if measured_cct <= sorted_pts[0][0]:
        return sorted_pts[0][1]
    if measured_cct >= sorted_pts[-1][0]:
        return sorted_pts[-1][1]
    for (m0, a0), (m1, a1) in zip(sorted_pts, sorted_pts[1:]):
        if m0 <= measured_cct <= m1:
            return _lerp_map(measured_cct, m0, m1, a0, a1)
    return measured_cct


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
        # TODO(calibration): estimated values for gamma=1.0; tune with --dry-run
        ev_log_min: float = -8.8,
        ev_log_max: float = -2.3,
        brightness_min_pct: int = 15,
        brightness_max_pct: int = 95,
        ambient_cct_min: float = 2800.0,
        ambient_cct_max: float = 6500.0,
        display_cct_min: int = 3400,
        display_cct_max: int = 6200,
        cct_offset: int = 0,
        # TODO(calibration): populate [measured_cct, actual_cct] pairs from known light sources
        cct_calibration: Optional[list[list[float]]] = None,
        ema_alpha: float = 0.35,
        brightness_deadband_pct: int = 2,
        cct_deadband_k: int = 80,
        brightness_offset_pct: int = 0,
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
        self.cct_calibration: list[list[float]] = list(cct_calibration) if cct_calibration else []
        self.ema_alpha = ema_alpha
        self.brightness_deadband_pct = brightness_deadband_pct
        self.cct_deadband_k = cct_deadband_k
        self.brightness_offset_pct: int = int(brightness_offset_pct)
        # False in shell mode: GNOME's slider is the manual offset, so the learned value is ignored.
        self.offset_enabled: bool = True
        # Night schedule (v0.1.9): when set, the CCT target is this value; brightness stays adaptive.
        self.fixed_cct: Optional[int] = None

        self._ema_ev_log: Optional[float] = None
        # EMA runs on the raw (uncalibrated) CCT; calibration is applied after smoothing (v0.1.8).
        self._ema_raw_cct: Optional[float] = None
        self._last_applied_brightness: Optional[int] = None
        self._last_applied_cct: Optional[int] = None

    def invalidate_applied(self) -> None:
        """Forget the last applied values so the next target is applied even if unchanged."""
        self._last_applied_brightness = None
        self._last_applied_cct = None

    def _effective_offset(self) -> int:
        return self.brightness_offset_pct if self.offset_enabled else 0

    def _targets(self) -> tuple[int, int]:
        """Map the smoothed state to (brightness %, CCT K) using the current parameters."""
        assert self._ema_ev_log is not None and self._ema_raw_cct is not None
        smoothed_cct = _calibrate_cct(self._ema_raw_cct, self.cct_calibration)

        raw_brightness = (
            _lerp_map(
                self._ema_ev_log,
                self.ev_log_min,
                self.ev_log_max,
                float(self.brightness_min_pct),
                float(self.brightness_max_pct),
            )
            + self._effective_offset()
        )
        target_brightness = int(round(_clamp(raw_brightness, self.brightness_min_pct, self.brightness_max_pct)))

        raw_cct = _lerp_map(
            smoothed_cct,
            self.ambient_cct_min,
            self.ambient_cct_max,
            float(self.display_cct_min),
            float(self.display_cct_max),
        ) + self.cct_offset
        # The display range (display-cct-min/max, schema allows up to 10000K) already bounds the map;
        # this clamp only guards against offsets pushing past what Night Light can sensibly show.
        target_cct = int(round(_clamp(raw_cct, _CCT_FLOOR_K, _CCT_CEIL_K)))
        if self.fixed_cct is not None:
            target_cct = int(self.fixed_cct)
        return target_brightness, target_cct

    def update(self, reading: SensorReading, actual_brightness_pct: Optional[int] = None) -> ControlTarget:
        if (
            self.offset_enabled
            and actual_brightness_pct is not None
            and actual_brightness_pct >= 0
            and self._last_applied_brightness is not None
        ):
            manual_diff = actual_brightness_pct - self._last_applied_brightness
            if abs(manual_diff) >= self.brightness_deadband_pct:
                self.brightness_offset_pct = int(
                    _clamp(self.brightness_offset_pct + manual_diff, -100, 100)
                )
                self._last_applied_brightness = actual_brightness_pct

        if self._ema_ev_log is None or self._ema_raw_cct is None:
            self._ema_ev_log = reading.ev_log
            self._ema_raw_cct = reading.cct
        else:
            a = self.ema_alpha
            self._ema_ev_log = a * reading.ev_log + (1.0 - a) * self._ema_ev_log
            self._ema_raw_cct = a * reading.cct + (1.0 - a) * self._ema_raw_cct

        target_brightness, target_cct = self._targets()

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
            smoothed_cct=_calibrate_cct(self._ema_raw_cct, self.cct_calibration),
        )

    def recompute(self, want_brightness: bool = True, want_cct: bool = True) -> Optional[ControlTarget]:
        """Recompute the target from the stored EMA state without a new measurement.

        No deadband: any difference from the last applied value is marked for applying, so a
        small settings change shows up at once. want_* = False leaves that channel untouched
        (its last applied value is not updated). Returns None before the first measurement.
        """
        if self._ema_ev_log is None or self._ema_raw_cct is None:
            return None
        target_brightness, target_cct = self._targets()

        apply_brightness = want_brightness and target_brightness != self._last_applied_brightness
        apply_cct = want_cct and target_cct != self._last_applied_cct
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
            smoothed_cct=_calibrate_cct(self._ema_raw_cct, self.cct_calibration),
        )
