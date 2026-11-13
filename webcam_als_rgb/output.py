from pathlib import Path
import subprocess


class GnomeWaylandOutput:
    def __init__(
        self,
        backlight_name: str = "intel_backlight",
        enable_brightness: bool = True,
        enable_color_temp: bool = True,
        dry_run: bool = False,
    ):
        self.backlight_name = backlight_name
        self.backlight_dir = Path("/sys/class/backlight") / backlight_name
        self.enable_brightness = enable_brightness
        self.enable_color_temp = enable_color_temp
        self.dry_run = dry_run
        self._max_brightness = self._read_max_brightness()

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

    def ensure_night_light_active(self) -> None:
        if not self.enable_color_temp or self.dry_run:
            return
        schema = "org.gnome.settings-daemon.plugins.color"
        subprocess.run(["gsettings", "set", schema, "night-light-enabled", "true"], check=True)
        subprocess.run(["gsettings", "set", schema, "night-light-schedule-automatic", "false"], check=True)
        subprocess.run(["gsettings", "set", schema, "night-light-schedule-from", "0.0"], check=True)
        subprocess.run(["gsettings", "set", schema, "night-light-schedule-to", "24.0"], check=True)

    def set_color_temperature(self, cct: int) -> None:
        if not self.enable_color_temp or self.dry_run:
            return
        subprocess.run(
            [
                "gsettings",
                "set",
                "org.gnome.settings-daemon.plugins.color",
                "night-light-temperature",
                str(int(cct)),
            ],
            capture_output=True,
            check=True,
        )
