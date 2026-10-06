"""Thin async client for the Moodle web service API."""
import time

import httpx


class MoodleError(Exception):
    pass


def normalize_site(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


async def request_token(site: str, username: str, password: str) -> str:
    """Exchange username/password for a mobile-app web service token."""
    site = normalize_site(site)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{site}/login/token.php",
            data={"username": username, "password": password, "service": "moodle_mobile_app"},
        )
    try:
        data = r.json()
    except ValueError:
        raise MoodleError("The site did not return a valid response. Check the URL.")
    if "token" not in data:
        raise MoodleError(data.get("error", "Login failed"))
    return data["token"]


async def call(site: str, token: str, function: str, **params):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{normalize_site(site)}/webservice/rest/server.php",
            data={"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params},
        )
    try:
        data = r.json()
    except ValueError:
        raise MoodleError("Invalid response from Moodle")
    if isinstance(data, dict) and "exception" in data:
        raise MoodleError(data.get("message") or data["exception"])
    return data


async def site_info(site: str, token: str) -> dict:
    return await call(site, token, "core_webservice_get_site_info")


async def fetch_events(site: str, token: str, days_back: int = 14, days_ahead: int = 120):
    """Return actionable (not yet completed) calendar events, e.g. assignments and quizzes."""
    now = int(time.time())
    data = await call(
        site,
        token,
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=now - days_back * 86400,
        timesortto=now + days_ahead * 86400,
        limitnum=100,
    )
    events = []
    for e in data.get("events", []):
        action = e.get("action") or {}
        if not action.get("actionable", False):
            continue  # already submitted / completed
        course = e.get("course") or {}
        events.append(
            {
                "event_id": int(e["id"]),
                "name": e.get("name", "Untitled"),
                "course_id": int(course.get("id", 0)),
                "course": course.get("fullname", "Unknown course"),
                "due_ts": int(e.get("timesort") or 0),
                "url": e.get("url") or "",
                "kind": e.get("modulename") or "task",
            }
        )
    return events
