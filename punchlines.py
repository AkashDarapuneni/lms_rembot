"""Reminder lines (dialogue + optional GIF) per reminder stage, from punchlines.json.

Edit punchlines.json to change the lines or GIFs. Each stage key (1d, 6h, 2h, 1h, 50m, 10m ...) holds a list of
{"quote": ..., "note": ..., "gif": ...}. A GIF must be a direct link to a .gif or .mp4 file.
If GEMINI_API_KEY is set, Gemini writes a fresh quote in the same style and the fixed lines are the fallback.
"""
import json
import logging
import math
import random
from pathlib import Path

import httpx

import gemini
from utils import parse_offsets

log = logging.getLogger("punchlines")
DATA_FILE = Path(__file__).with_name("punchlines.json")

_stages: dict[str, dict] = {}
_last: dict[str, int] = {}


def load(path: Path = DATA_FILE) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _stages.clear()
    for label, block in raw.items():
        secs = parse_offsets(label)[0]
        lines = [dict(l) for l in block.get("lines", []) if l.get("quote")]
        if lines:
            _stages[label] = {"seconds": secs, "emoji": block.get("emoji", "⏰"), "lines": lines}


def stage_for(offset_s: int) -> str:
    """The configured stage closest to this reminder offset (e.g. 5400s -> '2h')."""
    return min(_stages, key=lambda k: abs(math.log(max(offset_s, 1)) - math.log(_stages[k]["seconds"])))


async def build(offset_s: int, seconds_left: int | None = None) -> dict:
    """Pick the dialogue (and GIF) for a reminder sent `offset_s` seconds before the deadline."""
    label = stage_for(offset_s)
    stage = _stages[label]
    lines = stage["lines"]
    idx = random.choice([i for i in range(len(lines)) if i != _last.get(label)] or [0])
    _last[label] = idx
    item = lines[idx]
    quote, note = item["quote"], item.get("note", "")
    if gemini.enabled():
        fresh = await gemini.fresh_line(label, stage["seconds"], [l["quote"] for l in lines])
        if fresh:
            quote, note = fresh["quote"], fresh["note"]
    gif = item.get("gif") or ""
    if item.get("gif_ok") is False:
        gif = ""
    return {"emoji": stage["emoji"], "quote": quote, "note": note, "gif": gif}


def mark_gif_bad(url: str) -> None:
    for stage in _stages.values():
        for l in stage["lines"]:
            if l.get("gif") == url:
                l["gif_ok"] = False


async def verify_gifs(transport=None) -> None:
    """Check every GIF link once at startup; links that don't work are skipped (text only) and logged."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, transport=transport) as c:
        for label, stage in _stages.items():
            for l in stage["lines"]:
                url = l.get("gif")
                if not url:
                    continue
                ok = False
                try:
                    async with c.stream("GET", url) as r:
                        ctype = r.headers.get("content-type", "")
                        ok = r.status_code == 200 and ctype.startswith(("image/", "video/"))
                except Exception:
                    ok = False
                l["gif_ok"] = ok
                if not ok:
                    log.warning("GIF link not usable, sending text only for it: [%s] %s", label, url)


load()
