import argparse
import json
from pathlib import Path
import signal
import subprocess
import time
from typing import Any

from .controller import AdaptiveController
from .output import GnomeWaylandOutput
from .sensor import WebcamSensor

DEFAULT_CONFIG: dict[str, Any] = {
    "sensor": {
        "device": "/dev/video0",
        "width": 320,
        "height": 240,
        "wb_ref_temp": 4600,
        "min_exposure": 50,
        # 2026-11-13 실측: 유효 노출 상한 약 4800에 여유를 둔 값 (v0.1.2 계획서 2절)
        "max_exposure": 4000,
        "initial_exposure": 350,
        "black_level": 16.0,
        # 2026-11-13 실측: luma<=80에서 노출에 정비례, v0.1.2 계획서 2절
        "gamma": 1.0,
        "yuv_range": "limited",
        "target_luma_min": 50.0,
        "target_luma_max": 80.0,
        "target_luma_setpoint": 65.0,
        "roi_mode": "full",
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
    },
    "output": {
        "backlight_name": "intel_backlight",
        "enable_brightness": True,
        "enable_color_temp": True,
    },
    "interval_sec": 15.0,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return DEFAULT_CONFIG
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _deep_merge(DEFAULT_CONFIG, data)


def run_cycle(
    sensor: WebcamSensor,
    controller: AdaptiveController,
    output: GnomeWaylandOutput,
    dry_run: bool,
) -> None:
    reading = sensor.sample()
    ts = time.strftime("%H:%M:%S")
    if reading is None:
        print(f"[{ts}] SKIP: {sensor.device} is busy (used by another app)")
        return

    target = controller.update(reading)
    actions = []
    if target.apply_brightness and output.enable_brightness:
        output.set_brightness_pct(target.brightness_pct)
        actions.append(f"brightness={target.brightness_pct}%")
    if target.apply_cct and output.enable_color_temp:
        output.set_color_temperature(target.cct)
        actions.append(f"cct={target.cct}K")

    mode_tag = "DRY-RUN" if dry_run else "APPLIED"
    action_str = ", ".join(actions) if actions else "no-change (within deadband)"
    print(
        f"[{ts}] exp={reading.exposure:4d} luma={reading.luma:5.1f} "
        f"ev_log={reading.ev_log:5.2f} amb_cct={reading.cct:5.0f}K "
        f"| target=({target.brightness_pct}%, {target.cct}K) [{mode_tag}: {action_str}]"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="webcam-as-als-rgb",
        description="Use laptop webcam as an Ambient Light (ALS) + Color Temperature (RGB/CCT) sensor on GNOME Wayland.",
    )
    parser.add_argument("--config", help="Path to JSON config file")
    parser.add_argument("--once", action="store_true", help="Run a single measurement cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Measure and print targets without changing display settings")
    parser.add_argument("--interval", type=float, help="Polling interval in seconds (overrides config)")
    parser.add_argument("--no-brightness", action="store_true", help="Disable backlight control")
    parser.add_argument("--no-color", action="store_true", help="Disable Night Light color temperature control")
    args = parser.parse_args(argv)

    def _handle_sigterm(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    cfg = load_config(args.config)
    interval = args.interval if args.interval is not None else float(cfg["interval_sec"])

    sensor = WebcamSensor(**cfg["sensor"])
    controller = AdaptiveController(**cfg["controller"])
    out_cfg = dict(cfg["output"])
    if args.no_brightness:
        out_cfg["enable_brightness"] = False
    if args.no_color:
        out_cfg["enable_color_temp"] = False
    output = GnomeWaylandOutput(**out_cfg, dry_run=args.dry_run)

    if not args.dry_run:
        output.ensure_night_light_active()

    try:
        if args.once:
            try:
                run_cycle(sensor, controller, output, args.dry_run)
            except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] ERROR: cycle failed ({type(exc).__name__}: {exc})")
                return 1
            return 0

        print(f"Starting webcam-as-als-rgb (interval={interval}s, dry_run={args.dry_run}). Press Ctrl+C to stop.")
        try:
            while True:
                try:
                    run_cycle(sensor, controller, output, args.dry_run)
                except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] WARN: cycle failed ({type(exc).__name__}: {exc})")
                time.sleep(interval)
        except (KeyboardInterrupt, SystemExit):
            print("\nStopped.")
            return 0
    finally:
        if not args.dry_run:
            output.restore_night_light()
