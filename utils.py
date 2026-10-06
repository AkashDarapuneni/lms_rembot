"""Pure helpers (easy to test)."""
import re
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_offsets(text: str) -> list[int]:
    """'3d,1d,6h,30m' -> [259200, 86400, 21600, 1800] (descending, unique)."""
    out = set()
    for part in re.split(r"[,\s]+", text.strip().lower()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)([mhdw])", part)
        if not m:
            raise ValueError(f"Can't read '{part}'. Use values like 3d, 12h, 30m.")
        secs = int(m.group(1)) * _UNITS[m.group(2)]
        if secs <= 0:
            raise ValueError("Offsets must be greater than zero.")
        out.add(secs)
    if not out:
        raise ValueError("Give at least one reminder, e.g. 1d,3h.")
    return sorted(out, reverse=True)


def fmt_offset(secs: int) -> str:
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def fmt_delta(secs: int) -> str:
    neg = secs < 0
    secs = abs(int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m and not d:
        parts.append(f"{m}m")
    text = " ".join(parts) or "less than a minute"
    return f"{text} ago" if neg else f"in {text}"


def fmt_due(ts: int, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%a %d %b, %I:%M %p")


def parse_hhmm(text: str) -> str:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text.strip())
    if not m:
        raise ValueError("Use 24-hour HH:MM, e.g. 08:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def in_quiet_hours(now_ts: int, tz: str, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    now = datetime.fromtimestamp(now_ts, ZoneInfo(tz)).time()
    s = dtime(*map(int, start.split(":")))
    e = dtime(*map(int, end.split(":")))
    if s <= e:
        return s <= now < e
    return now >= s or now < e  # window crosses midnight


def due_reminder_offsets(now_ts: int, due_ts: int, offsets: list[int], already_sent: set[int]):
    """Offsets whose trigger time has passed, not yet sent. Returns (to_send_offset or None, all_passed)."""
    if due_ts <= now_ts:
        return None, []
    passed = [o for o in offsets if now_ts >= due_ts - o]
    unsent = [o for o in passed if o not in already_sent]
    if not unsent:
        return None, passed
    # Send only the most urgent one (smallest offset) to avoid spam after downtime / late sync.
    return min(unsent), passed
