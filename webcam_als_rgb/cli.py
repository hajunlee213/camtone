import argparse
import json
from pathlib import Path
import signal
import subprocess
import time
from typing import Any

from .controller import AdaptiveController
from .output import GnomeWaylandOutput
from .sensor import SensorReading, WebcamSensor, get_exposure_range

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


def _summarize_sweep(rows: list[SensorReading]) -> None:
    asc_rows = sorted(rows, key=lambda r: r.exposure)
    if len(asc_rows) < 2:
        print("\nSummary: insufficient data points (need at least 2 exposures).")
        return

    # Linear range: longest contiguous run where adjacent luma_eff ratio / exp ratio is in [0.90, 1.10]
    best_start = -1
    best_end = -1
    cur_start: int | None = None
    for idx in range(len(asc_rows) - 1):
        r0, r1 = asc_rows[idx], asc_rows[idx + 1]
        exp_ratio = r1.exposure / r0.exposure if r0.exposure > 0 else 0.0
        luma_ratio = r1.luma_effective / r0.luma_effective if r0.luma_effective > 0 else 0.0
        rel = (luma_ratio / exp_ratio) if exp_ratio > 0 else 0.0
        if 0.90 <= rel <= 1.10:
            if cur_start is None:
                cur_start = idx
            if (idx + 1 - cur_start) > (best_end - best_start):
                best_start = cur_start
                best_end = idx + 1
        else:
            cur_start = None

    # Effective exposure cap: first exposure where luma fails to increase
    cap_msg = f"none observed up to exp={asc_rows[-1].exposure}"
    for idx in range(1, len(asc_rows)):
        if asc_rows[idx].luma <= asc_rows[idx - 1].luma:
            cap_msg = (
                f"exp={asc_rows[idx].exposure} "
                f"(luma {asc_rows[idx - 1].luma:.1f} -> {asc_rows[idx].luma:.1f})"
            )
            break

    print("\nSummary:")
    if best_start >= 0:
        lin_rows = asc_rows[best_start : best_end + 1]
        luma_lo = min(r.luma for r in lin_rows)
        luma_hi = max(r.luma for r in lin_rows)
        ev_vals = [r.ev_log for r in lin_rows]
        ev_spread = max(ev_vals) - min(ev_vals)
        verdict = "PASS" if ev_spread <= 0.3 else "FAIL"
        print(
            f"  Linear luma range (est.): {luma_lo:.1f} ~ {luma_hi:.1f} "
            f"(exp {lin_rows[0].exposure} ~ {lin_rows[-1].exposure})"
        )
        print(f"  Effective exposure cap (est.): {cap_msg}")
        print(f"  Linear ev_log spread (max-min): {ev_spread:.2f} [{verdict}, threshold <= 0.30]")
    else:
        print("  Linear luma range (est.): none detected (ratio outside 0.90..1.10)")
        print(f"  Effective exposure cap (est.): {cap_msg}")
        print("  Linear ev_log spread (max-min): N/A [FAIL]")


def run_sweep(sensor: WebcamSensor, exposures_str: str, order: str) -> int:
    try:
        exposures = [int(tok.strip()) for tok in exposures_str.split(",") if tok.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --sweep-exposures value ({exc})")
        return 1
    if not exposures:
        print("ERROR: --sweep-exposures is empty")
        return 1

    exposures = sorted(exposures, reverse=(order == "desc"))
    hw_range = get_exposure_range(sensor.device)
    if hw_range is not None:
        hw_min, hw_max = hw_range
        filtered: list[int] = []
        for exp in exposures:
            if hw_min <= exp <= hw_max:
                filtered.append(exp)
            else:
                print(
                    f"WARN: skipping exp={exp} "
                    f"(outside camera exposure_time_absolute range {hw_min}..{hw_max})"
                )
        exposures = filtered
        if not exposures:
            print("ERROR: no exposures remain within camera range")
            return 1

    print(
        f"Sweep diagnostics on {sensor.device} "
        f"(gamma={sensor.gamma}, yuv_range={sensor.yuv_range}, order={order})"
    )
    print("  exp |  luma | luma_eff | sat_pct |  ev_log | ev_log_gamma1 | amb_cct")
    print("------+-------+----------+---------+---------+---------------+--------")
    rows: list[SensorReading] = []
    for exp in exposures:
        reading = sensor.sample_fixed(exp)
        if reading is None:
            print(f"ERROR: {sensor.device} is busy (used by another app)")
            return 2
        rows.append(reading)
        print(
            f"{reading.exposure:5d} | {reading.luma:5.1f} | {reading.luma_effective:8.1f} | "
            f"{reading.sat_pct:6.2f}% | {reading.ev_log:7.2f} | "
            f"{reading.ev_log_gamma1:13.2f} | {reading.cct:6.0f}K"
        )

    _summarize_sweep(rows)
    return 0


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
    parser.add_argument("--sweep", action="store_true", help="Run fixed-exposure diagnostic sweep (never modifies display)")
    parser.add_argument(
        "--sweep-exposures",
        default="200,400,800,1200,1600,2400,3200,4800,6400",
        help="Comma-separated exposure list for --sweep",
    )
    parser.add_argument(
        "--sweep-order",
        choices=("asc", "desc"),
        default="asc",
        help="Exposure order for --sweep (default: asc)",
    )
    parser.add_argument(
        "--restore-night-light",
        action="store_true",
        help="Restore Night Light settings from saved state file and exit without measuring",
    )
    args = parser.parse_args(argv)

    def _handle_sigterm(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    cfg = load_config(args.config)
    out_cfg = dict(cfg["output"])
    if args.no_brightness:
        out_cfg["enable_brightness"] = False
    if args.no_color:
        out_cfg["enable_color_temp"] = False

    if args.restore_night_light:
        output = GnomeWaylandOutput(**out_cfg, dry_run=args.dry_run)
        if not output.state_file.exists():
            print(f"No Night Light state file found at {output.state_file}; nothing to restore.")
            return 0
        if args.dry_run:
            print(f"[DRY-RUN] Would restore Night Light settings from {output.state_file}.")
            return 0
        if output.restore_night_light():
            print(f"Restored Night Light settings from {output.state_file}.")
            return 0
        return 1

    interval = args.interval if args.interval is not None else float(cfg["interval_sec"])

    sensor = WebcamSensor(**cfg["sensor"])
    if args.sweep:
        try:
            return run_sweep(sensor, args.sweep_exposures, args.sweep_order)
        except (KeyboardInterrupt, SystemExit):
            print("\nStopped.")
            return 0

    controller = AdaptiveController(**cfg["controller"])
    output = GnomeWaylandOutput(**out_cfg, dry_run=args.dry_run)

    if not args.dry_run:
        output.ensure_night_light_active()

    try:
        if args.once:
            try:
                run_cycle(sensor, controller, output, args.dry_run)
            except (subprocess.CalledProcessError, RuntimeError, OSError, ValueError) as exc:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] ERROR: cycle failed ({type(exc).__name__}: {exc})")
                return 1
            return 0

        print(f"Starting webcam-as-als-rgb (interval={interval}s, dry_run={args.dry_run}). Press Ctrl+C to stop.")
        try:
            while True:
                try:
                    run_cycle(sensor, controller, output, args.dry_run)
                except (subprocess.CalledProcessError, RuntimeError, OSError, ValueError) as exc:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] WARN: cycle failed ({type(exc).__name__}: {exc})")
                time.sleep(interval)
        except (KeyboardInterrupt, SystemExit):
            print("\nStopped.")
            return 0
    finally:
        if not args.dry_run:
            output.restore_night_light()
