import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Optional

_BACKLIGHT_ROOT = Path("/sys/class/backlight")
_TYPE_PRIORITY = {"raw": 0, "platform": 1, "firmware": 2}
_COLOR_SCHEMA = "org.gnome.settings-daemon.plugins.color"
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
    return base / "webcam-as-als-rgb" / "night-light.json"


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
        self._saved_night_light: dict[str, str] = {}
        self._night_light_paused_warned = False

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
        if not self.enable_brightness or self.dry_run or not self._backlight_available:
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

    def restore_night_light(self) -> bool:
        if not self.enable_color_temp or self.dry_run:
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
