"""Log in to Moodle once with the user's credentials and obtain their calendar export URL.

The password is only used inside this call and is never stored or logged.
Two methods are tried: the Moodle web service API, then the normal website login.
"""
import html
import re

import httpx

import moodle
from moodle import MoodleError, normalize_site

WRONG_LOGIN = "Wrong username or password."


def build_url(site: str, userid: int | str, authtoken: str) -> str:
    return (
        f"{normalize_site(site)}/calendar/export_execute.php"
        f"?userid={userid}&authtoken={authtoken}&preset_what=all&preset_time=recentupcoming"
    )


async def _via_api(site: str, username: str, password: str) -> str:
    token = await moodle.request_token(site, username, password)
    info = await moodle.site_info(site, token)
    res = await moodle.call(site, token, "core_calendar_get_calendar_export_token")
    authtoken = res.get("token") if isinstance(res, dict) else None
    if not authtoken:
        raise MoodleError("Calendar token not available through the API.")
    return build_url(site, info["userid"], authtoken)


_URL_RE = re.compile(r"https?://[^\s\"'<>]+export_execute\.php\?[^\s\"'<>]+")


def extract_calendar_url(page_html: str) -> str | None:
    m = _URL_RE.search(page_html)
    return html.unescape(m.group(0)) if m else None


async def _via_web(site: str, username: str, password: str) -> str:
    site = normalize_site(site)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(f"{site}/login/index.php")
        m = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
        data = {"username": username, "password": password, "anchor": ""}
        if m:
            data["logintoken"] = m.group(1)
        r = await c.post(f"{site}/login/index.php", data=data)
        if "/login/index.php" in str(r.url) or 'id="loginerrormessage"' in r.text:
            raise MoodleError(WRONG_LOGIN)
        r = await c.get(f"{site}/calendar/export.php")
        s = re.search(r'"sesskey":"([^"]+)"', r.text) or re.search(r"sesskey=([A-Za-z0-9]+)", r.text)
        if not s:
            raise MoodleError("Could not read the calendar page after login.")
        r = await c.post(
            f"{site}/calendar/export.php",
            data={
                "_qf__core_calendar_export_form": "1",
                "sesskey": s.group(1),
                "events[exportevents]": "all",
                "period[timeperiod]": "recentupcoming",
                "generateurl": "Get calendar URL",
            },
        )
        url = extract_calendar_url(r.text)
        if not url:
            raise MoodleError("Logged in, but could not find the calendar link.")
        return url


async def get_calendar_url(site: str, username: str, password: str) -> str:
    try:
        return await _via_api(site, username, password)
    except MoodleError as ex:
        if "invalid" in str(ex).lower() and "login" in str(ex).lower():
            raise MoodleError(WRONG_LOGIN)  # don't retry: avoids account lockout
    except (httpx.HTTPError, KeyError):
        pass
    return await _via_web(site, username, password)
