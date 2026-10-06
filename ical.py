"""Read deadlines from a Moodle calendar export URL (iCal feed). Works without the web service API."""
import re
import zlib
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import httpx
from icalendar import Calendar

from moodle import MoodleError

_OPENS = re.compile(r"\b(opens?|starts?)\s*$", re.I)


def validate_url(url: str, allowed_host: str | None) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise MoodleError("That doesn't look like a link. Paste the full calendar URL.")
    if "export_execute.php" not in u.path or "authtoken=" not in u.query:
        raise MoodleError("That is not a calendar export link. It should contain export_execute.php and authtoken.")
    if allowed_host and u.netloc.lower() != allowed_host.lower():
        raise MoodleError(f"Only {allowed_host} links are accepted.")
    return url.strip()


def userid_from_url(url: str) -> int | None:
    m = re.search(r"[?&]userid=(\d+)", url)
    return int(m.group(1)) if m else None


def _ts(value) -> int:
    dt = value.dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if isinstance(dt, date):  # all-day: treat as end of that day UTC
        return int(datetime(dt.year, dt.month, dt.day, 23, 59, tzinfo=timezone.utc).timestamp())
    return 0


def parse_ics(data: bytes, site: str = ""):
    cal = Calendar.from_ical(data)
    events = []
    for comp in cal.walk("VEVENT"):
        name = str(comp.get("SUMMARY", "Untitled")).strip()
        if _OPENS.search(name):
            continue  # "X opens" is not a deadline
        uid = str(comp.get("UID", ""))
        m = re.match(r"(\d+)@", uid)
        event_id = int(m.group(1)) if m else zlib.crc32(uid.encode())
        cats = comp.get("CATEGORIES")
        course = ""
        if cats is not None:
            try:
                course = ", ".join(str(c) for c in cats.cats)
            except AttributeError:
                course = str(cats)
        course = course or "Moodle"
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        url = str(comp.get("URL", "")) or (f"{site}/calendar/view.php?view=day&time={_ts(dtstart)}" if site else "")
        events.append(
            {
                "event_id": event_id,
                "name": name,
                "course_id": zlib.crc32(course.encode()),
                "course": course,
                "due_ts": _ts(dtstart),
                "url": url,
                "kind": "calendar",
            }
        )
    return events


async def fetch_events(url: str):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        r = await client.get(url)
    if r.status_code != 200 or b"BEGIN:VCALENDAR" not in r.content[:2000]:
        raise MoodleError("Could not read the calendar. The link may be wrong or expired; generate a new one.")
    p = urlparse(url)
    return parse_ics(r.content, f"{p.scheme}://{p.netloc}")
