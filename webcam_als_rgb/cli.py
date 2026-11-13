import argparse
import signal
import subprocess
import time
from typing import Any

from .controller import AdaptiveController
from .output import GnomeWaylandOutput
from .sensor import SensorReading, WebcamSensor, get_exposure_range
from .settings import load_cfg, open_settings


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

    actual_brightness = (
        output.get_brightness_pct()
        if (not dry_run and output.enable_brightness)
        else None
    )
    target = controller.update(reading, actual_brightness_pct=actual_brightness)
    actions = []
    b_target: int | None = None
    c_target: int | None = None
    if target.apply_brightness and output.enable_brightness:
        b_target = target.brightness_pct
        actions.append(f"brightness={target.brightness_pct}%")
    if target.apply_cct and output.enable_color_temp:
        c_target = target.cct
        actions.append(f"cct={target.cct}K")
    if b_target is not None or c_target is not None:
        output.apply_transition(brightness_pct=b_target, cct=c_target)

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

    # 1. Separate anomalous exposures (suspect firmware glitch)
    anomalies: list[SensorReading] = []
    normal_rows: list[SensorReading] = []
    last_normal: SensorReading | None = None

    if (
        asc_rows[1].exposure > 0
        and asc_rows[0].luma_effective
        < asc_rows[1].luma_effective * (asc_rows[0].exposure / asc_rows[1].exposure) * 0.65
    ):
        anomalies.append(asc_rows[0])
    else:
        normal_rows.append(asc_rows[0])
        last_normal = asc_rows[0]

    for r in asc_rows[1:]:
        if last_normal is not None and r.luma < last_normal.luma * 0.80:
            anomalies.append(r)
        else:
            normal_rows.append(r)
            last_normal = r

    # 2. Linear range: exclude dark-floor points (luma_effective < 10.0) and take contiguous
    # prefix where cumulative (max(ev_log) - min(ev_log)) <= 0.15
    lin_candidates = [r for r in normal_rows if r.luma_effective >= 10.0]
    if len(lin_candidates) < 2:
        lin_candidates = normal_rows

    lin_rows: list[SensorReading] = []
    if len(lin_candidates) >= 2:
        run_min = lin_candidates[0].ev_log
        run_max = lin_candidates[0].ev_log
        for r in lin_candidates:
            new_min = min(run_min, r.ev_log)
            new_max = max(run_max, r.ev_log)
            if (new_max - new_min) <= 0.15:
                lin_rows.append(r)
                run_min = new_min
                run_max = new_max
            else:
                break

    # 3. Effective exposure cap on normal_rows
    if not normal_rows:
        cap_msg = "N/A"
    else:
        cap_msg = f"none observed up to exp={normal_rows[-1].exposure}"
        for idx in range(1, len(normal_rows)):
            r0 = normal_rows[idx - 1]
            r1 = normal_rows[idx]
            if r1.luma <= r0.luma - 1.5 or (
                r1.exposure >= r0.exposure * 1.10 and r1.luma <= r0.luma
            ):
                cap_msg = f"exp={r1.exposure} (luma {r0.luma:.1f} -> {r1.luma:.1f})"
                break

    anom_msg = (
        ", ".join(
            f"exp={r.exposure} (luma={r.luma:.1f}, ev_log={r.ev_log:.2f})"
            for r in anomalies
        )
        if anomalies
        else "none"
    )

    print("\nSummary:")
    if len(lin_rows) >= 2:
        luma_lo = min(r.luma for r in lin_rows)
        luma_hi = max(r.luma for r in lin_rows)
        ev_vals = [r.ev_log for r in lin_rows]
        ev_spread = max(ev_vals) - min(ev_vals)
        verdict = "PASS" if ev_spread <= 0.30 else "FAIL"
        print(
            f"  Linear luma range (est.): {luma_lo:.1f} ~ {luma_hi:.1f} "
            f"(exp {lin_rows[0].exposure} ~ {lin_rows[-1].exposure})"
        )
        print(f"  Anomalous exposures (suspect glitch): {anom_msg}")
        print(f"  Effective exposure cap (est.): {cap_msg}")
        print(f"  Linear ev_log spread (max-min): {ev_spread:.2f} [{verdict}, threshold <= 0.30]")
    else:
        print(
            "  Linear luma range (est.): none detected "
            "(cumulative ev_log spread > 0.15 from lowest non-dark exposure)"
        )
        print(f"  Anomalous exposures (suspect glitch): {anom_msg}")
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
        default="200,400,800,1200,1600,2400,3200,4800,6400,8000",
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

    cfg = load_cfg(open_settings(), args.config)
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
        if output.restore_night_light(force=True):
            print(f"Restored Night Light settings from {output.state_file}.")
            return 0
        return 1

    interval = args.interval if args.interval is not None else float(cfg["service"]["interval_ac_sec"])

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
