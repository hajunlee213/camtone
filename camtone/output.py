from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Optional

from gi.repository import Gio, GLib

_BACKLIGHT_ROOT = Path("/sys/class/backlight")
_TYPE_PRIORITY = {"raw": 0, "platform": 1, "firmware": 2}
_COLOR_SCHEMA = "org.gnome.settings-daemon.plugins.color"
# GNOME Shell's own brightness manager (GNOME 49+).
_SHELL_BUS = "org.gnome.Shell.Brightness"
_SHELL_PATH = "/org/gnome/Shell/Brightness"
_SHELL_IFACE = "org.gnome.Shell.Brightness"
_DBUS_TIMEOUT_MS = 2000

BACKEND_SHELL = "shell"
BACKEND_LOGIND = "logind"
BACKEND_NONE = "none"
_NIGHT_LIGHT_KEYS = (
    "night-light-enabled",
    "night-light-schedule-automatic",
    "night-light-schedule-from",
    "night-light-schedule-to",
    "night-light-temperature",
)


def _default_night_light_state_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg_state) if xdg_state else (Path.home() / ".local" / "state")
    return base / "camtone" / "night-light.json"


def _detect_shell_brightness() -> bool:
    """True when org.gnome.Shell.Brightness is owned on the session bus and has brightness control.

    Read-only: only NameHasOwner and the HasBrightnessControl property are queried.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        owned = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (_SHELL_BUS,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            _DBUS_TIMEOUT_MS,
            None,
        ).unpack()[0]
        if not owned:
            return False
        has_control = bus.call_sync(
            _SHELL_BUS,
            _SHELL_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (_SHELL_IFACE, "HasBrightnessControl")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            _DBUS_TIMEOUT_MS,
            None,
        ).unpack()[0]
        return bool(has_control)
    except GLib.Error:
        return False


def _shell_set_target(fraction: float) -> None:
    """Call SetAutoBrightnessTarget(fraction). -1.0 turns GNOME's auto brightness off."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_sync(
        _SHELL_BUS,
        _SHELL_PATH,
        _SHELL_IFACE,
        "SetAutoBrightnessTarget",
        GLib.Variant("(d)", (float(fraction),)),
        None,
        Gio.DBusCallFlags.NONE,
        _DBUS_TIMEOUT_MS,
        None,
    )


def _discover_backlight(preferred_name: str) -> tuple[str, Path, bool]:
    """Return (name, dir, found) using preferred_name or raw > platform > firmware fallback."""
    preferred_dir = _BACKLIGHT_ROOT / preferred_name
    if preferred_dir.is_dir():
        return preferred_name, preferred_dir, True

    if _BACKLIGHT_ROOT.is_dir():
        candidates: list[tuple[int, str, Path]] = []
        for entry in sorted(_BACKLIGHT_ROOT.iterdir()):
            if not entry.is_dir():
                continue
            type_file = entry / "type"
            btype = type_file.read_text().strip() if type_file.exists() else ""
            prio = _TYPE_PRIORITY.get(btype, 3)
            candidates.append((prio, entry.name, entry))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            _, chosen_name, chosen_dir = candidates[0]
            print(
                f"WARN: backlight '{preferred_name}' not found; "
                f"falling back to '{chosen_name}'"
            )
            return chosen_name, chosen_dir, True

    print(f"WARN: backlight '{preferred_name}' not found and no fallback device in {_BACKLIGHT_ROOT}")
    return preferred_name, preferred_dir, False


