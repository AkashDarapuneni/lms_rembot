"""Optional: ask Google's Gemini API to write a fresh reminder line in the same style.

Turned on only when GEMINI_API_KEY is set. Any failure (no key, quota, timeout, bad answer) returns None and the
bot uses the fixed lines from punchlines.json instead, so reminders are never blocked by Gemini.
"""
import json
import logging
import os
import time

import httpx

log = logging.getLogger("gemini")

_TRANSPORT = None  # tests inject an httpx transport here
_cache: dict[str, tuple[float, dict | None]] = {}

PROMPT = """You write one short reminder for a college student whose LMS assignment deadline is close.
Style: Telugu written in English letters, mixed with a little English, in the over-the-top "mass hero dialogue" style of Telugu cinema. Funny and motivating.
Time left: {label}. Mood: {mood}.
Rules: write an ORIGINAL line, do not copy a real film dialogue word for word. No insults, no swearing, nothing about religion, caste or politics, no real people's names.
Style examples (do not repeat them): {examples}
Reply as JSON with two fields: "quote" (the punchy line, at most 14 words) and "note" (a plain English nudge to submit, at most 12 words)."""


def enabled() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _mood(seconds: int) -> str:
    if seconds >= 12 * 3600:
        return "calm but firm, plenty of time"
    if seconds >= 2 * 3600:
        return "getting serious, time to start"
    if seconds >= 3000:
        return "urgent, almost out of time"
    return "last-minute panic, submit right now"


async def fresh_line(label: str, seconds: int, examples: list[str]) -> dict | None:
    ttl = int(os.getenv("GEMINI_CACHE_MINUTES", "30")) * 60
    hit = _cache.get(label)
    if hit and time.time() - hit[0] < (ttl if hit[1] else 300):
        return hit[1]
    result = await _call(label, seconds, examples)
    _cache[label] = (time.time(), result)
    return result


async def _call(label: str, seconds: int, examples: list[str]) -> dict | None:
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gen = {"temperature": 1.0, "maxOutputTokens": 400, "responseMimeType": "application/json"}
    if "2.5" in model and "flash" in model:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(label=label, mood=_mood(seconds), examples=" | ".join(examples[:3]))}]}],
        "generationConfig": gen,
    }
    try:
        async with httpx.AsyncClient(timeout=8, transport=_TRANSPORT) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                json=body,
            )
        if r.status_code != 200:
            log.warning("Gemini returned HTTP %s, using fixed lines", r.status_code)
            return None
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        quote, note = str(data["quote"]).strip().strip('"'), str(data.get("note", "")).strip()
        if not (3 <= len(quote) <= 160) or len(note) > 120:
            return None
        return {"quote": quote, "note": note}
    except Exception as ex:
        log.warning("Gemini failed (%s), using fixed lines", type(ex).__name__)
        return None
