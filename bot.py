"""Moodle assignment reminder bot for Telegram."""
import asyncio
import html
import io
import json
import logging
import os
import random
import secrets
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import gemini
import ical
import moodle
import punchlines
import webapp
from aiohttp import web
from db import DB, DEFAULT_OFFSETS
from utils import (
    due_reminder_offsets,
    fmt_delta,
    fmt_due,
    fmt_offset,
    in_quiet_hours,
    parse_hhmm,
    parse_offsets,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)  # their INFO lines print bot tokens and calendar links
log = logging.getLogger("moodlebot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
fernet = Fernet(os.environ["SECRET_KEY"].encode())
db = DB(os.getenv("DATABASE_URL") or os.getenv("DB_PATH", "moodle_bot.sqlite3"))
SYNC_MINUTES = int(os.getenv("SYNC_MINUTES", "15"))
DEFAULT_SITE = os.getenv("DEFAULT_SITE", "lms.kluniversity.in")
ALLOW_ANY_SITE = os.getenv("ALLOW_ANY_SITE", "0") == "1"
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")  # public HTTPS URL of this server
PORT = int(os.getenv("PORT", "8080"))
_admin_items = [x.strip().lstrip("@") for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
ADMIN_IDS = {int(x) for x in _admin_items if x.isdigit()}
ADMIN_USERNAMES = {x.lower() for x in _admin_items if not x.isdigit()}  # a Telegram @username also works
GROUP_URL = os.getenv("GROUP_INVITE_URL", "").strip()  # invite link of your students' Telegram group
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
SHARE_DEADLINES = os.getenv("GEMINI_SHARE_DEADLINES", "1") != "0"
punchlines.init(db)
ADMIN_CODE = secrets.token_hex(4)  # one-time claim code, printed in the Render log at every start


def admin_ids() -> set:
    try:
        return ADMIN_IDS | db.admin_ids()
    except Exception:
        return set(ADMIN_IDS)


def is_admin(user) -> bool:
    """`user` is a Telegram user object or a numeric id. A listed @username is turned into an id on first use."""
    uid = getattr(user, "id", user)
    if uid in admin_ids():
        return True
    name = (getattr(user, "username", None) or "").lower()
    if name and name in ADMIN_USERNAMES:
        db.add_admin(uid)
        return True
    return False

def esc(text) -> str:
    """Escape for Telegram HTML (only & < > matter; quotes and apostrophes are fine as they are)."""
    return html.escape(str(text), quote=False)

HELP = (
    "<b>What this bot does</b>\n"
    "It reads your deadlines from LMS and pings you before each one, so you never miss a submission.\n\n"
    "<b>Buttons under the chat box</b>\n"
    "📅 Today / 📆 This week / ⏳ Upcoming / ⚠️ Overdue: your task lists\n"
    "⚙️ Settings: tap the ON/OFF switches\n"
    "🏆 My stats: your points, level and streak (/leaderboard shows friends)\n"
    "🎲 Fun: a random meme, GIF or dialogue\n\n"
    "<b>What each setting means</b>\n"
    "🎬 <b>Mass mode</b>: a Telugu hero dialogue comes with every reminder\n"
    "🖼 <b>Memes and GIFs</b>: a funny GIF, sticker or animation comes with reminders\n"
    "🤖 <b>AI dialogues</b>: Gemini AI writes extra new dialogues so you rarely see the same one\n"
    "📊 <b>Weekly summary</b>: every Sunday 6 PM, your week ahead\n"
    "🏅 <b>Leaderboard</b>: show your points to friends who joined\n"
    "🌐 <b>Language</b>: switch buttons and headings to Telugu style\n"
    "🌅 <b>Daily digest</b>: one summary message every morning (08:00)\n"
    "🌙 <b>Quiet hours</b>: no reminders at night (11 PM to 7 AM), except in the last hour\n"
    "🔔 <b>Reminder times</b>: by default 24h, 6h, 2h, 1h, 50m and 10m before each deadline\n\n"
    "<b>Ask me anything</b>\n"
    "Just type a question like \"what is due tomorrow?\" or \"how do I plan my week?\". AI answers.\n\n"
    "<b>Handy extras</b>\n"
    "🧪 Test (inside Settings): preview how a reminder looks\n"
    "/mute &lt;course&gt; and /unmute: silence a course\n"
    "/timezone Asia/Kolkata\n"
    "/logout: delete all your data"
)

WELCOME_NEW = (
    "👋 <b>Welcome!</b>\n\n"
    f"Tap the button below to connect <b>{DEFAULT_SITE}</b>. Sign in once and I'll remind you before every deadline."
)

LEVELS = [(0, "Beginner 🐣"), (50, "Hero 🦸"), (150, "Mass Hero 🔥"), (400, "Power Star ⚡"), (1000, "Pan-India Star 🌟")]
DICE = ["🎯", "🎲", "🏀", "⚽", "🎳", "🎰"]
FUN_LINES = [
    "Assignment cheyyaka pothe, memes kuda nee kosam raavu 😎",
    "Break aipoindi ra, ippudu pani 💪",
    "Thagganu le... kaani submit cheyyakunda thagganu 🔥",
    "Nuvvu hero ra, kaani deadline villain 🎬",
]


# Telegram full-screen message effects (private chats only)
FX_PARTY, FX_SAD, FX_FIRE = "5046509860389126442", "5104858069142078462", "5104841245755180586"
PRESETS = [
    ("Default", "1d,6h,2h,1h,50m,10m", "24h, 6h, 2h, 1h, 50m, 10m"),
    ("Relaxed", "1d,3h,1h", "24h, 3h, 1h"),
    ("Intense", "2d,1d,12h,6h,3h,2h,1h,30m,10m,5m", "2d to 5m, 10 reminders"),
    ("Last minute", "2h,30m,10m", "2h, 30m, 10m"),
]


async def send_fx(bot, chat_id, text, effect=None, **kw):
    """Send a message with a full-screen celebration effect (🎉 / 👎 / 🔥); plain message if effects are unavailable."""
    if effect:
        try:
            return await bot.send_message(chat_id, text, message_effect_id=effect, **kw)
        except Exception:
            pass
    return await bot.send_message(chat_id, text, **kw)


TR = {
    "en": {"hdr": "Reminder ({o} before): ", "snooze_over": "Snooze over: ", "done": "Submitted", "snz": "Snooze 1h", "tom": "Tomorrow",
           "mute": "Mute course", "open": "Open in Moodle", "task_done": "Task done!", "week": "Your week ahead"},
    "te": {"hdr": "Gurthu chesko ({o} mundu): ", "snooze_over": "Snooze aipoyindi: ", "done": "Submit chesa", "snz": "1h tarvata", "tom": "Repu",
           "mute": "Course mute", "open": "Moodle lo chudu", "task_done": "Pani aipoyindi!", "week": "Ee vaaram plan"},
}


def tr(lang, key, **kw):
    return TR.get(lang or "en", TR["en"])[key].format(**kw)


def share_url(uid=None) -> str:
    if not BOT_USERNAME:
        return ""
    from urllib.parse import quote
    link = "https://t.me/" + BOT_USERNAME + (f"?start=ref_{uid}" if uid else "")
    return (f"https://t.me/share/url?url={quote(link)}"
            f"&text={quote('Never miss an LMS deadline! This bot reminds you before every assignment 🔥')}")


def extras_markup(logout=False, uid=None):
    rows = []
    if share_url():
        rows.append([InlineKeyboardButton("📣 Invite friends (+20 pts each)" if uid else "📣 Share with friends", url=share_url(uid))])
    if GROUP_URL:
        rows.append([InlineKeyboardButton("👥 Join our community", url=GROUP_URL)])
    if logout:
        rows.append([InlineKeyboardButton("🚪 Logout", callback_data="lo:ask")])
    return InlineKeyboardMarkup(rows) if rows else None


def level_for(points: int) -> str:
    return [name for need, name in LEVELS if points >= need][-1]


# ---------- helpers ----------
def get_token(user) -> str:
    return fernet.decrypt(user["token_enc"].encode()).decode()


def event_text(e, tz: str, header: str = "") -> str:
    left = fmt_delta(e["due_ts"] - int(time.time()))
    head = f"<b>{esc(header.strip())}</b>\n" if header.strip() else ""
    return (
        f"{head}━━━━━━━━━━━━━━\n"
        f"📌 <b>{esc(e['name'])}</b>\n"
        f"📚 {esc(e['course'])}\n"
        f"⏰ {fmt_due(e['due_ts'], tz)}  •  <b>{left}</b>\n"
        "━━━━━━━━━━━━━━"
    )


def reminder_text(e, tz: str, header: str, style: dict | None) -> str:
    """Plain reminder, or dialogue first / details / English nudge when mass mode is on."""
    text = event_text(e, tz, header)
    if not style:
        return text
    out = f"{style['emoji']} <i>\"{esc(style['quote'])}\"</i>\n\n{text}"
    if style.get("note"):
        out += f"\n\n{esc(style['note'])}"
    return out


# Buttons shown permanently under the chat box (3 per row)
BTN_TODAY, BTN_WEEK, BTN_UPCOMING = "📅 Today", "📆 This week", "⏳ Upcoming"
BTN_OVERDUE, BTN_SETTINGS, BTN_HELP = "⚠️ Overdue", "⚙️ Settings", "❓ Help"
BTN_STATS, BTN_FUN, BTN_CONNECT, BTN_LOGOUT = "🏆 My stats", "🎲 Fun", "🔗 Connect", "🚪 Logout"
MENU_ROWS = [
    [BTN_TODAY, BTN_WEEK, BTN_UPCOMING],
    [BTN_OVERDUE, BTN_SETTINGS, BTN_HELP],
    [BTN_STATS, BTN_FUN, BTN_CONNECT],
    ["🧰 Tools", "🧠 Plan my week", "☑️ Tick done"],
]


def main_keyboard(linked: bool | None = None, chat_id: int | None = None) -> ReplyKeyboardMarkup:
    """Persistent buttons. Logged-in students see Logout in place of Connect."""
    if linked is None:
        linked = is_linked(chat_id) if chat_id else False
    rows = [list(r) for r in MENU_ROWS]
    rows[2][2] = BTN_LOGOUT if linked else BTN_CONNECT
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def event_keyboard(e, lang="en") -> InlineKeyboardMarkup:
    eid = e["event_id"]
    rows = [
        [
            InlineKeyboardButton("✅ " + tr(lang, "done"), callback_data=f"done:{eid}"),
            InlineKeyboardButton("😴 " + tr(lang, "snz"), callback_data=f"snz:{eid}:60"),
            InlineKeyboardButton("📅 " + tr(lang, "tom"), callback_data=f"snz:{eid}:1440"),
        ],
        [InlineKeyboardButton("🔕 " + tr(lang, "mute"), callback_data=f"mute:{e['course_id']}")],
    ]
    if e["url"]:
        rows.insert(0, [InlineKeyboardButton("🔗 " + tr(lang, "open"), url=e["url"])])
    return InlineKeyboardMarkup(rows)


async def require_login(update: Update):
    user = db.get_user(update.effective_chat.id)
    if not user or not user["token_enc"]:
        await update.effective_message.reply_text("You are not connected yet. Use /login or /token. See /help.")
        return None
    return user


# ---------- sync ----------
async def sync_user(app: Application, user, notify: bool = True) -> int:
    """Pull events from Moodle, store them, announce new / changed ones. Returns pending count."""
    chat_id = user["chat_id"]
    if user["source"] == "ics":
        events = await ical.fetch_events(get_token(user))
    else:
        events = await moodle.fetch_events(user["site_url"], get_token(user))
    fetched_ids = set()
    first_sync = user["last_sync"] == 0
    for e in events:
        fetched_ids.add(e["event_id"])
        old = db.get_event(chat_id, e["event_id"])
        db.upsert_event(chat_id, e)
        if old is None:
            if notify and not first_sync and not db.is_muted(chat_id, e["course_id"]):
                row = db.get_event(chat_id, e["event_id"])
                await app.bot.send_message(
                    chat_id,
                    event_text(row, user["tz"], "New: "),
                    parse_mode=ParseMode.HTML,
                    reply_markup=event_keyboard(row),
                )
        elif old["due_ts"] != e["due_ts"]:
            db.clear_sent(chat_id, e["event_id"])
            if notify and not db.is_muted(chat_id, e["course_id"]):
                row = db.get_event(chat_id, e["event_id"])
                await app.bot.send_message(
                    chat_id,
                    event_text(row, user["tz"], "Deadline changed: "),
                    parse_mode=ParseMode.HTML,
                    reply_markup=event_keyboard(row),
                )
    # API mode: no longer actionable means submitted. Calendar mode: a future event that vanished was deleted.
    horizon = int(time.time()) - (14 * 86400 if user["source"] != "ics" else 0)
    for row in db.pending_events(chat_id, since=horizon, include_muted=True):
        if row["event_id"] not in fetched_ids:
            db.mark_done(chat_id, row["event_id"])
    db.set_field(chat_id, "last_sync", int(time.time()))
    db.set_field(chat_id, "last_ok_sync", int(time.time()))
    if user["sync_fail"]:
        db.set_field(chat_id, "sync_fail", 0)
    return len(fetched_ids)


async def sync_job(context: ContextTypes.DEFAULT_TYPE):
    for user in db.all_users():
        try:
            await sync_user(context.application, user)
        except moodle.MoodleError as ex:
            log.warning("sync failed for %s: %s", user["chat_id"], ex)
            db.set_field(user["chat_id"], "sync_fail", (user["sync_fail"] or 0) + 1)
        except Exception:
            log.exception("sync crashed for %s", user["chat_id"])
            db.set_field(user["chat_id"], "sync_fail", (user["sync_fail"] or 0) + 1)


# ---------- reminders ----------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    for user in db.all_users():
        chat_id, tz = user["chat_id"], user["tz"]
        if (user["mute_until"] or 0) > now:
            continue  # "mute all today" is on
        user_offsets = parse_offsets(user["offsets"] or DEFAULT_OFFSETS)
        course_offs = db.course_offsets(chat_id)
        quiet = in_quiet_hours(now, tz, user["quiet_start"], user["quiet_end"])
        for e in db.pending_events(chat_id, since=now - 3600):
            offsets = parse_offsets(course_offs[e["course_id"]]) if e["course_id"] in course_offs else user_offsets
            # Skip events too far away to need a reminder yet (saves database queries)
            if not e["snooze_until"] and e["due_ts"] - now > offsets[0]:
                continue
            # snooze expiry
            if e["snooze_until"]:
                if now < e["snooze_until"]:
                    continue
                db.set_snooze(chat_id, e["event_id"], 0)
                if e["due_ts"] > now and not quiet:
                    left = e["due_ts"] - now
                    style = await punchlines.build(left, left, bool(user["ai_lines"])) if user["style"] else None
                    await send_reminder(context, chat_id, e, tz, tr(user["lang"], "snooze_over"), style, memes=bool(user["memes"]), lang=user["lang"])
                continue
            to_send, passed = due_reminder_offsets(
                now, e["due_ts"], offsets, db.sent_offsets(chat_id, e["event_id"])
            )
            if to_send is None:
                continue
            urgent = e["due_ts"] - now < 3600
            if quiet and not urgent:
                continue  # will fire once quiet hours end
            db.mark_sent(chat_id, e["event_id"], passed)
            style = await punchlines.build(to_send, e["due_ts"] - now, bool(user["ai_lines"])) if user["style"] else None
            await send_reminder(context, chat_id, e, tz, tr(user["lang"], "hdr", o=fmt_offset(to_send)), style, memes=bool(user["memes"]), lang=user["lang"])


async def send_reminder(context, chat_id, e, tz, header, style=None, keyboard=True, memes=False, lang="en"):
    text = reminder_text(e, tz, header, style)
    markup = event_keyboard(e, lang) if keyboard else None
    try:
        if style and style.get("gif"):
            try:
                await context.bot.send_animation(
                    chat_id, style["gif"], caption=text, parse_mode=ParseMode.HTML, reply_markup=markup
                )
                return
            except BadRequest as ex:  # link is not a usable GIF: remember that and send text instead
                log.warning("GIF rejected by Telegram (%s): %s", ex, style["gif"])
                punchlines.mark_gif_bad(style["gif"])
            except Exception:
                log.warning("GIF send failed, sending text instead")
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        log.exception("could not send reminder to %s", chat_id)
    finally:
        if memes and style:
            await send_meme(context, chat_id, style.get("stage"), only_if_lucky=bool(style.get("gif")))


async def send_meme(context, chat_id, stage=None, only_if_lucky=False) -> bool:
    """A random meme/GIF/sticker taught by the admin (matching the stage or 'any'), else a Telegram dice animation."""
    if only_if_lucky and random.random() > 0.35:
        return False  # reminder already had a GIF: only sometimes add another
    try:
        now = int(time.time())
        tags = [t for t in (stage, "any") if t]
        rows = db.media_free(tags, now - 86400)
        row = random.choice(rows) if rows else (db.media_oldest(tags) if random.random() < 0.7 else None)
        if row:
            db.media_touch(row["file_unique_id"], now)
            send = getattr(context.bot, {"animation": "send_animation", "sticker": "send_sticker", "photo": "send_photo"}[row["kind"]])
            await send(chat_id, row["file_id"])
            return True
        await context.bot.send_dice(chat_id, emoji=random.choice(DICE))
        return True
    except Exception:
        log.exception("meme send failed")
        return False


async def digest_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    for user in db.all_users():
        if not user["digest_time"]:
            continue
        local = datetime.fromtimestamp(now, ZoneInfo(user["tz"]))
        today = local.strftime("%Y-%m-%d")
        if local.strftime("%H:%M") != user["digest_time"] or user["last_digest"] == today:
            continue
        db.set_field(user["chat_id"], "last_digest", today)
        await send_list(context.bot, user, "Good morning. Your next 7 days:", days=7)


async def send_list(bot, user, title, days=None, overdue=False):
    now = int(time.time())
    chat_id = user["chat_id"]
    if overdue:
        rows = db.pending_events(chat_id, since=now - 14 * 86400, until=now)
    elif days is not None:
        end = datetime.fromtimestamp(now, ZoneInfo(user["tz"])).replace(hour=23, minute=59, second=59)
        until = int((end + timedelta(days=days - 1)).timestamp())
        rows = db.pending_events(chat_id, since=now, until=until)
    else:
        rows = db.pending_events(chat_id, since=now)[:15]
    if not rows:
        await bot.send_message(chat_id, f"{title}\nNothing here. Enjoy the free time.", reply_markup=main_keyboard(True))
        return
    lines = [f"<b>{esc(title)}</b>", ""]
    for i, e in enumerate(rows, 1):
        lines.append(
            f"{i}. <b>{esc(e['name'])}</b>\n   {esc(e['course'])}\n"
            f"   {fmt_due(e['due_ts'], user['tz'])} ({fmt_delta(e['due_ts'] - now)})"
        )
    await bot.send_message(
        chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )


# ---------- commands ----------
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_chat.id)
    if user and user["token_enc"]:
        await update.effective_message.reply_text(
            "✅ <b>You are already logged in.</b>\n\nDo you want to log out?",
            parse_mode=ParseMode.HTML, reply_markup=extras_markup(logout=True),
        )
        return
    if WEBAPP_URL:
        await update.effective_message.reply_text(
            f"Tap the button, sign in to {DEFAULT_SITE}, and I'll fetch your deadlines automatically.",
            reply_markup=connect_markup(),
        )
    else:
        await update.message.reply_text(
            f"Open https://{DEFAULT_SITE}/calendar/export.php, choose All events, click Get calendar URL, "
            "then send: /calendar <your link>"
        )


def connect_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔗 Connect {DEFAULT_SITE}", web_app=WebAppInfo(url=f"{WEBAPP_URL}/connect"))]]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    if context.args and context.args[0].startswith("ref_") and context.args[0][4:].isdigit() and not (user and user["token_enc"]):
        if not db.kv_get(f"ref:{chat_id}") and int(context.args[0][4:]) != chat_id:
            db.kv_set(f"ref:{chat_id}", context.args[0][4:])
    if user and user["token_enc"]:
        kb = extras_markup(logout=True, uid=chat_id)
        await update.message.reply_text("Menu buttons are ready 👇", reply_markup=main_keyboard(True))
        await update.message.reply_text(
            "✅ <b>You are already logged in.</b>\n\nDo you want to log out?",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        return
    name = esc(update.effective_user.first_name or "friend")
    hello = f"👋 <b>Hello {name}!</b> Welcome to the LMS Reminder Bot 🎓\n\nI remind you before every deadline, with some Telugu mass dialogues 🔥"
    await update.message.reply_text("🎬", reply_markup=main_keyboard(False))
    if not WEBAPP_URL:
        await update.message.reply_text(
            hello + f"\n\nOpen https://{DEFAULT_SITE}/calendar/export.php, choose All events, click Get calendar URL, "
            "then send: /calendar <your link>", parse_mode=ParseMode.HTML,
        )
        return
    kb = connect_markup()
    msg = await update.message.reply_text(hello + "\n\n👇", parse_mode=ParseMode.HTML, reply_markup=kb)
    for finger in ("👇", "☝️", "👇", "☝️", "👇"):  # pointing finger bounces at the Connect button
        await asyncio.sleep(0.45)
        try:
            await msg.edit_text(hello + f"\n\n{finger} <b>Tap Connect below to link your LMS</b> {finger}",
                                parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            break
    if extras_markup():
        await update.message.reply_text("📣 Know someone who forgets deadlines? Invite them!", reply_markup=extras_markup())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=main_keyboard(chat_id=update.effective_chat.id)
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buttons are on. Use them under the chat box.", reply_markup=main_keyboard(chat_id=update.effective_chat.id))


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a preview reminder right now, e.g. /test 1h."""
    label = (context.args[0] if context.args else "1h").lower()
    try:
        secs = parse_offsets(label)[0]
    except ValueError as ex:
        await update.message.reply_text(str(ex))
        return
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    tz = user["tz"] if user else "Asia/Kolkata"
    sample = {
        "event_id": -1, "course_id": 0, "name": "Sample assignment",
        "course": "Preview only, not a real task", "due_ts": int(time.time()) + secs, "url": "",
    }
    style = await punchlines.build(secs, secs) if (user["style"] if user else True) else None
    await send_reminder(context, chat_id, sample, tz, f"Reminder ({fmt_offset(secs)} before): ", style, keyboard=False)


async def notify_admins(bot, text):
    for aid in admin_ids():
        try:
            await bot.send_message(aid, text)
        except Exception:
            log.warning("could not message admin %s", aid)


async def link_calendar(app: Application, chat_id: int, url: str):
    """Called by the Mini App: store the calendar link with the Telegram id and run the first sync."""
    url = ical.validate_url(url, None if ALLOW_ANY_SITE else DEFAULT_SITE)
    site = "https://" + url.split("/")[2]
    db.save_login(chat_id, site, fernet.encrypt(url.encode()).decode(), "ics", ical.userid_from_url(url))
    count = await sync_user(app, db.get_user(chat_id), notify=False)
    await send_fx(
        app.bot, chat_id,
        f"🎉 <b>You're in! Welcome aboard!</b> 🥳\n\n"
        f"Connected to {DEFAULT_SITE}. I found <b>{count}</b> pending task(s) and reminders are ON.\n\n"
        "Tap <b>⚙️ Settings</b> to try a preview, and press <b>Submitted</b> on a reminder when you finish a task.",
        FX_PARTY, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True),
    )
    if extras_markup():
        await app.bot.send_message(chat_id, "📣 Invite your friends so they never miss a deadline either:", reply_markup=extras_markup())
    await notify_admins(app.bot, f"🆕 New user connected (Telegram ID {chat_id}). Total users: {db.count_users()}")
    await reward_referral(app.bot, chat_id)


async def _finish_login(update, context, site, token, source="api"):
    chat_id = update.effective_chat.id
    who = "your account"
    uid = ical.userid_from_url(token) if source == "ics" else None
    if source == "api":
        info = await moodle.site_info(site, token)
        who = info.get("fullname", who)
        uid = info.get("userid")
    db.save_login(chat_id, moodle.normalize_site(site), fernet.encrypt(token.encode()).decode(), source, uid)
    user = db.get_user(chat_id)
    count = await sync_user(context.application, user, notify=False)
    note = "" if source == "api" else "\nNote: calendar mode can't see submissions, so tap Submitted on a reminder when you finish."
    await send_fx(
        context.bot, chat_id,
        f"🎉 <b>You're in, {esc(who)}!</b> 🥳\n\nI found <b>{count}</b> pending task(s). Reminders: {esc(user['offsets'])}.{esc(note)}",
        FX_PARTY, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True),
    )
    if extras_markup():
        await context.bot.send_message(chat_id, "📣 Invite your friends so they never miss a deadline either:", reply_markup=extras_markup())
    await notify_admins(context.bot, f"🆕 New user connected (Telegram ID {chat_id}). Total users: {db.count_users()}")
    await reward_referral(context.bot, chat_id)


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            f"Usage: /calendar <link>\nGet the link at https://{DEFAULT_SITE}/calendar/export.php "
            "(All events, Get calendar URL)."
        )
        return
    try:
        await update.message.delete()  # the link contains a personal token
    except Exception:
        pass
    try:
        url = ical.validate_url(context.args[0], None if ALLOW_ANY_SITE else DEFAULT_SITE)
        site = "https://" + url.split("/")[2]
        await _finish_login(update, context, site, url, source="ics")
    except moodle.MoodleError as ex:
        await context.bot.send_message(update.effective_chat.id, f"Could not connect: {ex}")


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /login <username> <password>")
        return
    if len(context.args) >= 3 and "." in context.args[0]:  # explicit site given
        site, username, password = context.args[0], context.args[1], " ".join(context.args[2:])
    else:
        site, username, password = DEFAULT_SITE, context.args[0], " ".join(context.args[1:])
    if not ALLOW_ANY_SITE and moodle.normalize_site(site).split("//")[1] != DEFAULT_SITE:
        await update.message.reply_text(f"Only {DEFAULT_SITE} is supported.")
        return
    try:
        await update.message.delete()  # remove the password from the chat
    except Exception:
        pass
    try:
        token = await moodle.request_token(site, username, password)
        await _finish_login(update, context, site, token)
    except moodle.MoodleError as ex:
        await context.bot.send_message(update.effective_chat.id, f"Login failed: {ex}")


async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) not in (1, 2):
        await update.message.reply_text("Usage: /token <token>")
        return
    site, token = (DEFAULT_SITE, context.args[0]) if len(context.args) == 1 else (context.args[0], context.args[1])
    try:
        await update.message.delete()
    except Exception:
        pass
    if not ALLOW_ANY_SITE and moodle.normalize_site(site).split("//")[1] != DEFAULT_SITE:
        await context.bot.send_message(update.effective_chat.id, f"Only {DEFAULT_SITE} is supported.")
        return
    try:
        await _finish_login(update, context, site, token)
    except moodle.MoodleError as ex:
        await context.bot.send_message(update.effective_chat.id, f"Token rejected: {ex}")


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, log out", callback_data="lo:yes"),
        InlineKeyboardButton("↩️ Cancel", callback_data="lo:no"),
    ]])
    await update.message.reply_text("Log out and delete all your data from this bot?", reply_markup=kb)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await require_login(update)
    if not user:
        return
    try:
        count = await sync_user(context.application, user)
        await update.message.reply_text(f"Synced. {count} pending task(s).")
    except moodle.MoodleError as ex:
        await update.message.reply_text(f"Sync failed: {ex}")


async def cmd_today(update, context):
    user = await require_login(update)
    if user:
        await send_list(context.bot, user, "Due today", days=1)


async def cmd_week(update, context):
    user = await require_login(update)
    if user:
        await send_list(context.bot, user, "Due in the next 7 days", days=7)


async def cmd_upcoming(update, context):
    user = await require_login(update)
    if user:
        await send_list(context.bot, user, "Upcoming deadlines")


async def cmd_overdue(update, context):
    user = await require_login(update)
    if user:
        await send_list(context.bot, user, "Overdue (last 14 days)", overdue=True)


async def cmd_reminders(update, context):
    user = await require_login(update)
    if not user:
        return
    if not context.args:
        await update.message.reply_text(
            f"Current reminders: {user['offsets']}\nChange: /reminders 3d,1d,6h,1h (units: m, h, d, w)"
        )
        return
    try:
        offs = parse_offsets(" ".join(context.args))
    except ValueError as ex:
        await update.message.reply_text(str(ex))
        return
    db.set_field(user["chat_id"], "offsets", ",".join(fmt_offset(o) for o in offs))
    await update.message.reply_text("Saved: " + ", ".join(fmt_offset(o) for o in offs) + " before each deadline.")


async def cmd_digest(update, context):
    user = await require_login(update)
    if not user:
        return
    if not context.args:
        await update.message.reply_text(f"Daily digest: {user['digest_time'] or 'off'}. Use /digest 08:00 or /digest off")
        return
    if context.args[0].lower() == "off":
        db.set_field(user["chat_id"], "digest_time", None)
        await update.message.reply_text("Daily digest turned off.")
        return
    try:
        t = parse_hhmm(context.args[0])
    except ValueError as ex:
        await update.message.reply_text(str(ex))
        return
    db.set_field(user["chat_id"], "digest_time", t)
    await update.message.reply_text(f"Daily digest at {t} ({user['tz']}).")


async def cmd_quiet(update, context):
    user = await require_login(update)
    if not user:
        return
    if context.args and context.args[0].lower() == "off":
        db.set_field(user["chat_id"], "quiet_start", None)
        db.set_field(user["chat_id"], "quiet_end", None)
        await update.message.reply_text("Quiet hours off.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /quiet 23:00 07:00  or  /quiet off")
        return
    try:
        s, e = parse_hhmm(context.args[0]), parse_hhmm(context.args[1])
    except ValueError as ex:
        await update.message.reply_text(str(ex))
        return
    db.set_field(user["chat_id"], "quiet_start", s)
    db.set_field(user["chat_id"], "quiet_end", e)
    await update.message.reply_text(
        f"Quiet hours {s} to {e}. Reminders wait until the window ends, except those due within the hour."
    )


async def cmd_timezone(update, context):
    user = await require_login(update)
    if not user:
        return
    if not context.args:
        await update.message.reply_text(f"Timezone: {user['tz']}. Example: /timezone Asia/Kolkata")
        return
    try:
        ZoneInfo(context.args[0])
    except (ZoneInfoNotFoundError, ValueError):
        await update.message.reply_text("Unknown timezone. Use an IANA name like Asia/Kolkata or Europe/London.")
        return
    db.set_field(user["chat_id"], "tz", context.args[0])
    await update.message.reply_text(f"Timezone set to {context.args[0]}.")


async def cmd_mute(update, context):
    user = await require_login(update)
    if not user:
        return
    needle = " ".join(context.args).lower()
    if not needle:
        muted = db.muted_courses(user["chat_id"])
        text = "Muted: " + ", ".join(m["course"] for m in muted) if muted else "No muted courses. Use /mute <part of course name>"
        await update.message.reply_text(text)
        return
    matches = {e["course_id"]: e["course"] for e in db.pending_events(user["chat_id"], include_muted=True) if needle in e["course"].lower()}
    if not matches:
        await update.message.reply_text("No course matches that text.")
        return
    for cid, name in matches.items():
        db.mute_course(user["chat_id"], cid, name)
    await update.message.reply_text("Muted: " + ", ".join(matches.values()))


async def cmd_unmute(update, context):
    user = await require_login(update)
    if not user:
        return
    needle = " ".join(context.args).lower()
    removed = [m["course"] for m in db.muted_courses(user["chat_id"]) if not needle or needle in m["course"].lower()]
    for m in db.muted_courses(user["chat_id"]):
        if m["course"] in removed:
            db.unmute_course(user["chat_id"], m["course_id"])
    await update.message.reply_text("Unmuted: " + ", ".join(removed) if removed else "Nothing to unmute.")


async def cmd_style(update, context):
    user = await require_login(update)
    if not user:
        return
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):  # no argument (e.g. the Mass mode button): flip the current setting
        arg = "off" if user["style"] else "on"
    db.set_field(user["chat_id"], "style", 1 if arg == "on" else 0)
    await update.message.reply_text(
        "Mass mode ON. Reminders will come with dialogues and GIFs. Try /test 1h to preview."
        if arg == "on" else "Mass mode OFF. Plain reminders only."
    )


def _onoff(v) -> str:
    return "ON ✅" if v else "OFF ❌"


def settings_panel(user):
    quiet = bool(user["quiet_start"])
    rows = [
        [InlineKeyboardButton(f"🎬 Mass mode: {_onoff(user['style'])}", callback_data="set:style")],
        [InlineKeyboardButton(f"🖼 Memes & GIFs: {_onoff(user['memes'])}", callback_data="set:memes")],
        [InlineKeyboardButton(f"🤖 AI dialogues: {_onoff(user['ai_lines'])}", callback_data="set:ai_lines")],
        [InlineKeyboardButton(f"🌅 Daily digest: {_onoff(user['digest_time'])}", callback_data="set:digest")],
        [InlineKeyboardButton(f"🌙 Quiet hours: {_onoff(quiet)}", callback_data="set:quiet")],
        [InlineKeyboardButton(f"📊 Weekly summary: {_onoff(user['weekly'])}", callback_data="set:weekly")],
        [InlineKeyboardButton(f"🏅 Show me on leaderboard: {_onoff(user['board'])}", callback_data="set:board")],
        [InlineKeyboardButton(f"🌐 Language: {'తెలుగు style (Telugu)' if user['lang'] == 'te' else 'English'}", callback_data="set:lang")],
        [
            InlineKeyboardButton("🔔 Reminder times", callback_data="set:times"),
            InlineKeyboardButton("🧪 Test", callback_data="set:test"),
        ],
        [InlineKeyboardButton("💡 Send feedback to admin", callback_data="fb:ask")],
        [InlineKeyboardButton("🚪 Logout", callback_data="lo:ask")],
    ]
    text = (
        "⚙️ <b>Settings</b>\nTap a switch to turn it ON or OFF.\n\n"
        f"🔔 Reminders: <b>{esc(user['offsets'])}</b> before each deadline\n"
        f"🌅 Digest: {esc(user['digest_time'] or 'off')}  🌙 Quiet: "
        f"{esc(user['quiet_start'] + '-' + user['quiet_end']) if quiet else 'off'}\n"
        f"🕐 Timezone: {esc(user['tz'])}\n\nNot sure what a switch does? Open ❓ Help."
    )
    return text, InlineKeyboardMarkup(rows)


async def cmd_settings(update, context):
    user = await require_login(update)
    if not user:
        return
    text, kb = settings_panel(user)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_stats(update, context):
    user = await require_login(update)
    if not user:
        return
    st = db.get_stats(user["chat_id"])
    nxt = next((n for n in LEVELS if n[0] > st["points"]), None)
    bar_to = f"\nNext level in {nxt[0] - st['points']} points" if nxt else "\nMax level reached!"
    await update.effective_message.reply_text(
        f"🏆 <b>Your stats</b>\n\nLevel: <b>{level_for(st['points'])}</b>\nPoints: {st['points']}{bar_to}\n"
        f"✅ Submitted: {st['submitted']}  ⚡ Early: {st['early']}\n"
        f"🔥 Streak: {st['streak']} day(s)  (best {st['best_streak']})\n"
        f"🎖 Badges: {', '.join(BADGES[b] for b in filter(None, (user['badges'] or '').split(',')) if b in BADGES) or 'none yet'}\n\n"
        "You earn 10 points for every task you mark Submitted, +5 if it was more than a day before the deadline.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_fun(update, context):
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text(random.choice(FUN_LINES))
    await send_meme(context, chat_id, random.choice(["1d", "6h", "2h", "1h", "50m", "10m"]))


async def cmd_addmeme_help(update, context):
    await update.message.reply_text(
        "Admin: send me a GIF, sticker or photo with the caption <code>meme any</code> (or <code>meme 1h</code>, "
        "<code>meme 10m</code>...) and I'll use it in reminders.", parse_mode=ParseMode.HTML,
    )


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin teaches the bot a meme/GIF/sticker: send it with caption 'meme <stage|any>'."""
    m = update.message
    if not is_admin(update.effective_user):
        return
    caption = (m.caption or "").lower().split()
    tag = caption[1] if len(caption) > 1 and caption[0] == "meme" else None
    if tag is None and not m.sticker:
        return
    tag = tag or "any"
    if tag != "any":
        try:
            tag = punchlines.stage_for(parse_offsets(tag)[0])
        except ValueError:
            await m.reply_text("Unknown stage. Use any, 1d, 6h, 2h, 1h, 50m or 10m.")
            return
    obj, kind = (m.animation, "animation") if m.animation else (m.sticker, "sticker") if m.sticker else (m.photo[-1], "photo") if m.photo else (None, None)
    if obj is None:
        return
    db.media_add(obj.file_unique_id, obj.file_id, kind, tag, update.effective_user.id, int(time.time()))
    await m.reply_text(f"Saved as {kind} for stage: {tag}")


async def cmd_botstats(update, context):
    if not is_admin(update.effective_user):
        return
    media = ", ".join(f"{r['tag']}/{r['kind']}: {r['n']}" for r in db.media_counts()) or "none"
    pool = db.pool_counts(int(time.time()) - 3 * 86400)
    lines = ", ".join(f"{k}: {v[0]}/{v[1]}" for k, v in sorted(pool.items()))
    await update.message.reply_text(
        f"Users: {db.count_users()}\nDialogue pool (free/total): {lines}\nMemes: {media}\nGemini: {'on' if gemini.enabled() else 'off'}"
    )


MENU_ACTIONS = {
    BTN_TODAY: cmd_today, BTN_WEEK: cmd_week, BTN_UPCOMING: cmd_upcoming,
    BTN_OVERDUE: cmd_overdue, BTN_SETTINGS: cmd_settings, BTN_HELP: cmd_help,
    BTN_STATS: cmd_stats, BTN_FUN: cmd_fun, BTN_CONNECT: cmd_connect, BTN_LOGOUT: cmd_logout,
}
_chat_times: dict[int, list[float]] = {}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu button taps arrive as text and run their command; any other text is a question for Gemini."""
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    action = MENU_ACTIONS.get(text)
    if action:
        context.user_data.pop("await_fb", None)
        context.user_data.pop("adm", None)
        touch(chat_id)
        await action(update, context)
        await maybe_ask_rating(context.bot, chat_id)
        return
    if is_admin(update.effective_user) and await admin_flow_text(update, context, text):
        return
    if context.user_data.pop("await_rate_comment", False):
        u = update.effective_user
        db.rating_set(chat_id, int(context.user_data.pop("stars", 3)), text[:900], int(time.time()), u.full_name or "", u.username or "")
        await notify_admins(context.bot, f"⭐ Rating comment from {u.full_name} (@{u.username or '-'}): {text[:900]}")
        await update.message.reply_text("🙏 Thank you, this really helps us improve!")
        return
    if context.user_data.pop("await_fb", False):
        now = time.time()
        recent = [t for t in _fb_times.get(chat_id, []) if now - t < 3600]
        if len(recent) >= 3:
            await update.message.reply_text("You've sent a few messages already. Please try again in an hour 🙏")
            return
        _fb_times[chat_id] = recent + [now]
        u = update.effective_user
        await notify_admins(context.bot, f"💡 Feedback from {u.full_name} (@{u.username or '-'}, ID {u.id}):\n\n{text[:1500]}")
        await update.message.reply_text("🙏 Thank you! Your message was sent to the admin.")
        return
    touch(chat_id)
    await gemini_answer(context, chat_id, text, update.message.reply_text)


async def weekly_job(context: ContextTypes.DEFAULT_TYPE):
    """Sunday 18:00 (student's time zone): a summary of the week ahead."""
    now = int(time.time())
    for user in db.all_users():
        if not user["weekly"]:
            continue
        try:
            local = datetime.fromtimestamp(now, ZoneInfo(user["tz"]))
        except ZoneInfoNotFoundError:
            continue
        today = local.strftime("%Y-%m-%d")
        if local.weekday() != 6 or local.strftime("%H:%M") != "18:00" or user["last_weekly"] == today:
            continue
        db.set_field(user["chat_id"], "last_weekly", today)
        cid = user["chat_id"]
        evs = db.pending_events(cid, since=now, until=now + 7 * 86400)
        late = db.pending_events(cid, until=now, since=now - 30 * 86400)
        st = db.get_stats(cid)
        lines = [f"📊 <b>{tr(user['lang'], 'week')}</b>", ""]
        if evs:
            lines.append(f"📌 <b>{len(evs)}</b> deadline(s) in the next 7 days:")
            lines += [f"• {esc(e['name'])} - {fmt_due(e['due_ts'], user['tz'])}" for e in evs[:10]]
        else:
            lines.append("🎉 Nothing due this week. Enjoy!")
        if late:
            lines.append(f"\n⚠️ {len(late)} overdue task(s)")
        lines.append(f"\n{level_for(st['points'])}  •  ⭐ {st['points']} pts  •  🔥 {st['streak']}-day streak")
        try:
            await context.bot.send_message(cid, "\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("weekly summary failed for %s", cid)


async def cmd_leaderboard(update, context):
    rows = db.leaderboard(10)
    if not rows:
        await update.effective_message.reply_text(
            "🏅 The leaderboard is empty. Turn on <b>Show me on leaderboard</b> in ⚙️ Settings to join!", parse_mode=ParseMode.HTML)
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏅 <b>Leaderboard</b> (friends who joined)", ""]
    for i, r in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {esc(r['name'] or 'Student')} - ⭐ {r['points']} pts • 🔥 {r['streak']}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_admin(update, context):
    """/admin <code> claims admin rights (the code is printed in the Render log); /admin alone opens the panel."""
    uid = update.effective_user.id
    if context.args:
        if context.args[0] == ADMIN_CODE:
            db.add_admin(uid)
            try:
                await update.message.delete()
            except Exception:
                pass
            await context.bot.set_my_commands(
                [BotCommand("admin", "Admin panel"), BotCommand("broadcast", "Message all students"),
                 BotCommand("botstats", "Users and pool stats")] + _public_commands(),
                scope=BotCommandScopeChat(uid),
            )
            await context.bot.send_message(uid, "✅ You are now the admin. New users, feedback and errors will be sent to you.")
        else:
            await update.message.reply_text("Wrong code.")
            return
    elif not is_admin(uid):
        u = update.effective_user
        await update.message.reply_text(
            f"🔒 This is for the admin only.\n\nI see you as: @{u.username or '(no username)'}  •  ID {u.id}\n"
            f"ADMIN_IDS is set in Render: {'yes' if (ADMIN_IDS or ADMIN_USERNAMES) else 'NO (empty, or the bot has not restarted yet)'}\n\n"
            f"To become admin: in Render > Environment set ADMIN_IDS = {u.id}  (just this number), save, wait for the redeploy, then send /admin again."
        )
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast now", callback_data="adm:bc"), InlineKeyboardButton("🗓 Schedule announcement", callback_data="adm:sched")],
        [InlineKeyboardButton("📝 Exam countdown", callback_data="adm:exam"), InlineKeyboardButton("🗓 Scheduled list", callback_data="adm:pending")],
        [InlineKeyboardButton("🩺 Sync health", callback_data="adm:health"), InlineKeyboardButton("⭐ Ratings", callback_data="adm:ratings")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats"), InlineKeyboardButton("👥 Recent users", callback_data="adm:users")],
        [InlineKeyboardButton("🔄 Sync everyone now", callback_data="adm:sync"), InlineKeyboardButton("🗄 Backup now", callback_data="adm:backup")],
        [InlineKeyboardButton("🤖 Test Gemini", callback_data="adm:gemini"), InlineKeyboardButton("🖼 Memes saved", callback_data="adm:memes")],
    ])
    await context.bot.send_message(uid, f"🛠 <b>Admin panel</b>\nStudents connected: <b>{db.count_users()}</b>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_broadcast(update, context):
    """Admin only: /broadcast <message> sends to every connected student."""
    if not is_admin(update.effective_user):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast your message")
        return
    sent = failed = 0
    for u in db.all_users():
        try:
            await context.bot.send_message(u["chat_id"], f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # stay under Telegram's rate limit
    await update.message.reply_text(f"Broadcast done. Sent: {sent}, failed (blocked the bot): {failed}")


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Daily: send the admin a copy of the users table (calendar links stay encrypted with SECRET_KEY)."""
    if not admin_ids():
        return
    data = json.dumps(db.export_users(), default=str, ensure_ascii=False, indent=1).encode()
    for aid in admin_ids():
        try:
            await context.bot.send_document(
                aid, io.BytesIO(data), filename=f"backup-{datetime.utcnow():%Y-%m-%d}.json",
                caption="🗄 Daily backup of users (keep SECRET_KEY safe, it is needed to read it).",
            )
        except Exception:
            log.warning("backup send failed")


_last_alert = 0.0


async def on_error(update, context):
    """Log every error and tell the admin (at most one alert per 10 minutes)."""
    global _last_alert
    if isinstance(context.error, Conflict):
        # Two copies ran at once (normal for a minute while Render swaps versions). Telegram settles it by itself.
        log.warning("Telegram Conflict: another instance was polling; ignoring (no alert)")
        return
    if isinstance(context.error, BadRequest) and "not modified" in str(context.error).lower():
        return  # harmless: the message already showed exactly that
    log.error("Unhandled error", exc_info=context.error)
    if time.time() - _last_alert > 600:
        _last_alert = time.time()
        await notify_admins(context.bot, f"⚠️ Bot error: {type(context.error).__name__}: {str(context.error)[:300]}")


# ======================= more features =======================
BADGES = {
    "streak7": "🔥 7-day streak", "streak30": "👑 30-day streak", "early5": "⚡ Early bird (5 tasks early)",
    "sub10": "🎯 10 tasks submitted", "sub50": "🏆 50 tasks submitted",
}
_fb_times: dict[int, list[float]] = {}
_gem_day: dict[int, tuple[str, int]] = {}
AUDIENCES = {"all": "👥 Everyone", "te": "🌐 Telugu-style users", "due24": "⏰ Has a task due in 24h", "board": "🏅 Leaderboard members"}


def local_now(user):
    try:
        return datetime.fromtimestamp(time.time(), ZoneInfo(user["tz"]))
    except ZoneInfoNotFoundError:
        return datetime.fromtimestamp(time.time(), ZoneInfo("Asia/Kolkata"))


def new_badges(chat_id) -> list[str]:
    st = db.get_stats(chat_id)
    user = db.get_user(chat_id)
    have = set(filter(None, (user["badges"] or "").split(","))) if user else set()
    earned = set()
    if st["best_streak"] >= 7: earned.add("streak7")
    if st["best_streak"] >= 30: earned.add("streak30")
    if st["early"] >= 5: earned.add("early5")
    if st["submitted"] >= 10: earned.add("sub10")
    if st["submitted"] >= 50: earned.add("sub50")
    fresh = sorted(earned - have)
    if fresh and user:
        db.set_field(chat_id, "badges", ",".join(sorted(have | earned)))
    return fresh


# ---- rating / feedback in the middle of use ----
async def maybe_ask_rating(bot, chat_id, force=False):
    user = db.get_user(chat_id)
    if not user or user["rated"]:
        return
    now = int(time.time())
    if not force and (user["uses"] < 12 or now - (user["last_rating_ask"] or 0) < 7 * 86400):
        return
    db.set_field(chat_id, "last_rating_ask", now)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐", callback_data="rate:1"), InlineKeyboardButton("⭐⭐", callback_data="rate:2"), InlineKeyboardButton("⭐⭐⭐", callback_data="rate:3")],
        [InlineKeyboardButton("⭐⭐⭐⭐", callback_data="rate:4"), InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="rate:5")],
        [InlineKeyboardButton("Maybe later", callback_data="rate:0")],
    ])
    try:
        await bot.send_message(chat_id, "💬 <b>Quick question!</b>\nHow are you liking the bot so far? Tap the stars:",
                               parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass


def touch(chat_id):
    try:
        db.bump_uses(chat_id)
    except Exception:
        pass


# ---- Gemini helper used by chat and the study planner ----
async def gemini_answer(context, chat_id, text, reply):
    if not gemini.enabled():
        await reply("🤖 AI answers are off right now (the admin has not set the Gemini key).")
        return
    now = time.time()
    recent = [t for t in _chat_times.get(chat_id, []) if now - t < 60]
    day = time.strftime("%Y-%m-%d")
    d_day, d_n = _gem_day.get(chat_id, (day, 0))
    if d_day != day:
        d_n = 0
    if len(recent) >= 6:
        await reply("Slow down a little 😅 Try again in a minute.")
        return
    if d_n >= int(os.getenv("GEMINI_DAILY_LIMIT", "40")):
        await reply("You've used today's AI questions. Come back tomorrow 🙏")
        return
    _chat_times[chat_id] = recent + [now]
    _gem_day[chat_id] = (day, d_n + 1)
    user = db.get_user(chat_id)
    tz = user["tz"] if user else "Asia/Kolkata"
    deadlines = "The student is not connected, so you know no deadlines."
    if user and user["token_enc"]:
        if SHARE_DEADLINES:
            evs = db.pending_events(chat_id, since=int(now) - 86400)[:15]
            deadlines = ("Pending tasks:\n" + "\n".join(f"- {e['name']} ({e['course']}) due {fmt_due(e['due_ts'], tz)}" for e in evs)) if evs else "The student has no pending tasks."
        else:
            deadlines = "You have no access to the student's deadlines."
    try:
        await context.bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass
    history = context.user_data.setdefault("hist", [])
    try:
        stamp = datetime.now(ZoneInfo(tz)).strftime("%a %d %b %Y %H:%M")
    except ZoneInfoNotFoundError:
        stamp = datetime.now().strftime("%a %d %b %Y %H:%M")
    answer = await gemini.chat(text, history, deadlines, DEFAULT_SITE, tz, stamp)
    if not answer:
        await reply("I couldn't think of an answer right now. Try again, or use the buttons.")
        return
    history += [("user", text[:800]), ("model", answer[:800])]
    del history[:-8]
    await reply(answer[:3500])


# ---- Tools panel ----
BTN_TOOLS, BTN_PLAN, BTN_TICK = "🧰 Tools", "🧠 Plan my week", "☑️ Tick done"


def tools_panel(user):
    muted = (user["mute_until"] or 0) > time.time()
    rows = [
        [InlineKeyboardButton("🧠 Plan my week (AI)", callback_data="tl:plan"), InlineKeyboardButton("☑️ Tick finished tasks", callback_data="tick:list")],
        [InlineKeyboardButton(f"😴 Mute all today: {_onoff(muted)}", callback_data="tl:mute"), InlineKeyboardButton("📚 Course reminder times", callback_data="cm:list")],
        [InlineKeyboardButton("📝 Exam countdown", callback_data="tl:exams"), InlineKeyboardButton(f"🎴 Daily dialogue card: {_onoff(user['quote_on'])}", callback_data="tl:quote")],
        [InlineKeyboardButton("🏅 Leaderboard", callback_data="tl:board"), InlineKeyboardButton("🎖 My badges", callback_data="tl:badges")],
        [InlineKeyboardButton("🔐 My data (download / delete)", callback_data="tl:data")],
        [InlineKeyboardButton("💡 Feedback", callback_data="fb:ask"), InlineKeyboardButton("⭐ Rate the bot", callback_data="tl:rate")],
    ]
    return "🧰 <b>Tools</b>\nEverything extra lives here. Tap what you need:", InlineKeyboardMarkup(rows)


async def cmd_tools(update, context):
    user = await require_login(update)
    if not user:
        return
    touch(user["chat_id"])
    text, kb = tools_panel(user)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_plan(update, context):
    user = await require_login(update)
    if not user:
        return
    touch(user["chat_id"])
    await gemini_answer(context, user["chat_id"],
                        "Make a realistic day-by-day study plan for my next 7 days using my pending tasks. Short bullet lines, one per day.",
                        update.effective_message.reply_text)


async def cmd_tick(update, context):
    user = await require_login(update)
    if not user:
        return
    await show_tick(update.effective_message, user["chat_id"], reply=True)


async def show_tick(message, chat_id, reply=False):
    rows = db.pending_events(chat_id, since=int(time.time()) - 14 * 86400)[:10]
    if not rows:
        text, kb = "🎉 Nothing pending. You're all caught up!", None
    else:
        text = "☑️ <b>Tap the tasks you have finished</b> (you earn points):"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬜ {e['name'][:45]}", callback_data=f"tick:{e['event_id']}")] for e in rows])
    if reply:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def complete_task(context, chat_id, event_id, quiet=False):
    """Mark a task done and award points/badges. Returns a one-line summary."""
    ev = db.get_event(chat_id, event_id)
    db.mark_done(chat_id, event_id)
    cur = db.get_user(chat_id)
    tz = cur["tz"] if cur else "Asia/Kolkata"
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Kolkata")
    today = datetime.now(zone).date()
    early = bool(ev) and ev["due_ts"] - time.time() > 86400
    gained = 15 if early else 10
    st = db.record_submit(chat_id, gained, early, today.isoformat(), (today - timedelta(days=1)).isoformat())
    for b in new_badges(chat_id):
        await send_fx(context.bot, chat_id, f"🎖 <b>New badge unlocked!</b>\n{BADGES[b]}", FX_PARTY, parse_mode=ParseMode.HTML)
    return gained, st


# ---- course reminder times ----
def course_menu(chat_id):
    co = db.course_offsets(chat_id)
    courses = db.courses_of(chat_id)
    if not courses:
        return "📚 No courses with pending tasks yet.", None
    rows = [[InlineKeyboardButton(f"{c[:30]}  ({co[cid] if cid in co else 'default'})"[:60], callback_data=f"cm:{cid}")] for cid, c in courses[:12]]
    return "📚 <b>Pick a course</b> to set its own reminder times:", InlineKeyboardMarkup(rows)


# ---- exams ----
async def cmd_exam(update, context):
    """Admin: /exam 2026-11-20 Mid-term exams"""
    if not is_admin(update.effective_user):
        return
    try:
        day = datetime.strptime(context.args[0], "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        title = " ".join(context.args[1:]).strip()[:90]
        assert title
    except Exception:
        await update.message.reply_text("Usage: /exam 2026-11-20 Mid-term exams\n/exams lists them, /clearexams removes all.")
        return
    db.exam_add(title, int(day.timestamp()))
    await update.message.reply_text(f"✅ Exam saved: {title} on {context.args[0]}. Students get a daily countdown at 8 AM.")


async def cmd_exams(update, context):
    rows = db.exams_upcoming(int(time.time()) - 86400)
    if not rows:
        await update.effective_message.reply_text("📝 No exams announced yet.")
        return
    now = time.time()
    await update.effective_message.reply_text("📝 <b>Exams</b>\n" + "\n".join(
        f"• {esc(r['title'])}: <b>{max(0, int((r['exam_ts'] - now) // 86400))} days</b> left" for r in rows), parse_mode=ParseMode.HTML)


async def cmd_clearexams(update, context):
    if is_admin(update.effective_user):
        db.exam_delete_all()
        await update.message.reply_text("Exams cleared.")


async def exam_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    exams = [e for e in db.exams_upcoming(now) if e["exam_ts"] - now < 60 * 86400]
    if not exams:
        return
    for user in db.all_users():
        loc = local_now(user)
        today = loc.strftime("%Y-%m-%d")
        if loc.strftime("%H:%M") != "08:00" or user["last_exam"] == today:
            continue
        db.set_field(user["chat_id"], "last_exam", today)
        lines = ["📝 <b>Exam countdown</b>"] + [f"• {esc(e['title'])}: <b>{max(0, int((e['exam_ts'] - now) // 86400))} days</b> left" for e in exams[:5]]
        lines.append("\nPlan your revision today 💪")
        try:
            await context.bot.send_message(user["chat_id"], "\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---- daily dialogue card ----
async def quote_job(context: ContextTypes.DEFAULT_TYPE):
    for user in db.all_users():
        if not user["quote_on"]:
            continue
        loc = local_now(user)
        today = loc.strftime("%Y-%m-%d")
        if loc.strftime("%H:%M") != "09:00" or user["last_quote"] == today:
            continue
        db.set_field(user["chat_id"], "last_quote", today)
        st = await punchlines.build(random.choice([86400, 21600, 7200]), None, bool(user["ai_lines"]))
        try:
            await send_fx(context.bot, user["chat_id"], f"🎴 <b>Today's dialogue</b>\n\n{st['emoji']} <i>\"{esc(st['quote'])}\"</i>\n\nHave a great day, hero! 💪",
                          None, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---- group leaderboard ----
async def cmd_setgroup(update, context):
    """Admin: send /setgroup inside your students' group (add the bot there first)."""
    if not is_admin(update.effective_user):
        return
    if update.effective_chat.type == "private":
        await update.message.reply_text("Add me to your students' group, then send /setgroup there.")
        return
    db.kv_set("group_id", update.effective_chat.id)
    await update.message.reply_text("✅ Done! Every Sunday 7 PM I will post the leaderboard here.")


async def group_job(context: ContextTypes.DEFAULT_TYPE):
    gid = db.kv_get("group_id")
    if not gid:
        return
    loc = datetime.fromtimestamp(time.time(), ZoneInfo("Asia/Kolkata"))
    today = loc.strftime("%Y-%m-%d")
    if loc.weekday() != 6 or loc.strftime("%H:%M") != "19:00" or db.kv_get("group_last") == today:
        return
    db.kv_set("group_last", today)
    rows = db.leaderboard(5)
    if not rows:
        return
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    text = "🏅 <b>Weekly leaderboard</b>\n\n" + "\n".join(f"{medals[i]} {esc(r['name'] or 'Student')} - ⭐ {r['points']} • 🔥 {r['streak']}" for i, r in enumerate(rows))
    try:
        await context.bot.send_message(int(gid), text + "\n\nJoin in: open the bot, ⚙️ Settings > Leaderboard ON", parse_mode=ParseMode.HTML)
    except Exception:
        log.warning("group post failed")


# ---- scheduled announcements and targeted broadcast ----
async def deliver(bot, text, ids):
    sent = failed = 0
    for cid in ids:
        try:
            await bot.send_message(cid, f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


async def announce_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    for a in db.ann_due(now):
        db.ann_done(a["id"])
        sent, failed = await deliver(context.bot, a["text"], db.users_where(a["audience"], now))
        await notify_admins(context.bot, f"🗓 Scheduled announcement sent to {sent} students (failed {failed}).")


async def admin_flow_text(update, context, text):
    """Steps of the admin broadcast / schedule flow typed after pressing a panel button."""
    flow = context.user_data.get("adm")
    if not flow:
        return False
    if text == "/cancel":
        context.user_data.pop("adm", None)
        await update.message.reply_text("Cancelled.")
        return True
    if flow["step"] == "text":
        if flow["mode"] == "bc":
            context.user_data.pop("adm", None)
            sent, failed = await deliver(context.bot, text, db.users_where(flow["aud"], int(time.time())))
            await update.message.reply_text(f"✅ Sent to {sent} students (failed: {failed}).")
        else:
            flow.update(text=text, step="when")
            await update.message.reply_text("🕐 When should I send it? Type date and time in India time, like:\n2026-10-20 18:30")
        return True
    if flow["step"] == "when":
        try:
            when = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            assert when.timestamp() > time.time()
        except Exception:
            await update.message.reply_text("Please use the format 2026-10-20 18:30 (a future time), or /cancel.")
            return True
        db.ann_add(int(when.timestamp()), flow["text"], flow["aud"])
        context.user_data.pop("adm", None)
        await update.message.reply_text(f"🗓 Scheduled for {text} IST to: {AUDIENCES[flow['aud']]}")
        return True
    return False


# ---- privacy ----
async def send_my_data(context, chat_id):
    user = db.get_user(chat_id)
    if not user:
        return
    data = {
        "telegram_id": chat_id, "lms_user_id": user["moodle_userid"], "site": user["site_url"], "timezone": user["tz"],
        "reminder_times": user["offsets"], "settings": {k: user[k] for k in ("style", "memes", "ai_lines", "weekly", "lang", "board")},
        "stats": db.get_stats(chat_id),
        "tasks": [{"name": e["name"], "course": e["course"], "due": fmt_due(e["due_ts"], user["tz"]), "done": bool(e["done"])}
                  for e in db.pending_events(chat_id, include_muted=True)],
        "note": "Your LMS calendar link is stored encrypted and is not included here.",
    }
    await context.bot.send_document(chat_id, io.BytesIO(json.dumps(data, indent=1, ensure_ascii=False, default=str).encode()),
                                    filename="my-data.json", caption="🔐 Everything this bot stores about you.")


async def cmd_mydata(update, context):
    user = await require_login(update)
    if user:
        await send_my_data(context, user["chat_id"])


# ---- referral reward ----
async def reward_referral(bot, new_chat_id):
    ref = db.kv_get(f"ref:{new_chat_id}")
    if not ref or db.kv_get(f"refdone:{new_chat_id}"):
        return
    db.kv_set(f"refdone:{new_chat_id}", 1)
    ref = int(ref)
    if ref == new_chat_id or not db.get_user(ref):
        return
    db.add_points(ref, 20)
    db.add_points(new_chat_id, 20)
    for cid, txt in ((ref, "🎁 Your friend joined from your invite! +20 points for you."), (new_chat_id, "🎁 Welcome bonus: +20 points for joining through a friend's invite!")):
        try:
            await send_fx(bot, cid, txt, FX_PARTY)
        except Exception:
            pass


# ---- sync health (admin) ----
async def ask_failing_to_reconnect(bot):
    now = int(time.time())
    n = 0
    for r in db.failing_users(now):
        u = db.get_user(r["chat_id"])
        if now - (u["reconnect_asked"] or 0) < 3 * 86400:
            continue
        db.set_field(r["chat_id"], "reconnect_asked", now)
        try:
            await bot.send_message(r["chat_id"], "⚠️ I can't reach your LMS calendar any more, so reminders may stop.\nPlease log out and connect again to fix it.",
                                   reply_markup=extras_markup(logout=True))
            n += 1
        except Exception:
            pass
    return n



async def pool_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await punchlines.refill_once()
    except Exception:
        log.exception("pool refill failed")


def is_linked(chat_id) -> bool:
    u = db.get_user(chat_id)
    return bool(u and u["token_enc"])


async def cmd_msg(update, context):
    """Admin only: /msg <telegram id> <text> sends a private message to one student."""
    if not is_admin(update.effective_user):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /msg 123456789 your text")
        return
    try:
        await context.bot.send_message(int(context.args[0]), "💬 Message from admin:\n\n" + " ".join(context.args[1:]))
        await update.message.reply_text("Sent ✅")
    except Exception:
        await update.message.reply_text("Could not send (user blocked the bot or ID is wrong).")


async def cmd_myid(update, context):
    u = update.effective_user
    await update.message.reply_text(
        f"🆔 Your Telegram ID: <code>{u.id}</code>\nUsername: @{u.username or '(none)'}\n\n"
        "Admin? Put this number in Render > Environment > ADMIN_IDS.", parse_mode=ParseMode.HTML)


async def cmd_cancel(update, context):
    context.user_data.pop("await_bc", None)
    context.user_data.pop("await_fb", None)
    await update.message.reply_text("Cancelled.")


def NS_update(q):
    """Tiny stand-in so admin buttons can reuse command handlers."""
    from types import SimpleNamespace
    return SimpleNamespace(effective_user=q.from_user, message=q.message, effective_message=q.message, effective_chat=q.message.chat)


# ---------- inline buttons ----------
async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    parts = q.data.split(":")
    action = parts[0]
    if action not in ("rate",):
        touch(chat_id)
    if action == "done":
        cur = db.get_user(chat_id)
        lang = cur["lang"] if cur else "en"
        gained, st = await complete_task(context, chat_id, int(parts[1]))
        await q.answer(f"🎉 Awesome! Submitted!\n+{gained} points  •  🔥 {st['streak']} day streak", show_alert=True)
        try:
            await q.edit_message_text(
                (q.message.text or q.message.caption or "") + f"\n\n✅ <b>Submitted!</b> +{gained} points", parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await q.edit_message_reply_markup(None)
        await send_fx(
            context.bot, chat_id,
            f"🎉 <b>{tr(lang, 'task_done')}</b> 🥳\n{level_for(st['points'])}  •  ⭐ {st['points']} pts  •  🔥 {st['streak']}-day streak",
            FX_PARTY, parse_mode=ParseMode.HTML,
        )
        if st["submitted"] in (3, 10):
            await maybe_ask_rating(context.bot, chat_id, force=True)  # a happy moment to ask
    elif action == "snz":
        until = int(time.time()) + int(parts[2]) * 60
        db.set_snooze(chat_id, int(parts[1]), until)
        await q.answer("Snoozed")
        await q.edit_message_reply_markup(None)
        u = db.get_user(chat_id)
        n = (u["snooze_count"] or 0) + 1 if u else 0
        db.set_field(chat_id, "snooze_count", n)
        if u and n >= 3:
            db.set_field(chat_id, "snooze_count", 0)
            await context.bot.send_message(chat_id, "😴 You snooze a lot! Want me to remind you earlier, 3 hours before each deadline?",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏰ Yes, add a 3h reminder", callback_data="early:add")]]))
    elif action == "early":
        u = db.get_user(chat_id)
        offs = [o for o in (u["offsets"] or DEFAULT_OFFSETS).split(",") if o]
        if "3h" not in offs:
            offs.append("3h")
            offs = [fmt_offset(o) for o in sorted(parse_offsets(",".join(offs)), reverse=True)]
            db.set_field(chat_id, "offsets", ",".join(offs))
        await q.answer("Added!")
        await q.edit_message_text("✅ Done. I'll also remind you 3 hours before each deadline.")
    elif action == "tick":
        if parts[1] == "list":
            await q.answer()
            await show_tick(q.message, chat_id, reply=True)
        else:
            gained, st = await complete_task(context, chat_id, int(parts[1]))
            await q.answer(f"✅ +{gained} points • 🔥 {st['streak']}-day streak", show_alert=False)
            await show_tick(q.message, chat_id)
    elif action == "tl":
        u = db.get_user(chat_id)
        key = parts[1]
        await q.answer()
        if not u:
            return
        if key == "plan":
            await gemini_answer(context, chat_id, "Make a realistic day-by-day study plan for my next 7 days using my pending tasks. Short bullet lines, one per day.", q.message.reply_text)
        elif key == "mute":
            on = (u["mute_until"] or 0) > time.time()
            end = local_now(u).replace(hour=23, minute=59, second=59)
            db.set_field(chat_id, "mute_until", 0 if on else int(end.timestamp()))
            text, kb = tools_panel(db.get_user(chat_id))
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif key == "quote":
            db.set_field(chat_id, "quote_on", 0 if u["quote_on"] else 1)
            text, kb = tools_panel(db.get_user(chat_id))
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif key == "exams":
            await cmd_exams(NS_update(q), context)
        elif key == "board":
            await cmd_leaderboard(NS_update(q), context)
        elif key == "badges":
            have = [BADGES[b] for b in filter(None, (u["badges"] or "").split(",")) if b in BADGES]
            locked = [v for k, v in BADGES.items() if k not in (u["badges"] or "")]
            await q.message.reply_text("🎖 <b>Your badges</b>\n" + ("\n".join("✅ " + b for b in have) or "None yet") + "\n\n<b>To unlock</b>\n" + "\n".join("🔒 " + b for b in locked), parse_mode=ParseMode.HTML)
        elif key == "data":
            await q.message.reply_text("🔐 <b>Your data</b>\nI store only your Telegram ID, LMS user ID, an encrypted calendar link, your tasks and your settings. Your password is never stored.",
                                       parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download my data", callback_data="tl:dl")],
                                                                          [InlineKeyboardButton("🗑 Delete my data", callback_data="lo:ask")]]))
        elif key == "dl":
            await send_my_data(context, chat_id)
        elif key == "rate":
            await maybe_ask_rating(context.bot, chat_id, force=True)
    elif action == "cm":
        if parts[1] == "list":
            await q.answer()
            text, kb = course_menu(chat_id)
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif len(parts) == 2:
            await q.answer()
            cid = int(parts[1])
            rows = [[InlineKeyboardButton(f"{n}: {d}", callback_data=f"cm:{cid}:{i}")] for i, (n, v, d) in enumerate(PRESETS)]
            rows.append([InlineKeyboardButton("↩️ Use my default", callback_data=f"cm:{cid}:x")])
            await q.message.reply_text("⏱ Choose reminder times for this course:", reply_markup=InlineKeyboardMarkup(rows))
        else:
            cid = int(parts[1])
            db.set_course_offsets(chat_id, cid, None if parts[2] == "x" else PRESETS[int(parts[2])][1])
            await q.answer("Saved ✅")
            text, kb = course_menu(chat_id)
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif action == "rate":
        stars = int(parts[1])
        await q.answer()
        if stars == 0:
            await q.edit_message_text("No problem, maybe later 🙂")
            return
        u = q.from_user
        db.rating_set(chat_id, stars, "", int(time.time()), u.full_name or "", u.username or "")
        db.set_field(chat_id, "rated", 1)
        await notify_admins(context.bot, f"⭐ New rating: {stars}/5 from {u.full_name} (@{u.username or '-'}, ID {u.id})")
        if stars >= 4:
            await q.edit_message_text("🥳 Thank you! Tell your friends, the invite button is in 🧰 Tools.")
        else:
            context.user_data["await_rate_comment"] = True
            context.user_data["stars"] = stars
            await q.edit_message_text("🙏 Thanks for being honest. What should we improve? Type it now and I'll pass it to the admin.")
    elif action == "lo":
        if parts[1] == "ask":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, log out", callback_data="lo:yes"),
                InlineKeyboardButton("↩️ Cancel", callback_data="lo:no"),
            ]])
            await q.answer()
            await q.message.reply_text("Log out and delete all your data from this bot?", reply_markup=kb)
        elif parts[1] == "yes":
            db.delete_user(chat_id)
            await q.answer("😢 Logged out. We'll miss you!", show_alert=True)
            await q.edit_message_text("😢 Logged out. All your data was deleted.")
            await send_fx(context.bot, chat_id, "😭 <b>Goodbye...</b>\n\nCome back any time: send /start and tap Connect.",
                          FX_SAD, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(False))
        else:
            await q.answer("Cancelled")
            await q.edit_message_text("Okay, you stay logged in ✅")
    elif action == "adm":
        if not is_admin(q.from_user):
            await q.answer("Admin only", show_alert=True)
            return
        await q.answer()
        if parts[1] in ("bc", "sched"):
            context.user_data["adm"] = {"mode": parts[1], "step": "aud"}
            await q.message.reply_text("Who should get it?", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(v, callback_data=f"adm:aud:{k}")] for k, v in AUDIENCES.items()]))
        elif parts[1] == "aud":
            flow = context.user_data.get("adm")
            if flow:
                flow.update(aud=parts[2], step="text")
                await q.message.reply_text(f"✍️ Type your message now ({AUDIENCES[parts[2]]}). /cancel to stop.")
        elif parts[1] == "exam":
            await q.message.reply_text("📝 Add an exam countdown:\n/exam 2026-11-20 Mid-term exams\n\nSee them: /exams   Remove all: /clearexams")
        elif parts[1] == "health":
            rows = db.failing_users(int(time.time()))
            await q.message.reply_text(
                "🩺 <b>Sync health</b>\n" + ("\n".join(f"• <code>{r['chat_id']}</code> failed {r['sync_fail']}x" for r in rows) if rows else "All students sync fine ✅"),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📨 Ask them to reconnect", callback_data="adm:reconnect")]]) if rows else None)
        elif parts[1] == "reconnect":
            n = await ask_failing_to_reconnect(context.bot)
            await q.message.reply_text(f"📨 Asked {n} student(s) to reconnect.")
        elif parts[1] == "ratings":
            n, avg = db.rating_summary()
            rows = db.ratings_list(15)
            lines = [f"⭐ <b>Ratings</b>: {n} so far, average <b>{avg:.1f}/5</b>", ""]
            for r in rows:
                who = esc(r["name"] or "Unknown") + (f" (@{esc(r['username'])})" if r["username"] else "") + f" <code>{r['chat_id']}</code>"
                lines.append(f"{'⭐' * int(r['stars'])} {who}" + (f"\n   💬 {esc(r['comment'])}" if r["comment"] else ""))
            await q.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        elif parts[1] == "pending":
            rows = db.ann_pending()
            await q.message.reply_text("🗓 Scheduled:\n" + ("\n".join(f"• {datetime.fromtimestamp(r['run_at'], ZoneInfo('Asia/Kolkata')):%d %b %H:%M} → {AUDIENCES.get(r['audience'], r['audience'])}: {r['text'][:40]}" for r in rows) or "nothing"))
        elif parts[1] == "stats":
            st = db.admin_stats(int(time.time()))
            pool = db.pool_counts(int(time.time()) - 3 * 86400)
            await q.message.reply_text(
                f"📊 <b>Stats</b>\n👥 Students: {st['users']}\n📌 Pending tasks: {st['pending']} (due in 24h: {st['due_24h']})\n"
                f"🔔 Reminders sent: {st['reminders']}\n✅ Submitted via bot: {st['submitted']}\n🏅 On leaderboard: {st['board']}\n"
                f"💬 Dialogues free/total: " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(pool.items())),
                parse_mode=ParseMode.HTML)
        elif parts[1] == "users":
            rows = db.recent_users(10)
            now = int(time.time())
            lines = [f"• <code>{r['chat_id']}</code> synced {fmt_delta(now - int(r['last_sync'] or 0)) if r['last_sync'] else 'never'} ago ({r['source']}, {r['lang']})" for r in rows]
            await q.message.reply_text("👥 <b>Recent users</b>\n" + ("\n".join(lines) or "none yet") + "\n\nMessage one: /msg ID your text", parse_mode=ParseMode.HTML)
        elif parts[1] == "sync":
            await q.message.reply_text("🔄 Syncing everyone...")
            await sync_job(context)
            await q.message.reply_text("✅ Sync finished.")
        elif parts[1] == "gemini":
            if not gemini.enabled():
                await q.message.reply_text("🤖 Gemini is OFF: GEMINI_API_KEY is not set in Render.")
            else:
                ans = await gemini.chat("Say hello in one short Telugu mass-hero style line.", [], "No deadlines.", DEFAULT_SITE, "Asia/Kolkata", "now")
                await q.message.reply_text("🤖 Gemini works:\n" + ans if ans else "🤖 Gemini is ON but the call failed. Check the key, model name or quota in the Render logs.")
        elif parts[1] == "memes":
            media = ", ".join(f"{r['tag']}/{r['kind']}: {r['n']}" for r in db.media_counts()) or "none yet (send me a GIF/sticker with caption: meme any)"
            await q.message.reply_text("🖼 Memes saved: " + media)
        elif parts[1] == "backup":
            await backup_job(context)
            await q.message.reply_text("🗄 Backup sent.")
    elif action == "fb":
        context.user_data["await_fb"] = True
        await q.answer()
        await q.message.reply_text("💡 Type your idea, problem or message now. I'll send it to the admin.")
    elif action == "tp":
        name, value, desc = PRESETS[int(parts[1])]
        db.set_field(chat_id, "offsets", value)
        await q.answer(f"✅ {name} reminders saved", show_alert=False)
        await q.edit_message_text(f"✅ <b>{name}</b> reminders saved: {desc} before each deadline.", parse_mode=ParseMode.HTML)
    elif action == "set":
        user = db.get_user(chat_id)
        if not user:
            await q.answer("Not connected")
            return
        key = parts[1]
        if key in ("weekly",):
            db.set_field(chat_id, key, 0 if user[key] else 1)
        elif key == "board":
            on = 0 if user["board"] else 1
            db.set_field(chat_id, "board", on)
            if on:
                db.set_field(chat_id, "name", (q.from_user.first_name or "Student")[:40])
        elif key == "lang":
            db.set_field(chat_id, "lang", "en" if user["lang"] == "te" else "te")
        elif key in ("style", "memes", "ai_lines"):
            db.set_field(chat_id, key, 0 if user[key] else 1)
        elif key == "digest":
            db.set_field(chat_id, "digest_time", None if user["digest_time"] else "08:00")
        elif key == "quiet":
            if user["quiet_start"]:
                db.set_field(chat_id, "quiet_start", None)
                db.set_field(chat_id, "quiet_end", None)
            else:
                db.set_field(chat_id, "quiet_start", "23:00")
                db.set_field(chat_id, "quiet_end", "07:00")
        elif key == "times":
            await q.answer()
            rows = [[InlineKeyboardButton(f"{'✅ ' if user['offsets'] == v else ''}{n}: {d}", callback_data=f"tp:{i}")]
                    for i, (n, v, d) in enumerate(PRESETS)]
            await q.message.reply_text("🔔 <b>When should I remind you?</b>\nPick one:", parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(rows))
            return
        elif key == "test":
            await q.answer("Sending a preview")
            secs = random.choice(parse_offsets("1d,6h,2h,1h,50m,10m"))
            sample = {"event_id": -1, "course_id": 0, "name": "Sample assignment", "course": "Preview only",
                      "due_ts": int(time.time()) + secs, "url": ""}
            st = await punchlines.build(secs, secs, bool(user["ai_lines"])) if user["style"] else None
            await send_reminder(context, chat_id, sample, user["tz"], f"Reminder ({fmt_offset(secs)} before): ", st, False, bool(user["memes"]))
            return
        user = db.get_user(chat_id)
        text, kb = settings_panel(user)
        await q.answer("Saved")
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
    elif action == "mute":
        cid = int(parts[1])
        name = next((e["course"] for e in db.pending_events(chat_id, include_muted=True) if e["course_id"] == cid), "course")
        db.mute_course(chat_id, cid, name)
        await q.answer("Course muted. /unmute to undo.", show_alert=True)
        await q.edit_message_reply_markup(None)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the button handler; tapping a button that would change nothing ("message is not modified") is not an error."""
    try:
        await _on_button(update, context)
    except BadRequest as ex:
        if "not modified" not in str(ex).lower():
            raise
        try:
            await update.callback_query.answer()  # stop the button's loading spinner
        except Exception:
            pass


async def ping_job(context: ContextTypes.DEFAULT_TYPE):
    """Hit our own public /health URL so hosts that sleep idle web services (Render free) stay awake."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.get(f"{WEBAPP_URL}/health")
    except Exception:
        log.warning("self-ping failed")


def _public_commands():
    return [
        BotCommand("start", "Start and show the buttons"),
        BotCommand("today", "Due today"),
        BotCommand("week", "Due this week"),
        BotCommand("upcoming", "Upcoming deadlines"),
        BotCommand("overdue", "Overdue tasks"),
        BotCommand("tools", "More tools"),
        BotCommand("stats", "My points and streak"),
        BotCommand("leaderboard", "Friends leaderboard"),
        BotCommand("settings", "Settings (ON/OFF switches)"),
        BotCommand("help", "Help"),
    ]


async def post_init(app: Application):
    log.warning("ADMIN CLAIM CODE (this run only): %s  ->  send  /admin %s  to your bot to become admin", ADMIN_CODE, ADMIN_CODE)
    await app.bot.set_my_commands([
        BotCommand("start", "Start and show the buttons"),
        BotCommand("today", "Due today"),
        BotCommand("week", "Due this week"),
        BotCommand("upcoming", "Upcoming deadlines"),
        BotCommand("overdue", "Overdue tasks"),
        BotCommand("reminders", "Change reminder times"),
        BotCommand("stats", "My points and streak"),
        BotCommand("fun", "Random meme or GIF"),
        BotCommand("leaderboard", "Friends leaderboard"),
        BotCommand("test", "Preview a reminder"),
        BotCommand("settings", "Settings (ON/OFF switches)"),
        BotCommand("menu", "Show the buttons again"),
        BotCommand("help", "Help"),
    ])
    global BOT_USERNAME
    if not BOT_USERNAME:
        try:
            BOT_USERNAME = (await app.bot.get_me()).username or ""
        except Exception:
            log.warning("could not read bot username")
    app.bot_data["gif_check"] = asyncio.create_task(punchlines.verify_gifs())  # keep a reference
    if not WEBAPP_URL:
        log.info("WEBAPP_URL not set: Connect button disabled, /calendar still works")
        return
    runner = web.AppRunner(webapp.create_app(BOT_TOKEN, lambda cid, url: link_calendar(app, cid, url), DEFAULT_SITE, share_url(), GROUP_URL, is_linked))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["web_runner"] = runner
    log.info("Mini App server on port %s, public URL %s", PORT, WEBAPP_URL)


async def post_shutdown(app: Application):
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


MENU_ACTIONS.update({"🧰 Tools": cmd_tools, "🧠 Plan my week": cmd_plan, "☑️ Tick done": cmd_tick})


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    handlers = {
        "start": cmd_start, "help": cmd_help, "menu": cmd_menu, "test": cmd_test,
        "login": cmd_login, "token": cmd_token, "calendar": cmd_calendar,
        "logout": cmd_logout, "sync": cmd_sync, "today": cmd_today, "week": cmd_week,
        "upcoming": cmd_upcoming, "overdue": cmd_overdue, "reminders": cmd_reminders,
        "digest": cmd_digest, "quiet": cmd_quiet, "timezone": cmd_timezone,
        "mute": cmd_mute, "unmute": cmd_unmute, "settings": cmd_settings, "style": cmd_style,
        "admin": cmd_admin, "tools": cmd_tools, "plan": cmd_plan, "tick": cmd_tick, "mydata": cmd_mydata, "exam": cmd_exam, "exams": cmd_exams, "clearexams": cmd_clearexams, "setgroup": cmd_setgroup, "myid": cmd_myid, "msg": cmd_msg, "cancel": cmd_cancel, "stats": cmd_stats, "fun": cmd_fun, "leaderboard": cmd_leaderboard, "broadcast": cmd_broadcast, "botstats": cmd_botstats, "addmeme": cmd_addmeme_help,
    }
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.ANIMATION | filters.Sticker.ALL | filters.PHOTO, on_media))

    app.job_queue.run_repeating(sync_job, interval=SYNC_MINUTES * 60, first=30)
    app.job_queue.run_repeating(reminder_job, interval=60, first=45)
    app.job_queue.run_repeating(digest_job, interval=60, first=50)
    app.job_queue.run_repeating(pool_job, interval=60, first=20)
    app.job_queue.run_repeating(weekly_job, interval=60, first=55)
    for _fn, _first in ((exam_job, 58), (quote_job, 59), (group_job, 61), (announce_job, 62)):
        app.job_queue.run_repeating(_fn, interval=60, first=_first)
    app.job_queue.run_daily(backup_job, time=dtime(21, 30))  # 03:00 IST
    app.add_error_handler(on_error)
    if os.getenv("RENDER") and WEBAPP_URL:
        app.job_queue.run_repeating(ping_job, interval=600, first=120)
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
