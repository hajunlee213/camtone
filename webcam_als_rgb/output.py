from pathlib import Path
import subprocess

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
    ):
        resolved_name, resolved_dir, found = _discover_backlight(backlight_name)
        self.backlight_name = resolved_name
        self.backlight_dir = resolved_dir
        self._backlight_available = found
        self.enable_brightness = enable_brightness
        self.enable_color_temp = enable_color_temp
        self.dry_run = dry_run
        self._max_brightness = self._read_max_brightness()
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

    def ensure_night_light_active(self) -> None:
        if not self.enable_color_temp or self.dry_run:
            return
        if not self._saved_night_light:
            for key in _NIGHT_LIGHT_KEYS:
                try:
                    self._saved_night_light[key] = self._gsettings_get(key)
                except subprocess.CalledProcessError:
                    pass
        self._gsettings_set("night-light-enabled", "true")
        self._gsettings_set("night-light-schedule-automatic", "false")
        self._gsettings_set("night-light-schedule-from", "0.0")
        self._gsettings_set("night-light-schedule-to", "24.0")

    def restore_night_light(self) -> None:
        if not self.enable_color_temp or self.dry_run or not self._saved_night_light:
            return
        for key, value in self._saved_night_light.items():
            try:
                self._gsettings_set(key, value)
            except subprocess.CalledProcessError:
                pass
        self._saved_night_light.clear()

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
