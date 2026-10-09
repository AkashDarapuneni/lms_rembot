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
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
  background:linear-gradient(135deg,#1e3c72,#2a5298 45%,#7b4397);color:#fff;overflow:hidden}
.card{width:100%;max-width:380px;padding:28px 24px;border-radius:24px;background:rgba(255,255,255,.14);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border:1px solid rgba(255,255,255,.25);box-shadow:0 20px 50px rgba(0,0,0,.35);animation:rise .6s ease both;text-align:center;position:relative;z-index:2}
@keyframes rise{from{opacity:0;transform:translateY(24px) scale(.96)}to{opacity:1;transform:none}}
.logo{font-size:52px;animation:bob 2.4s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-8px) rotate(-4deg)}}
h2{margin:6px 0 4px;font-size:22px} p{margin:0 0 16px;font-size:14px;line-height:1.45;opacity:.85}
.site{display:inline-block;padding:3px 10px;border-radius:99px;background:rgba(255,255,255,.2);font-weight:600}
input{width:100%;padding:14px 16px;margin:6px 0;font-size:16px;color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:14px;background:rgba(255,255,255,.12);outline:none;transition:.2s}
input::placeholder{color:rgba(255,255,255,.65)} input:focus{border-color:#fff;background:rgba(255,255,255,.22)}
button{width:100%;padding:15px;margin-top:12px;font-size:17px;font-weight:700;color:#2a2a6a;border:0;border-radius:14px;cursor:pointer;
  background:linear-gradient(90deg,#ffe259,#ffa751);box-shadow:0 8px 20px rgba(255,167,81,.45);transition:transform .15s}
button:active{transform:scale(.97)} button:disabled{opacity:.6}
#msg{min-height:22px;margin-top:14px;font-size:14px}
.shake{animation:shake .4s} @keyframes shake{25%{transform:translateX(-8px)}75%{transform:translateX(8px)}}
.ok .logo{animation:pop .6s ease both} @keyframes pop{from{transform:scale(.3)}to{transform:scale(1)}}
canvas{position:fixed;inset:0;pointer-events:none;z-index:1}
.lock{font-size:12px;opacity:.7;margin-top:14px}
</style></head><body>
<canvas id="c"></canvas>
<div class="card" id="card">
  <div class="logo" id="logo">🎓</div>
  <h2 id="title">Connect your LMS</h2>
  <p id="sub">Sign in to <span class="site">__SITE__</span> once. I'll fetch your deadlines and remind you before each one.</p>
  <div id="form">
    <input id="u" placeholder="👤  Username or email" autocomplete="username">
    <input id="p" type="password" placeholder="🔒  Password" autocomplete="current-password">
    <button id="go">Connect 🚀</button>
  </div>
  <div id="msg"></div>
  <div class="lock">🔐 Your password is used once and never stored.</div>
</div>
<script>
const tg=window.Telegram.WebApp; tg.ready(); tg.expand();
const msg=document.getElementById('msg'), card=document.getElementById('card');
const cv=document.getElementById('c'), cx=cv.getContext('2d');
function confetti(){
  cv.width=innerWidth; cv.height=innerHeight;
  const cols=['#ffe259','#ffa751','#ff6b6b','#4ecdc4','#fff','#a29bfe'];
  const ps=Array.from({length:140},()=>({x:Math.random()*cv.width,y:-20-Math.random()*cv.height*.5,r:4+Math.random()*6,
    c:cols[Math.floor(Math.random()*cols.length)],vy:2+Math.random()*4,vx:-2+Math.random()*4,a:Math.random()*6}));
  let t=0; (function f(){cx.clearRect(0,0,cv.width,cv.height);
    ps.forEach(p=>{p.x+=p.vx;p.y+=p.vy;p.a+=.1;cx.save();cx.translate(p.x,p.y);cx.rotate(p.a);cx.fillStyle=p.c;cx.fillRect(-p.r,-p.r/2,p.r*2,p.r);cx.restore()});
    if(++t<220) requestAnimationFrame(f); else cx.clearRect(0,0,cv.width,cv.height)})();
}
document.getElementById('go').onclick=async()=>{
  const btn=document.getElementById('go'); btn.disabled=true; msg.textContent='⏳ Connecting...';
  try{
    const r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({initData:tg.initData,username:document.getElementById('u').value,password:document.getElementById('p').value})});
    const d=await r.json();
    if(d.ok){
      card.classList.add('ok'); document.getElementById('form').style.display='none';
      document.getElementById('logo').textContent='🎉'; document.getElementById('title').textContent="You're in!";
      document.getElementById('sub').textContent='Connected! Check your chat, your reminders are ON.'; msg.textContent='';
      confetti(); try{tg.HapticFeedback.notificationOccurred('success')}catch(e){}
      setTimeout(()=>tg.close(),2600);
    } else {
      msg.textContent='😕 '+(d.error||'Failed.'); btn.disabled=false;
      card.classList.remove('shake'); void card.offsetWidth; card.classList.add('shake');
      try{tg.HapticFeedback.notificationOccurred('error')}catch(e){}
    }
  }catch(e){msg.textContent='😕 Network error. Try again.'; btn.disabled=false;}
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
