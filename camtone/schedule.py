"""Fixed night schedule (v0.1.9): pure time helpers, ported from gnome-adaptive-tone's extension.js.

Times are minutes after local midnight. A window with from == to is empty (schedule off);
from > to wraps past midnight (e.g. 22:00 -> 07:00).
"""

from __future__ import annotations

import time
from typing import Optional

DAY_SEC = 86400


def in_night_schedule(enabled: bool, from_min: int, to_min: int, cur_min: int) -> bool:
    if not enabled or from_min == to_min:
        return False
    if from_min < to_min:
        return from_min <= cur_min < to_min
    return cur_min >= from_min or cur_min < to_min


def seconds_until_boundary(
    enabled: bool, from_min: int, to_min: int, cur_sec: int
) -> Optional[int]:
    """Seconds until the next start/end boundary (+1 s so the tick lands past it), or None when off."""
    if not enabled or from_min == to_min:
        return None
    inside = in_night_schedule(True, from_min, to_min, cur_sec // 60)
    delta = (to_min if inside else from_min) * 60 - cur_sec
    if delta <= 0:
        delta += DAY_SEC
    return delta + 1


def local_seconds_of_day() -> int:
    t = time.localtime()
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
