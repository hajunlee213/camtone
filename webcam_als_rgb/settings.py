"""GSettings <-> daemon config (v0.1.6).

Config precedence (lowest first):
  1. DEFAULT_CONFIG (code defaults; ``sensor`` section is code-only)
  2. GSettings ``org.gnome.shell.extensions.webcam-als-rgb`` if the schema is installed
  3. ``--config path.json`` (development override; applied last)
  4. CLI overrides (``--no-brightness``, ``--no-color``, ``--interval``)

If the schema is not installed, ``open_settings()`` returns None and steps 2 is skipped,
so ``--once``/``--sweep`` keep working from a plain checkout.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from gi.repository import Gio

if TYPE_CHECKING:  # pragma: no cover
    from .controller import AdaptiveController

SCHEMA_ID = "org.gnome.shell.extensions.webcam-als-rgb"

# GSettings keys whose change requires the measurement schedule to be recomputed.
INTERVAL_KEYS = frozenset({"interval-ac-sec", "interval-battery-sec", "interval-max-sec"})

# GSettings keys that change the brightness/CCT mapping. A change recomputes the current target
# at once (debounced), instead of waiting for the next measurement (v0.1.8, plan A2).
RECOMPUTE_KEYS = frozenset({
    "cct-offset",
    "display-cct-min",
    "display-cct-max",
    "ambient-cct-min",
    "ambient-cct-max",
    "cct-calibration",
    "brightness-min-pct",
    "brightness-max-pct",
    "ev-log-min",
    "ev-log-max",
    "brightness-offset-pct",
})

# Written by the daemon itself (learned manual offset); clamped to the schema range on write.
OFFSET_KEY = "brightness-offset-pct"
OFFSET_RANGE = (-50, 50)

DEFAULT_CONFIG: dict[str, Any] = {
    "sensor": {
        "device": "/dev/video0",
        "width": 320,
        "height": 240,
        "wb_ref_temp": 4600,
        "min_exposure": 50,
        # 2026-11-13 재실측: 노출 10000까지 luma가 계속 증가하나 캡처 시간(LED 점등)과의 균형을 위해 8000으로 설정 (v0.1.3 계획서 2절)
        "max_exposure": 8000,
        "initial_exposure": 350,
        "black_level": 16.0,
        # 2026-11-13 실측: luma<=80에서 노출에 정비례, v0.1.2 계획서 2절
        "gamma": 1.0,
        "yuv_range": "limited",
        "target_luma_min": 50.0,
        "target_luma_max": 80.0,
        "target_luma_setpoint": 65.0,
        "roi_mode": "full",
        "exposure_blacklist": [1250, 2500],
    },
    "controller": {
        # TODO(calibration): estimated values for gamma=1.0; tune with --dry-run
        "ev_log_min": -8.8,
        "ev_log_max": -2.3,
        "brightness_min_pct": 15,
        "brightness_max_pct": 95,
        "ambient_cct_min": 2800.0,
        "ambient_cct_max": 6500.0,
        "display_cct_min": 3400,
        "display_cct_max": 6200,
        "cct_offset": 0,
        # TODO(calibration): populate [measured_cct, actual_cct] pairs from known light sources
        "cct_calibration": [],
        "ema_alpha": 0.35,
        "brightness_deadband_pct": 2,
        "cct_deadband_k": 80,
        "brightness_offset_pct": 0,
    },
    "output": {
        "backlight_name": "intel_backlight",
        "enable_brightness": True,
        "enable_color_temp": True,
        "transition_steps": 4,
        "transition_interval_sec": 0.3,
    },
    "service": {
        "enabled": True,
        "interval_ac_sec": 60.0,
        "interval_battery_sec": 120.0,
        "interval_max_sec": 300.0,
        "pause_on_battery": False,
        "night_enabled": False,
        "night_temp": 3700,
        "night_from_min": 22 * 60,
        "night_to_min": 7 * 60,
    },
}


def open_settings() -> Optional[Gio.Settings]:
    """Return the GSettings object, or None when the schema is not installed."""
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(SCHEMA_ID, True) is None:
        return None
    return Gio.Settings.new(SCHEMA_ID)


def read_gsettings(settings: Gio.Settings) -> dict[str, Any]:
    """Translate the GSettings keys into the same nested shape as DEFAULT_CONFIG."""
    g = settings
    return {
        "controller": {
            "ev_log_min": g.get_double("ev-log-min"),
            "ev_log_max": g.get_double("ev-log-max"),
            "brightness_min_pct": g.get_int("brightness-min-pct"),
            "brightness_max_pct": g.get_int("brightness-max-pct"),
            "ambient_cct_min": g.get_double("ambient-cct-min"),
            "ambient_cct_max": g.get_double("ambient-cct-max"),
            "display_cct_min": g.get_int("display-cct-min"),
            "display_cct_max": g.get_int("display-cct-max"),
            "cct_offset": g.get_int("cct-offset"),
            "cct_calibration": [[float(m), float(a)] for m, a in g.get_value("cct-calibration").unpack()],
            "ema_alpha": g.get_double("ema-alpha"),
            "brightness_deadband_pct": g.get_int("brightness-deadband-pct"),
            "cct_deadband_k": g.get_int("cct-deadband-k"),
            "brightness_offset_pct": g.get_int(OFFSET_KEY),
        },
        "output": {
            "enable_brightness": g.get_boolean("auto-brightness"),
            "enable_color_temp": g.get_boolean("auto-color-temp"),
        },
        "service": {
            "enabled": g.get_boolean("enabled"),
            "interval_ac_sec": float(g.get_int("interval-ac-sec")),
            "interval_battery_sec": float(g.get_int("interval-battery-sec")),
            "interval_max_sec": float(g.get_int("interval-max-sec")),
            "pause_on_battery": g.get_boolean("pause-on-battery"),
            "night_enabled": g.get_boolean("night-schedule-enabled"),
            "night_temp": g.get_int("night-schedule-temp"),
            "night_from_min": g.get_int("night-schedule-from-hour") * 60 + g.get_int("night-schedule-from-minute"),
            "night_to_min": g.get_int("night-schedule-to-hour") * 60 + g.get_int("night-schedule-to-minute"),
        },
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_cfg(
    settings: Optional[Gio.Settings],
    config_path: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the full config dict. Always returns a fresh copy (safe to mutate)."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if settings is not None:
        cfg = _deep_merge(cfg, read_gsettings(settings))
    if config_path:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        cfg = _deep_merge(cfg, data)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    return cfg


def clamp_offset(value: int) -> int:
    lo, hi = OFFSET_RANGE
    return max(lo, min(hi, int(value)))


def apply_controller_settings(
    controller: AdaptiveController,
    ccfg: dict[str, Any],
    *,
    include_offset: bool,
) -> None:
    """Update controller parameters in place so EMA/last-applied state is kept."""
    for name, value in ccfg.items():
        if name == "brightness_offset_pct" and not include_offset:
            continue
        if name == "cct_calibration":
            value = [list(p) for p in value]
        setattr(controller, name, value)


def persist_learned_offset(
    controller: AdaptiveController,
    settings: Optional[Gio.Settings],
    dry_run: bool,
) -> None:
    """Write the learned offset to GSettings only when it differs from the stored value (plan §3 INT-01)."""
    if dry_run or settings is None:
        return
    want = clamp_offset(controller.brightness_offset_pct)
    if settings.get_int(OFFSET_KEY) != want:
        settings.set_int(OFFSET_KEY, want)
        print(f"INFO: learned brightness offset saved: {OFFSET_KEY}={want}")
