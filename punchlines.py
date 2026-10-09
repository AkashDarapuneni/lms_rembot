"""Reminder lines (dialogue + optional GIF) per reminder stage.

All lines live in the database table `line_pool`. Fixed lines come from punchlines.json (synced at startup); when
GEMINI_API_KEY is set, a background job keeps adding new Gemini-written lines. A line is used only if it was last
used more than LINE_REUSE_DAYS (default 3) ago, so no two reminders in a row (or in a few days) match.
GIF links must be direct .gif/.mp4 URLs; broken ones are skipped automatically.
"""
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path

import httpx

import gemini
from utils import parse_offsets

log = logging.getLogger("punchlines")
DATA_FILE = Path(__file__).with_name("punchlines.json")

REUSE_DAYS = float(os.getenv("LINE_REUSE_DAYS", "3"))
POOL_MIN_FREE = int(os.getenv("POOL_MIN_LINES", "30"))
POOL_MAX = int(os.getenv("POOL_MAX_LINES", "3000"))

_stages: dict[str, dict] = {}
_gif_bad: set[str] = set()
_db = None


def load(path: Path = DATA_FILE) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _stages.clear()
    for label, block in raw.items():
        secs = parse_offsets(label)[0]
        lines = [dict(l) for l in block.get("lines", []) if l.get("quote")]
        if lines:
            _stages[label] = {"seconds": secs, "emoji": block.get("emoji", "⏰"), "lines": lines}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _hash(text: str) -> str:
    return hashlib.sha1(_norm(text).encode()).hexdigest()[:20]


def _similar(a: str, b: str) -> bool:
    x, y = set(_norm(a).split()), set(_norm(b).split())
    return bool(x and y) and len(x & y) / len(x | y) >= 0.6


def init(db) -> None:
    """Attach the database and sync the fixed lines from punchlines.json into the pool."""
    global _db
    _db = db
    keep: dict[str, set] = {}
    for label, st in _stages.items():
        keep[label] = set()
        for l in st["lines"]:
            h = _hash(l["quote"])
            keep[label].add(h)
            db.pool_add(label, h, l["quote"], l.get("note", ""), l.get("gif", ""), "fixed", int(time.time()))
    for label in _stages:
        for h in db.pool_fixed_hashes(label) - keep[label]:
            db.pool_delete(label, h)


def stage_for(offset_s: int) -> str:
    """The configured stage closest to this reminder offset (e.g. 5400s -> '2h')."""
    return min(_stages, key=lambda k: abs(math.log(max(offset_s, 1)) - math.log(_stages[k]["seconds"])))


def emoji_for(offset_s: int) -> str:
    return _stages[stage_for(offset_s)]["emoji"]


async def build(offset_s: int, seconds_left: int | None = None, ai: bool = True) -> dict:
    """Pick the dialogue (and GIF) for a reminder sent `offset_s` seconds before the deadline."""
    label = stage_for(offset_s)
    st = _stages[label]
    now = int(time.time())
    cutoff = now - int(REUSE_DAYS * 86400)
    source = None if ai else "fixed"
    rows = _db.pool_free(label, cutoff, 200, source)
    if rows:
        row = random.choice(rows)
    else:
        row = _db.pool_oldest(label, source)
        log.warning("No unused line left for stage %s: reusing the oldest one. Add more lines or set GEMINI_API_KEY.", label)
    _db.pool_touch(label, row["h"], now)
    gif = row["gif"] or ""
    if not gif and row["source"] != "fixed":
        gifs = [l["gif"] for l in st["lines"] if l.get("gif") and l["gif"] not in _gif_bad]
        gif = random.choice(gifs) if gifs else ""
    if gif in _gif_bad:
        gif = ""
    return {"emoji": st["emoji"], "quote": row["quote"], "note": row["note"] or "", "gif": gif, "stage": label}


async def refill_once() -> int:
    """If a stage is running low on unused lines, ask Gemini for a new batch. Returns lines added."""
    if not gemini.enabled() or _db is None:
        return 0
    cutoff = int(time.time()) - int(REUSE_DAYS * 86400)
    counts = _db.pool_counts(cutoff)
    label = min(_stages, key=lambda k: counts.get(k, (0, 0))[0])
    free, total = counts.get(label, (0, 0))
    if free >= POOL_MIN_FREE or total >= POOL_MAX:
        return 0
    existing = _db.pool_quotes(label)
    batch = await gemini.generate_batch(label, _stages[label]["seconds"], [l["quote"] for l in _stages[label]["lines"]])
    added = 0
    for item in batch:
        q = item["quote"]
        if any(_similar(q, old) for old in existing):
            continue
        _db.pool_add(label, _hash(q), q, item["note"], "", "gemini", int(time.time()))
        existing.append(q)
        added += 1
    if added:
        log.info("Added %s Gemini lines to stage %s (free before: %s)", added, label, free)
    return added


def mark_gif_bad(url: str) -> None:
    _gif_bad.add(url)


async def verify_gifs(transport=None) -> None:
    """Check every GIF link once at startup; links that don't work are skipped (text only) and logged."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, transport=transport) as c:
        for label, st in _stages.items():
            for l in st["lines"]:
                url = l.get("gif")
                if not url:
                    continue
                try:
                    async with c.stream("GET", url) as r:
                        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith(("image/", "video/"))
                except Exception:
                    ok = False
                if not ok:
                    _gif_bad.add(url)
                    log.warning("GIF link not usable, skipping it: [%s] %s", label, url)


load()