class GnomeWaylandOutput:
    def __init__(
        self,
        backlight_name: str = "intel_backlight",
        enable_brightness: bool = True,
        enable_color_temp: bool = True,
        dry_run: bool = False,
        state_file: Optional[Path] = None,
        transition_steps: int = 4,
        transition_interval_sec: float = 0.3,
    ):
        resolved_name, resolved_dir, found = _discover_backlight(backlight_name)
        self.backlight_name = resolved_name
        self.backlight_dir = resolved_dir
        self._backlight_available = found
        self.enable_brightness = enable_brightness
        self.enable_color_temp = enable_color_temp
        self.dry_run = dry_run
        self._max_brightness = self._read_max_brightness()
        self.state_file = state_file if state_file is not None else _default_night_light_state_path()
        self.transition_steps = max(1, int(transition_steps))
        self.transition_interval_sec = max(0.0, float(transition_interval_sec))
        self._current_cct: Optional[int] = None
        self._saved_night_light: dict[str, str] = {}
        self._night_light_paused_warned = False
        self._transition: Optional["_Transition"] = None
        # Brightness backend: "shell" (GNOME target), "logind" (raw backlight), or "none".
        self._backend: Optional[str] = None
        # Last brightness percent sent to the shell backend (its transition start point).
        self._last_sent_brightness: Optional[int] = None
        self._backend = self._resolve_backend()

    def _resolve_backend(self) -> str:
        if _detect_shell_brightness():
            backend = BACKEND_SHELL
        elif self._backlight_available:
            backend = BACKEND_LOGIND
        else:
            backend = BACKEND_NONE
        self._set_backend(backend)
        return backend

    def _set_backend(self, backend: str) -> None:
        if backend != self._backend:
            print(f"BRIGHTNESS: backend={backend}")
            self._backend = backend

    @property
    def brightness_backend(self) -> str:
        """Current brightness backend ("shell", "logind" or "none"). Exposed over D-Bus."""
        return self._backend or BACKEND_NONE

    def _ensure_backend(self) -> str:
        # Re-judged when unknown (after a failed shell call) or "none" (backlight may appear later).
        if self._backend in (None, BACKEND_NONE):
            return self._resolve_backend()
        return self._backend

    def _read_max_brightness(self) -> int:
        max_file = self.backlight_dir / "max_brightness"
        if not max_file.exists():
            return 100
        return int(max_file.read_text().strip())

    def get_brightness_pct(self) -> int:
        cur_file = self.backlight_dir / "brightness"
        if not cur_file.exists():
            return -1
        cur = int(cur_file.read_text().strip())
        return int(round((cur / self._max_brightness) * 100.0))

    def set_brightness_pct(self, pct: int) -> None:
        if not self.enable_brightness or self.dry_run:
            return
        backend = self._ensure_backend()
        if backend == BACKEND_SHELL:
            target = max(0, min(100, int(pct)))
            try:
                _shell_set_target(target / 100.0)
            except GLib.Error as exc:
                # Shell may have restarted: re-judge the backend before the next call.
                self._backend = None
                raise OSError(f"SetAutoBrightnessTarget failed: {exc.message}") from exc
            self._last_sent_brightness = target
            return
        if backend != BACKEND_LOGIND:
            return
        raw_val = max(1, min(self._max_brightness, int(round((pct / 100.0) * self._max_brightness))))
        subprocess.run(
            [
                "gdbus",
                "call",
                "--system",
                "--dest",
                "org.freedesktop.login1",
                "--object-path",
                "/org/freedesktop/login1/session/auto",
                "--method",
                "org.freedesktop.login1.Session.SetBrightness",
                "backlight",
                self.backlight_name,
                str(raw_val),
            ],
            capture_output=True,
            check=True,
        )

    def _gsettings_get(self, key: str) -> str:
        res = subprocess.run(
            ["gsettings", "get", _COLOR_SCHEMA, key],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    def _gsettings_set(self, key: str, value: str) -> None:
        subprocess.run(
            ["gsettings", "set", _COLOR_SCHEMA, key, value],
            capture_output=True,
            check=True,
        )

    def _load_night_light_state(self) -> Optional[dict[str, str]]:
        if not self.state_file.exists():
            return None
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARN: failed to read Night Light state file {self.state_file}: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"WARN: invalid Night Light state file {self.state_file}; ignoring")
            return None
        return {str(k): str(v) for k, v in data.items() if k in _NIGHT_LIGHT_KEYS}

    def _save_night_light_state(self, state: dict[str, str]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".night-light-",
            suffix=".tmp",
            dir=str(self.state_file.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
            os.replace(tmp_name, self.state_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def ensure_night_light_active(self) -> None:
        if not self.enable_color_temp or self.dry_run:
            return
        loaded = self._load_night_light_state()
        if loaded is not None:
            self._saved_night_light = loaded
        elif not self._saved_night_light:
            saved: dict[str, str] = {}
            for key in _NIGHT_LIGHT_KEYS:
                try:
                    saved[key] = self._gsettings_get(key)
                except subprocess.CalledProcessError:
                    pass
            self._saved_night_light = saved
            if saved:
                self._save_night_light_state(saved)
        self._gsettings_set("night-light-enabled", "true")
        self._gsettings_set("night-light-schedule-automatic", "false")
        self._gsettings_set("night-light-schedule-from", "0.0")
        self._gsettings_set("night-light-schedule-to", "24.0")

    def restore_night_light(self, force: bool = False) -> bool:
        if (not self.enable_color_temp and not force) or self.dry_run:
            return False
        if not self._saved_night_light:
            loaded = self._load_night_light_state()
            if loaded:
                self._saved_night_light = loaded
        if not self._saved_night_light:
            return False
        failed_keys: list[str] = []
        for key, value in self._saved_night_light.items():
            try:
                self._gsettings_set(key, value)
            except subprocess.CalledProcessError:
                failed_keys.append(key)
        if failed_keys:
            print(
                f"WARN: failed to restore Night Light keys ({', '.join(failed_keys)}); "
                f"keeping state file {self.state_file}"
            )
            return False
        if self.state_file.exists():
            try:
                self.state_file.unlink()
            except OSError as exc:
                print(f"WARN: failed to remove Night Light state file {self.state_file}: {exc}")
        self._saved_night_light.clear()
        return True

    def set_color_temperature(self, cct: int) -> None:
        if not self.enable_color_temp or self.dry_run:
            return
        try:
            enabled = self._gsettings_get("night-light-enabled")
        except subprocess.CalledProcessError:
            enabled = "true"

        if enabled != "true":
            if not self._night_light_paused_warned:
                print("WARN: Night Light is disabled by user; pausing color temperature updates")
                self._night_light_paused_warned = True
            return

        if self._night_light_paused_warned:
            print("INFO: Night Light re-enabled; resuming color temperature updates")
            self._night_light_paused_warned = False

        self._gsettings_set("night-light-temperature", str(int(cct)))
        self._current_cct = int(cct)

    def get_color_temperature(self) -> Optional[int]:
        if self._current_cct is not None:
            return self._current_cct
        try:
            raw = self._gsettings_get("night-light-temperature")
            tokens = raw.strip().split()
            if tokens:
                return int(tokens[-1])
        except (subprocess.CalledProcessError, ValueError, OSError):
            pass
        return None

    @property
    def transitioning(self) -> bool:
        """True while a non-blocking brightness/color transition is still scheduled."""
        return self._transition is not None

    def apply_transition(
        self,
        brightness_pct: Optional[int] = None,
        cct: Optional[int] = None,
    ) -> None:
        """Start a stepwise transition and return immediately.

        Step 1 is applied synchronously; steps 2..N are GLib timeouts spaced by
        transition_interval_sec. A transition already in progress is superseded:
        it is cancelled without applying its final values, and the new one
        interpolates from the value that is actually on screen right now.
        Steps 2..N need a running GLib main loop (or wait_transition()).
        """
        self.cancel_transition()
        if self.dry_run:
            return
        backend = self._ensure_backend()
        do_b = (
            brightness_pct is not None
            and self.enable_brightness
            and backend in (BACKEND_SHELL, BACKEND_LOGIND)
        )
        do_c = cct is not None and self.enable_color_temp
        if not do_b and not do_c:
            return

        start_b: Optional[int] = None
        if do_b and brightness_pct is not None:
            if backend == BACKEND_SHELL:
                # sysfs includes GNOME's slider bias here, so start from the last value we sent.
                start_b = self._last_sent_brightness if self._last_sent_brightness is not None else brightness_pct
            else:
                cur_b = self.get_brightness_pct()
                start_b = cur_b if cur_b >= 0 else brightness_pct

        start_c: Optional[int] = None
        if do_c and cct is not None:
            cur_c = self.get_color_temperature()
            start_c = cur_c if cur_c is not None else cct

        b_unchanged = (not do_b) or (start_b == brightness_pct)
        c_unchanged = (not do_c) or (start_c == cct)
        if self.transition_steps <= 1 or (b_unchanged and c_unchanged):
            if do_b and brightness_pct is not None:
                self.set_brightness_pct(brightness_pct)
            if do_c and cct is not None:
                self.set_color_temperature(cct)
            return

        t = _Transition(
            start_b=start_b if do_b else None,
            end_b=brightness_pct if do_b else None,
            start_c=start_c if do_c else None,
            end_c=cct if do_c else None,
            steps=self.transition_steps,
        )
        self._transition = t
        if not self._transition_step() or self._transition is not t:
            return
        interval_ms = max(1, int(round(self.transition_interval_sec * 1000.0)))
        t.source_id = GLib.timeout_add(interval_ms, self._on_transition_tick)

    def cancel_transition(self, complete: bool = False) -> None:
        """Stop the pending transition.

        complete=True first writes the transition's final values (one write each), so an
        interrupted fade (lock, sleep, shutdown) does not leave the screen half-way.
        """
        t = self._transition
        if t is None:
            return
        self._transition = None
        if t.source_id is not None:
            GLib.source_remove(t.source_id)
            t.source_id = None
        if complete:
            try:
                if t.end_b is not None:
                    self.set_brightness_pct(t.end_b)
                if t.end_c is not None:
                    self.set_color_temperature(t.end_c)
            except (subprocess.CalledProcessError, OSError, ValueError) as exc:
                print(f"WARN: failed to complete interrupted transition ({type(exc).__name__}: {exc})")

    def release_brightness_control(self) -> None:
        """Hand brightness back to GNOME (shell backend only): SetAutoBrightnessTarget(-1.0).

        Finishes any pending fade first (so a color fade is not left half-way and no later step
        re-takes brightness control), then releases. Not called on lock or sleep; GNOME dims
        the screen itself there.
        """
        self.cancel_transition(complete=True)
        self._last_sent_brightness = None
        if self.dry_run or self._backend != BACKEND_SHELL:
            return
        try:
            _shell_set_target(-1.0)
        except GLib.Error as exc:
            print(f"WARN: failed to release GNOME brightness control ({exc.message})")
            self._backend = None
            return
        print("BRIGHTNESS: released GNOME brightness control")

    def wait_transition(self) -> None:
        """Block by iterating the default GLib context until the transition ends (used by --once)."""
        ctx = GLib.MainContext.default()
        while self._transition is not None:
            ctx.iteration(True)

    def _on_transition_tick(self) -> bool:
        return self._transition_step()

    def _transition_step(self) -> bool:
        """Apply the next step. Returns True if the GLib timeout should keep firing."""
        t = self._transition
        if t is None:
            return False
        t.done += 1
        frac = t.done / t.steps
        try:
            if t.start_b is not None and t.end_b is not None:
                self.set_brightness_pct(int(round(t.start_b + (t.end_b - t.start_b) * frac)))
            if t.start_c is not None and t.end_c is not None:
                self.set_color_temperature(int(round(t.start_c + (t.end_c - t.start_c) * frac)))
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            print(f"WARN: transition aborted at step {t.done}/{t.steps} ({type(exc).__name__}: {exc})")
            self._transition = None
            return False
        if t.done >= t.steps:
            self._transition = None
            return False
        return True


@dataclass
class _Transition:
    start_b: Optional[int]
    end_b: Optional[int]
    start_c: Optional[int]
    end_c: Optional[int]
    steps: int
    done: int = 0
    source_id: Optional[int] = None
