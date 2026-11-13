"""Resident daemon (v0.1.6): GLib main loop, adaptive measurement schedule, power/lock events.

Started by running the package with no mode flag (``webcam-als-rgb`` / ``python3 -m webcam_als_rgb``).
The GNOME Shell extension starts and stops it through systemd (INT-06); this module only
runs the loop.

Threading: none. Every callback (timer, GSettings, D-Bus signal, Unix signal) runs on the
default main context. A measurement (``run_cycle``) is synchronous and takes ~1-2 s, so
signals that arrive during it are dispatched only after it returns.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

from gi.repository import Gio, GLib

from . import __version__
from .controller import AdaptiveController, ControlTarget
from .output import GnomeWaylandOutput
from .schedule import in_night_schedule, local_seconds_of_day, seconds_until_boundary
from .sensor import SensorReading, WebcamSensor
from .settings import (
    INTERVAL_KEYS,
    OFFSET_KEY,
    RECOMPUTE_KEYS,
    apply_controller_settings,
    load_cfg,
    open_settings,
    persist_learned_offset,
)

# Mapping-key changes wait this long before recomputing (the SpinRow emits several changes per click).
RECOMPUTE_DEBOUNCE_MS = 300

# Seconds to wait after unlock / wake before the first measurement (adaptive-tone's resume-delay-sec).
RESUME_DELAY_SEC = 2.0
# Consecutive unchanged measurements before the interval doubles (plan INT-05).
UNCHANGED_BEFORE_BACKOFF = 3

_CYCLE_ERRORS = (subprocess.CalledProcessError, RuntimeError, OSError, ValueError)

BUS_NAME = "io.github.hajunlee213.WebcamAlsRgb"
OBJECT_PATH = "/io/github/hajunlee213/WebcamAlsRgb"
PAUSED_ERROR = f"{BUS_NAME}.Paused"

# Property name -> D-Bus signature. Read-only; the daemon owns the values.
_PROP_SIGNATURES = {
    "State": "s",
    "EvLog": "d",
    "AmbientCct": "d",
    "TargetBrightness": "i",
    "TargetCct": "i",
    "LastMeasured": "x",
    "BrightnessBackend": "s",
}
_PROP_DEFAULTS: dict[str, Any] = {
    "State": "starting",
    "EvLog": 0.0,
    "AmbientCct": 0.0,
    "TargetBrightness": -1,
    "TargetCct": -1,
    "LastMeasured": 0,
    "BrightnessBackend": "none",
}
_INTROSPECTION_XML = f"""
<node>
  <interface name="{BUS_NAME}">
    <method name="MeasureNow"/>
    <property name="State" type="s" access="read"/>
    <property name="EvLog" type="d" access="read"/>
    <property name="AmbientCct" type="d" access="read"/>
    <property name="TargetBrightness" type="i" access="read"/>
    <property name="TargetCct" type="i" access="read"/>
    <property name="LastMeasured" type="x" access="read"/>
    <property name="NextMeasureIn" type="i" access="read"/>
    <property name="BrightnessBackend" type="s" access="read"/>
  </interface>
