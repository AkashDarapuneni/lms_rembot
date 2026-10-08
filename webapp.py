"""Telegram Mini App: a login page opened from the bot. Verifies the Telegram user, then fetches their calendar URL."""
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

import mini_login
from moodle import MoodleError

log = logging.getLogger("webapp")


def verify_init_data(init_data: str, bot_token: str, max_age: int = 3600) -> dict:
    """Validate Telegram WebApp initData (HMAC) and return the Telegram user dict."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(calc, got):
        raise ValueError("bad signature")
    if time.time() - int(pairs.get("auth_date", "0")) > max_age:
        raise ValueError("expired")
    return json.loads(pairs["user"])


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:20px;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111)}
h2{margin:0 0 6px} p{opacity:.75;font-size:14px;line-height:1.4}
input{width:100%;box-sizing:border-box;padding:12px;margin:6px 0;font-size:16px;border:1px solid #8884;border-radius:8px;background:var(--tg-theme-secondary-bg-color,#f4f4f4);color:inherit}
button{width:100%;padding:13px;margin-top:10px;font-size:16px;border:0;border-radius:8px;background:var(--tg-theme-button-color,#2481cc);color:var(--tg-theme-button-text-color,#fff)}
#msg{margin-top:12px;font-size:14px}
</style></head><body>
<h2>Connect __SITE__</h2>
<p>Sign in with your LMS account. Your password is used once to fetch your calendar link and is never stored.</p>
<input id="u" placeholder="Username or email" autocomplete="username">
<input id="p" type="password" placeholder="Password" autocomplete="current-password">
<button id="go">Connect</button>
<div id="msg"></div>
<script>
const tg=window.Telegram.WebApp; tg.ready(); tg.expand();
const msg=document.getElementById('msg');
document.getElementById('go').onclick=async()=>{
  const btn=document.getElementById('go'); btn.disabled=true; msg.textContent='Connecting...';
  try{
    const r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({initData:tg.initData,username:document.getElementById('u').value,password:document.getElementById('p').value})});
    const d=await r.json();
    if(d.ok){msg.textContent='Connected! Check the chat.'; setTimeout(()=>tg.close(),1200);}
    else{msg.textContent=d.error||'Failed.'; btn.disabled=false;}
  }catch(e){msg.textContent='Network error. Try again.'; btn.disabled=false;}
};
</script></body></html>"""


def create_app(bot_token: str, on_connect, site: str) -> web.Application:
    """on_connect(chat_id, calendar_url) is awaited once the link is obtained."""
    attempts: dict[int, list[float]] = {}

    async def page(request):
        return web.Response(
            text=PAGE.replace("__SITE__", site),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def connect(request):
        try:
            body = await request.json()
            user = verify_init_data(body["initData"], bot_token)
            username, password = str(body["username"]).strip(), str(body["password"])
            if not username or not password:
                raise ValueError("empty")
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid request. Open this page from the bot."}, status=400)
        chat_id = int(user["id"])
        now = time.time()
        recent = [t for t in attempts.get(chat_id, []) if now - t < 600]
        if len(recent) >= 5:
            return web.json_response({"ok": False, "error": "Too many attempts. Wait 10 minutes."}, status=429)
        attempts[chat_id] = recent + [now]
        try:
            url = await mini_login.get_calendar_url(site, username, password)
        except MoodleError as ex:
            return web.json_response({"ok": False, "error": str(ex)}, status=401)
        except Exception:
            log.exception("calendar fetch failed")  # never log the request body
            return web.json_response({"ok": False, "error": "Could not reach the LMS. Try again later."}, status=502)
        finally:
            password = None
        try:
            await on_connect(chat_id, url)
        except Exception:
            log.exception("on_connect failed")
            return web.json_response({"ok": False, "error": "Saved login failed. Try /calendar instead."}, status=500)
        return web.json_response({"ok": True})

    async def health(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    app.router.add_get("/connect", page)
    app.router.add_post("/api/connect", connect)
    return app