</node>
"""


@dataclass
class CycleResult:
    status: str  # "ok" or "busy"
    changed: bool = False
    reading: Optional[SensorReading] = None
    target: Optional[ControlTarget] = None
    wall_sec: float = 0.0
    cpu_sec: float = 0.0


def _now() -> str:
    return time.strftime("%H:%M:%S")


def run_cycle(
    sensor: WebcamSensor,
    controller: AdaptiveController,
    output: GnomeWaylandOutput,
    dry_run: bool,
) -> CycleResult:
    """One measurement: sample the webcam, update the controller, start the display transition.

    Returns immediately after the first transition step; later steps run on the main loop.
    Wall and CPU time cover the whole cycle (sampling + control + first step), per plan §6.3.
    """
    ts = _now()
    t_wall = time.monotonic()
    t_cpu = time.process_time()
    reading = sensor.sample()
    if reading is None:
        wall = time.monotonic() - t_wall
        cpu = time.process_time() - t_cpu
        print(f"[{ts}] SKIP: {sensor.device} is busy (used by another app) [wall={wall:.2f}s cpu={cpu:.2f}s]")
        return CycleResult("busy", wall_sec=wall, cpu_sec=cpu)

    # Offset learning only with the logind backend. In shell mode GNOME's slider is the offset.
    learn_offset = output.brightness_backend == "logind"
    controller.offset_enabled = learn_offset
    # While a fade is running the on-screen brightness is an intermediate value, not the last
    # target. Skipping the manual-change check then avoids learning a fake offset.
    actual_brightness = (
        output.get_brightness_pct()
        if (learn_offset and not dry_run and output.enable_brightness and not output.transitioning)
        else None
    )
    target = controller.update(reading, actual_brightness_pct=actual_brightness)
    actions = []
    b_target: Optional[int] = None
    c_target: Optional[int] = None
    if target.apply_brightness and output.enable_brightness:
        b_target = target.brightness_pct
        actions.append(f"brightness={target.brightness_pct}%")
    if target.apply_cct and output.enable_color_temp:
        c_target = target.cct
        actions.append(f"cct={target.cct}K")
    if b_target is not None or c_target is not None:
        output.apply_transition(brightness_pct=b_target, cct=c_target)

    wall = time.monotonic() - t_wall
    cpu = time.process_time() - t_cpu
    mode_tag = "DRY-RUN" if dry_run else "APPLIED"
    action_str = ", ".join(actions) if actions else "no-change (within deadband)"
    print(
        f"[{ts}] exp={reading.exposure:4d} luma={reading.luma:5.1f} "
        f"ev_log={reading.ev_log:5.2f} amb_cct={reading.cct:5.0f}K "
        f"| target=({target.brightness_pct}%, {target.cct}K) [{mode_tag}: {action_str}] "
        f"[wall={wall:.2f}s cpu={cpu:.2f}s]"
    )
    return CycleResult(
        "ok",
        changed=bool(actions),
        reading=reading,
        target=target,
        wall_sec=wall,
        cpu_sec=cpu,
    )


class Service:
    def __init__(
        self,
        settings: Optional[Gio.Settings],
        config_path: Optional[str],
        overrides: dict[str, Any],
        dry_run: bool,
        cfg: dict[str, Any],
    ) -> None:
        self._settings = settings
        self._config_path = config_path
        self._overrides = overrides
        self._dry_run = dry_run
        self._svc: dict[str, Any] = dict(cfg["service"])

        self._sensor = WebcamSensor(**cfg["sensor"])
        self._controller = AdaptiveController(**cfg["controller"])
        self._output = GnomeWaylandOutput(**cfg["output"], dry_run=dry_run)

        self._loop = GLib.MainLoop()
        self._exit_code = 0

        # Schedule state. _timer_id is the single pending measurement; None means nothing is scheduled.
        self._timer_id: Optional[int] = None
        self._timer_due: Optional[float] = None
        self._interval = 0.0
        self._streak = 0
        self._last_busy = False

        # Pause inputs
        self._lock_active = False
        self._sleeping = False
        self._on_battery = False
        self._night = False  # inside the fixed night schedule (v0.1.9): CCT fixed, brightness still adaptive
        self._night_timer_id: Optional[int] = None

        self._state = ""
        # Debounced recompute after mapping-key changes (v0.1.8). Keys collected in the window.
        self._recompute_id: Optional[int] = None
        self._recompute_keys: list[str] = []
        self._props: dict[str, Any] = dict(_PROP_DEFAULTS)
        self._session: Optional[Gio.DBusConnection] = None
        self._system: Optional[Gio.DBusConnection] = None
        self._conn: Optional[Gio.DBusConnection] = None  # service bus connection (set in _on_bus_acquired)
        self._bus_owner_id = 0

    # ----- helpers -------------------------------------------------------

    def _log(self, msg: str) -> None:
        print(f"[{_now()}] {msg}")

    def _base_interval(self) -> float:
        key = "interval_battery_sec" if self._on_battery else "interval_ac_sec"
        return max(1.0, float(self._svc[key]))

    def _max_interval(self) -> float:
        return max(self._base_interval(), float(self._svc["interval_max_sec"]))

    def _pause_reason(self) -> Optional[str]:
        if self._sleeping:
            return "sleep"
        if self._lock_active:
            return "locked"
        if self._svc["pause_on_battery"] and self._on_battery:
            return "battery"
        return None

    def _publish(self, changed: dict[str, Any]) -> None:
        """Store runtime properties and emit PropertiesChanged for the ones that moved."""
        changed = {k: v for k, v in changed.items() if self._props.get(k) != v}
        if not changed:
            return
        self._props.update(changed)
        if self._conn is None:
            return
        values = {k: GLib.Variant(_PROP_SIGNATURES[k], v) for k, v in changed.items()}
        try:
            self._conn.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (BUS_NAME, values, [])),
            )
        except GLib.Error as exc:
            self._log(f"WARN: PropertiesChanged not sent ({exc.message})")

    def _refresh_state(self) -> None:
        reason = self._pause_reason()
        if reason is not None:
            state = f"paused-{reason}"
        elif self._last_busy:
            state = "camera-busy"
        else:
            state = "running"
        if state != self._state:
            self._log(f"STATE: {self._state or 'starting'} -> {state}")
            self._state = state
            self._publish({"State": state})
        self._publish({"BrightnessBackend": self._output.brightness_backend})

    # ----- timer ---------------------------------------------------------

    def _schedule(self, seconds: float) -> None:
        self._cancel_timer()
        seconds = max(0.0, float(seconds))
        self._timer_id = GLib.timeout_add(int(round(seconds * 1000.0)), self._on_timer)
        self._timer_due = time.monotonic() + seconds

    def _cancel_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
        self._timer_id = None
        self._timer_due = None

    def _on_timer(self) -> bool:
        # One-shot: the source is removed by returning False; _measure() schedules the next one.
        self._timer_id = None
        self._timer_due = None
        try:
            self._measure()
        except Exception as exc:  # keep the schedule alive if something unexpected escapes
            self._log(f"ERROR: measurement loop failed ({type(exc).__name__}: {exc}); retrying")
            self._interval = self._base_interval()
            self._streak = 0
            self._refresh_state()
            if self._pause_reason() is None:
                self._schedule(self._interval)
        return False

    def _pause(self) -> None:
        self._cancel_timer()
        # Lock / sleep / battery: finish the fade at once so the screen is not left half-way.
        self._output.cancel_transition(complete=True)
        self._refresh_state()

    def _resume(self, delay: float) -> None:
        self._sync_night()  # a suspend may have crossed a schedule boundary
        self._interval = self._base_interval()
        self._streak = 0
        self._refresh_state()
        if self._pause_reason() is None:
            self._schedule(delay)

    # ----- fixed night schedule (v0.1.9) ---------------------------------
    # The camera keeps measuring (brightness stays adaptive; this laptop has no light sensor).
    # Only the color temperature target is replaced by a fixed value.

    def _night_window(self) -> tuple[bool, int, int]:
        return bool(self._svc["night_enabled"]), int(self._svc["night_from_min"]), int(self._svc["night_to_min"])

    def _on_night_timer(self) -> bool:
        self._night_timer_id = None
        try:
            self._sync_night()
        except Exception as exc:
            self._log(f"ERROR: night schedule failed ({type(exc).__name__}: {exc})")
            self._arm_night_timer(60)
        return False

    def _arm_night_timer(self, seconds: Optional[int]) -> None:
        if self._night_timer_id is not None:
            GLib.source_remove(self._night_timer_id)
            self._night_timer_id = None
        if seconds is not None:
            self._night_timer_id = GLib.timeout_add_seconds(max(1, int(seconds)), self._on_night_timer)

    def _sync_night(self, force_apply: bool = False) -> None:
        """Recompute whether we are inside the schedule; switch the CCT mode and re-arm the boundary timer."""
        enabled, f, t = self._night_window()
        cur = local_seconds_of_day()
        night = in_night_schedule(enabled, f, t, cur // 60)
        self._arm_night_timer(seconds_until_boundary(enabled, f, t, cur))
        fixed = int(self._svc["night_temp"]) if night else None
        if night == self._night and not (night and force_apply and fixed != self._controller.fixed_cct):
            return
        if night != self._night:
            self._log(f"NIGHT: schedule {'started; CCT fixed at ' + str(fixed) + 'K' if night else 'ended; CCT adaptive again'}")
        self._night = night
        self._controller.fixed_cct = fixed
        self._controller.invalidate_applied()
        if self._pause_reason() is None:
            self._do_recompute("night")

    def _measure(self) -> None:
        if self._pause_reason() is not None:
            self._refresh_state()
            return

        res: Optional[CycleResult]
        try:
            res = run_cycle(self._sensor, self._controller, self._output, self._dry_run)
        except _CYCLE_ERRORS as exc:
            self._log(f"WARN: cycle failed ({type(exc).__name__}: {exc})")
            res = None

        self._last_busy = res is not None and res.status == "busy"
        base = self._base_interval()
        if res is None or res.status == "busy" or res.changed:
            self._interval = base
            self._streak = 0
        else:
            self._streak += 1
            if self._streak >= UNCHANGED_BEFORE_BACKOFF:
                self._interval = min(self._max_interval(), max(base, self._interval * 2.0))
                self._streak = 0

        if res is not None and res.status == "ok" and res.reading is not None and res.target is not None:
            self._publish(
                {
                    "EvLog": float(res.reading.ev_log),
                    "AmbientCct": float(res.reading.cct),
                    "TargetBrightness": int(res.target.brightness_pct),
                    "TargetCct": int(res.target.cct),
                    "LastMeasured": int(time.time()),
                }
            )
            if self._controller.offset_enabled:  # logind backend only; shell mode never learns
                persist_learned_offset(self._controller, self._settings, self._dry_run)

        self._refresh_state()
        if self._pause_reason() is None:
            self._schedule(self._interval)

    # ----- event handlers ------------------------------------------------

    def _on_lock_changed(self, active: bool) -> None:
        if active == self._lock_active:
            return
        self._lock_active = active
        self._log("SCREEN: locked" if active else "SCREEN: unlocked")
        if active:
            self._pause()
        else:
            self._resume(RESUME_DELAY_SEC)

    def _on_sleep_changed(self, sleeping: bool) -> None:
        if sleeping == self._sleeping:
            return
        self._sleeping = sleeping
        self._log("SLEEP: suspending" if sleeping else "SLEEP: resumed")
        if sleeping:
            self._pause()
        else:
            self._resume(RESUME_DELAY_SEC)

    def _on_battery_changed(self, on_battery: bool) -> None:
        if on_battery == self._on_battery:
            return
        self._on_battery = on_battery
        self._log("POWER: on battery" if on_battery else "POWER: on AC")
        self._interval = self._base_interval()
        self._streak = 0
        if self._pause_reason() is not None:
            self._pause()
        else:
            self._schedule(0.0)  # recalculated base interval; measure once now

    def _on_settings_changed(self, _settings: Gio.Settings, key: str) -> None:
        cfg = load_cfg(self._settings, self._config_path, self._overrides)
        if key == "enabled":
            if not cfg["service"]["enabled"]:
                self._log("CONFIG: enabled=false; stopping")
                self._loop.quit()
            return

        if key.startswith("night-schedule-"):
            self._log(f"CONFIG: {key} changed")
            self._svc = dict(cfg["service"])
            self._sync_night(force_apply=True)
            return

        self._log(f"CONFIG: {key} changed")
        apply_controller_settings(self._controller, cfg["controller"], include_offset=(key == OFFSET_KEY))

        ocfg = cfg["output"]
        prev_color = self._output.enable_color_temp
        prev_bright = self._output.enable_brightness
        self._output.enable_brightness = bool(ocfg["enable_brightness"])
        self._output.enable_color_temp = bool(ocfg["enable_color_temp"])
        if prev_color and not self._output.enable_color_temp:
            self._output.restore_night_light(force=True)
        elif self._output.enable_color_temp and not prev_color and not self._dry_run:
            self._output.ensure_night_light_active()
        if prev_bright and not self._output.enable_brightness:
            self._output.release_brightness_control()  # hand brightness back to GNOME

        if key == "auto-brightness" or key == "auto-color-temp":
            turned_on = (not prev_bright and self._output.enable_brightness) or (
                not prev_color and self._output.enable_color_temp
            )
            if turned_on:
                # Re-apply at once; the applied values may be stale from while the feature was off.
                self._controller.invalidate_applied()
                self._do_recompute(key)
        elif key in RECOMPUTE_KEYS:
            self._schedule_recompute(key)

        self._svc = dict(cfg["service"])
        if key in INTERVAL_KEYS or key == "pause-on-battery":
            self._interval = self._base_interval()
            self._streak = 0
            if self._pause_reason() is not None:
                self._pause()
            elif self._timer_id is None:
                self._schedule(0.0)
            else:
                self._schedule(self._interval)

    # ----- live recompute (v0.1.8, A2) -----------------------------------

    def _schedule_recompute(self, key: str) -> None:
        if key not in self._recompute_keys:
            self._recompute_keys.append(key)
        if self._recompute_id is not None:
            GLib.source_remove(self._recompute_id)
        self._recompute_id = GLib.timeout_add(RECOMPUTE_DEBOUNCE_MS, self._on_recompute_timeout)

    def _cancel_recompute(self) -> None:
        if self._recompute_id is not None:
            GLib.source_remove(self._recompute_id)
        self._recompute_id = None
        self._recompute_keys = []

    def _on_recompute_timeout(self) -> bool:
        self._recompute_id = None
        reason = ",".join(self._recompute_keys) or "settings"
        self._recompute_keys = []
        try:
            self._do_recompute(reason)
        except Exception as exc:  # a GLib callback must not leave the loop in a bad state
            self._log(f"ERROR: recompute failed ({type(exc).__name__}: {exc})")
        return False

    def _do_recompute(self, reason: str) -> None:
        """Re-derive the target from the stored EMA state and apply it. No measurement is taken."""
        if self._pause_reason() is not None:
            return  # the next measurement after resume applies the new settings
        out = self._output
        self._controller.offset_enabled = out.brightness_backend == "logind"
        target = self._controller.recompute(
            want_brightness=out.enable_brightness,
            want_cct=out.enable_color_temp,
        )
        if target is None:
            return  # no measurement yet; the first cycle will apply the settings
        b_target = target.brightness_pct if target.apply_brightness else None
        c_target = target.cct if target.apply_cct else None
        actions = []
        if b_target is not None:
            actions.append(f"brightness={b_target}%")
        if c_target is not None:
            actions.append(f"cct={c_target}K")
        if b_target is not None or c_target is not None:
            out.apply_transition(brightness_pct=b_target, cct=c_target)
        self._publish({"TargetBrightness": int(target.brightness_pct), "TargetCct": int(target.cct)})
        status = f"APPLIED: {', '.join(actions)}" if actions else "NO-CHANGE"
        self._log(
            f"RECOMPUTE ({reason}) | target=({target.brightness_pct}%, {target.cct}K) [{status}]"
        )

    # ----- system bus / session bus subscriptions ------------------------

    def _subscribe(self) -> None:
        try:
            self._session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            self._log(f"WARN: no session bus ({exc.message}); lock state unavailable")
        try:
            self._system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as exc:
            self._log(f"WARN: no system bus ({exc.message}); sleep/battery state unavailable")

        if self._session is not None:
            self._session.signal_subscribe(
                "org.gnome.ScreenSaver",
                "org.gnome.ScreenSaver",
                "ActiveChanged",
                "/org/gnome/ScreenSaver",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_screensaver_signal,
            )
            self._lock_active = self._query_bool(
                self._session, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                "org.gnome.ScreenSaver", "GetActive",
            )
        if self._system is not None:
            self._system.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep_signal,
            )
            self._system.signal_subscribe(
                "org.freedesktop.UPower",
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                "/org/freedesktop/UPower",
                "org.freedesktop.UPower",
                Gio.DBusSignalFlags.NONE,
                self._on_upower_signal,
            )
            self._on_battery = self._query_on_battery()

    @staticmethod
    def _query_bool(conn: Gio.DBusConnection, name: str, path: str, iface: str, method: str) -> bool:
        try:
            res = conn.call_sync(
                name, path, iface, method, None, GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE, 2000, None,
            )
            return bool(res.unpack()[0])
        except GLib.Error:
            return False

    def _query_on_battery(self) -> bool:
        if self._system is None:
            return False
        try:
            res = self._system.call_sync(
                "org.freedesktop.UPower",
                "/org/freedesktop/UPower",
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.freedesktop.UPower", "OnBattery")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            return bool(res.unpack()[0])
        except GLib.Error:
            return False  # UPower not running: treat as AC

    def _on_screensaver_signal(self, _conn, _sender, _path, _iface, _member, params, *_rest) -> None:
        self._on_lock_changed(bool(params.unpack()[0]))

    def _on_prepare_for_sleep_signal(self, _conn, _sender, _path, _iface, _member, params, *_rest) -> None:
        self._on_sleep_changed(bool(params.unpack()[0]))

    def _on_upower_signal(self, _conn, _sender, _path, _iface, _member, params, *_rest) -> None:
        changed = params.unpack()[1]
        if "OnBattery" in changed:
            self._on_battery_changed(bool(changed["OnBattery"]))

    # ----- D-Bus service (INT-07) ---------------------------------------

    def _next_measure_in(self) -> int:
        if self._timer_due is None:
            return -1
        return max(0, int(round(self._timer_due - time.monotonic())))

    def _on_bus_acquired(self, conn: Gio.DBusConnection, _name: str) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML)
        conn.register_object(
            OBJECT_PATH,
            node.interfaces[0],
            self._on_method_call,
            self._on_get_property,
            self._on_set_property,
        )
        self._conn = conn

    def _on_name_acquired(self, _conn: Gio.DBusConnection, _name: str) -> None:
        self._log(f"D-Bus: owning {BUS_NAME}")
        self._start()  # Type=dbus in the unit: ready once the name is ours

    def _on_name_lost(self, _conn: Optional[Gio.DBusConnection], _name: str) -> None:
        self._log(f"ERROR: could not own {BUS_NAME} (another instance running?)")
        self._exit_code = 1
        self._loop.quit()

    def _on_get_property(self, _conn, _sender, _path, _iface, name: str) -> GLib.Variant:
        if name == "NextMeasureIn":
            return GLib.Variant("i", self._next_measure_in())
        return GLib.Variant(_PROP_SIGNATURES[name], self._props[name])

    @staticmethod
    def _on_set_property(_conn, _sender, _path, _iface, _name, _value) -> bool:
        return False  # all properties are read-only

    def _on_method_call(self, _conn, _sender, _path, _iface, method: str, _params, invocation) -> None:
        if method != "MeasureNow":
            invocation.return_dbus_error(f"{BUS_NAME}.UnknownMethod", method)
            return
        reason = self._pause_reason()
        if reason is not None:
            invocation.return_dbus_error(PAUSED_ERROR, f"measurement paused: {reason}")
            return
        self._cancel_timer()
        self._interval = self._base_interval()
        self._streak = 0
        try:
            self._measure()
        except Exception as exc:  # report to the caller instead of dropping the call
            invocation.return_dbus_error(f"{BUS_NAME}.Failed", f"{type(exc).__name__}: {exc}")
            return
        invocation.return_value(None)

    # ----- lifecycle -----------------------------------------------------

    def _on_quit_signal(self) -> bool:
        self._log("SIGNAL: stopping")
        self._loop.quit()
        return GLib.SOURCE_REMOVE

    def _start(self) -> None:
        self._interval = self._base_interval()
        self._sync_night()
        self._refresh_state()
        if self._pause_reason() is None:
            self._measure()  # first measurement right away; _measure schedules the next

    def run(self) -> int:
        self._output.ensure_night_light_active()
        self._subscribe()
        if self._settings is not None:
            self._settings.connect("changed", self._on_settings_changed)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_quit_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_quit_signal)
        self._bus_owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            self._on_name_acquired,
            self._on_name_lost,
        )
        self._loop.run()
        return self._shutdown()

    def _shutdown(self) -> int:
        self._cancel_timer()
        self._arm_night_timer(None)
        self._cancel_recompute()
        self._output.cancel_transition(complete=True)
        self._output.release_brightness_control()
        if not self._dry_run:
            if self._output.restore_night_light(force=True):
                self._log("Night Light restored")
        self._log("service stopped")
        return self._exit_code


def run(config_path: Optional[str], overrides: dict[str, Any], dry_run: bool) -> int:
    """Entry point for service mode (no --once/--sweep/--restore-night-light)."""
    sys.stdout.reconfigure(line_buffering=True)  # journald sees lines immediately
    settings = open_settings()
    cfg = load_cfg(settings, config_path, overrides)
    if not cfg["service"]["enabled"]:
        print("webcam-als-rgb: enabled=false; not starting.")
        return 0
    print(
        f"webcam-als-rgb {__version__} service starting "
        f"(pid={os.getpid()}, dry_run={dry_run}, schema={'yes' if settings is not None else 'no'}, "
        f"interval ac={cfg['service']['interval_ac_sec']}s battery={cfg['service']['interval_battery_sec']}s "
        f"max={cfg['service']['interval_max_sec']}s, pause_on_battery={cfg['service']['pause_on_battery']})"
    )
    svc = Service(settings, config_path, overrides, dry_run, cfg)
    return svc.run()
