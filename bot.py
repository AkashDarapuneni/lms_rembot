"""Moodle assignment reminder bot for Telegram."""
import asyncio
import html
import logging
import os
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
log = logging.getLogger("moodlebot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
fernet = Fernet(os.environ["SECRET_KEY"].encode())
db = DB(os.getenv("DATABASE_URL") or os.getenv("DB_PATH", "moodle_bot.sqlite3"))
SYNC_MINUTES = int(os.getenv("SYNC_MINUTES", "15"))
DEFAULT_SITE = os.getenv("DEFAULT_SITE", "lms.kluniversity.in")
ALLOW_ANY_SITE = os.getenv("ALLOW_ANY_SITE", "0") == "1"
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")  # public HTTPS URL of this server
PORT = int(os.getenv("PORT", "8080"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
SHARE_DEADLINES = os.getenv("GEMINI_SHARE_DEADLINES", "1") != "0"
punchlines.init(db)

def esc(text) -> str:
    """Escape for Telegram HTML (only & < > matter; quotes and apostrophes are fine as they are)."""
    return html.escape(str(text), quote=False)

HELP = (
    "<b>What this bot does</b>\n"
    "It reads your deadlines from LMS and pings you before each one, so you never miss a submission.\n\n"
    "<b>Buttons under the chat box</b>\n"
    "📅 Today / 📆 This week / ⏳ Upcoming / ⚠️ Overdue: your task lists\n"
    "⚙️ Settings: tap the ON/OFF switches\n"
    "🏆 My stats: your points, level and streak\n"
    "🎲 Fun: a random meme, GIF or dialogue\n\n"
    "<b>What each setting means</b>\n"
    "🎬 <b>Mass mode</b>: a Telugu hero dialogue comes with every reminder\n"
    "🖼 <b>Memes and GIFs</b>: a funny GIF, sticker or animation comes with reminders\n"
    "🤖 <b>AI dialogues</b>: Gemini AI writes extra new dialogues so you rarely see the same one\n"
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
BTN_STATS, BTN_FUN, BTN_CONNECT = "🏆 My stats", "🎲 Fun", "🔗 Connect"
MENU_ROWS = [
    [BTN_TODAY, BTN_WEEK, BTN_UPCOMING],
    [BTN_OVERDUE, BTN_SETTINGS, BTN_HELP],
    [BTN_STATS, BTN_FUN, BTN_CONNECT],
]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t) for t in row] for row in MENU_ROWS],
        resize_keyboard=True,
        is_persistent=True,
    )


def event_keyboard(e) -> InlineKeyboardMarkup:
    eid = e["event_id"]
    rows = [
        [
            InlineKeyboardButton("Submitted", callback_data=f"done:{eid}"),
            InlineKeyboardButton("Snooze 1h", callback_data=f"snz:{eid}:60"),
            InlineKeyboardButton("Tomorrow", callback_data=f"snz:{eid}:1440"),
        ],
        [InlineKeyboardButton("Mute course", callback_data=f"mute:{e['course_id']}")],
    ]
    if e["url"]:
        rows.insert(0, [InlineKeyboardButton("Open in Moodle", url=e["url"])])
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
    return len(fetched_ids)


async def sync_job(context: ContextTypes.DEFAULT_TYPE):
    for user in db.all_users():
        try:
            await sync_user(context.application, user)
        except moodle.MoodleError as ex:
            log.warning("sync failed for %s: %s", user["chat_id"], ex)
        except Exception:
            log.exception("sync crashed for %s", user["chat_id"])


# ---------- reminders ----------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    for user in db.all_users():
        chat_id, tz = user["chat_id"], user["tz"]
        offsets = parse_offsets(user["offsets"] or DEFAULT_OFFSETS)
        quiet = in_quiet_hours(now, tz, user["quiet_start"], user["quiet_end"])
        for e in db.pending_events(chat_id, since=now - 3600):
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
                    await send_reminder(context, chat_id, e, tz, "Snooze over: ", style, memes=bool(user["memes"]))
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
            await send_reminder(context, chat_id, e, tz, f"Reminder ({fmt_offset(to_send)} before): ", style, memes=bool(user["memes"]))


async def send_reminder(context, chat_id, e, tz, header, style=None, keyboard=True, memes=False):
    text = reminder_text(e, tz, header, style)
    markup = event_keyboard(e) if keyboard else None
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
        await bot.send_message(chat_id, f"{title}\nNothing here. Enjoy the free time.", reply_markup=main_keyboard())
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
    if user and user["token_enc"]:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚪 Logout", callback_data="lo:ask")]])
        await update.message.reply_text("Menu buttons are ready 👇", reply_markup=main_keyboard())
        await update.message.reply_text(
            "✅ <b>You are already logged in.</b>\n\nDo you want to log out?",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        return
    msg = await update.message.reply_text("🎬 Starting...", reply_markup=main_keyboard())
    for frame in ("🎬 Starting.. ⏳", "🎬 Starting... 🔥", "🎬 Ready! 🚀"):  # tiny loading animation
        await asyncio.sleep(0.5)
        try:
            await msg.edit_text(frame)
        except Exception:
            break
    if WEBAPP_URL:
        await update.message.reply_text(WELCOME_NEW, parse_mode=ParseMode.HTML, reply_markup=connect_markup())
    else:
        await update.message.reply_text(
            f"Open https://{DEFAULT_SITE}/calendar/export.php, choose All events, click Get calendar URL, "
            "then send: /calendar <your link>"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=main_keyboard()
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buttons are on. Use them under the chat box.", reply_markup=main_keyboard())


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
        FX_PARTY, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(),
    )


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
        FX_PARTY, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(),
    )


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
        [
            InlineKeyboardButton("🔔 Reminder times", callback_data="set:times"),
            InlineKeyboardButton("🧪 Test", callback_data="set:test"),
        ],
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
        f"🔥 Streak: {st['streak']} day(s)  (best {st['best_streak']})\n\n"
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    BTN_STATS: cmd_stats, BTN_FUN: cmd_fun, BTN_CONNECT: cmd_connect,
}
_chat_times: dict[int, list[float]] = {}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu button taps arrive as text and run their command; any other text is a question for Gemini."""
    text = (update.message.text or "").strip()
    action = MENU_ACTIONS.get(text)
    if action:
        await action(update, context)
        return
    if not gemini.enabled():
        await update.message.reply_text("Use the buttons below or /help. (AI answers are off: the admin has not set GEMINI_API_KEY.)")
        return
    chat_id = update.effective_chat.id
    now = time.time()
    recent = [t for t in _chat_times.get(chat_id, []) if now - t < 60]
    if len(recent) >= 6:
        await update.message.reply_text("Slow down a little 😅 Try again in a minute.")
        return
    _chat_times[chat_id] = recent + [now]
    user = db.get_user(chat_id)
    tz = user["tz"] if user else "Asia/Kolkata"
    deadlines = "The student is not connected, so you know no deadlines."
    if user and user["token_enc"]:
        if SHARE_DEADLINES:
            evs = db.pending_events(chat_id, since=int(now) - 86400)[:15]
            deadlines = ("Pending tasks:\n" + "\n".join(f"- {e['name']} ({e['course']}) due {fmt_due(e['due_ts'], tz)}" for e in evs)) if evs else "The student has no pending tasks."
        else:
            deadlines = "You have no access to the student's deadlines."
    await context.bot.send_chat_action(chat_id, "typing")
    history = context.user_data.setdefault("hist", [])
    try:
        stamp = datetime.now(ZoneInfo(tz)).strftime("%a %d %b %Y %H:%M")
    except ZoneInfoNotFoundError:
        stamp = datetime.now().strftime("%a %d %b %Y %H:%M")
    answer = await gemini.chat(text, history, deadlines, DEFAULT_SITE, tz, stamp)
    if not answer:
        await update.message.reply_text("I couldn't think of an answer right now. Try again, or use the buttons.")
        return
    history += [("user", text[:800]), ("model", answer[:800])]
    del history[:-8]
    await update.message.reply_text(answer[:3500])


async def pool_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await punchlines.refill_once()
    except Exception:
        log.exception("pool refill failed")


# ---------- inline buttons ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    parts = q.data.split(":")
    action = parts[0]
    if action == "done":
        ev = db.get_event(chat_id, int(parts[1]))
        db.mark_done(chat_id, int(parts[1]))
        tz = (db.get_user(chat_id) or {"tz": "Asia/Kolkata"})["tz"]
        try:
            zone = ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("Asia/Kolkata")
        today = datetime.now(zone).date()
        early = bool(ev) and ev["due_ts"] - time.time() > 86400
        st = db.record_submit(chat_id, 15 if early else 10, early, today.isoformat(), (today - timedelta(days=1)).isoformat())
        gained = 15 if early else 10
        await q.answer(f"🎉 Awesome! Submitted!\n+{gained} points  •  🔥 {st['streak']} day streak", show_alert=True)
        try:
            await q.edit_message_text(
                (q.message.text or q.message.caption or "") + f"\n\n✅ <b>Submitted!</b> +{gained} points", parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await q.edit_message_reply_markup(None)
        await send_fx(
            context.bot, chat_id,
            f"🎉 <b>Task done!</b> 🥳\n{level_for(st['points'])}  •  ⭐ {st['points']} pts  •  🔥 {st['streak']}-day streak",
            FX_PARTY, parse_mode=ParseMode.HTML,
        )
    elif action == "snz":
        until = int(time.time()) + int(parts[2]) * 60
        db.set_snooze(chat_id, int(parts[1]), until)
        await q.answer("Snoozed")
        await q.edit_message_reply_markup(None)
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
                          FX_SAD, parse_mode=ParseMode.HTML)
        else:
            await q.answer("Cancelled")
            await q.edit_message_text("Okay, you stay logged in ✅")
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
        if key in ("style", "memes", "ai_lines"):
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


async def ping_job(context: ContextTypes.DEFAULT_TYPE):
    """Hit our own public /health URL so hosts that sleep idle web services (Render free) stay awake."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.get(f"{WEBAPP_URL}/health")
    except Exception:
        log.warning("self-ping failed")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Start and show the buttons"),
        BotCommand("today", "Due today"),
        BotCommand("week", "Due this week"),
        BotCommand("upcoming", "Upcoming deadlines"),
        BotCommand("overdue", "Overdue tasks"),
        BotCommand("reminders", "Change reminder times"),
        BotCommand("stats", "My points and streak"),
        BotCommand("fun", "Random meme or GIF"),
        BotCommand("test", "Preview a reminder"),
        BotCommand("settings", "Settings (ON/OFF switches)"),
        BotCommand("menu", "Show the buttons again"),
        BotCommand("help", "Help"),
    ])
    app.bot_data["gif_check"] = asyncio.create_task(punchlines.verify_gifs())  # keep a reference
    if not WEBAPP_URL:
        log.info("WEBAPP_URL not set: Connect button disabled, /calendar still works")
        return
    runner = web.AppRunner(webapp.create_app(BOT_TOKEN, lambda cid, url: link_calendar(app, cid, url), DEFAULT_SITE))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["web_runner"] = runner
    log.info("Mini App server on port %s, public URL %s", PORT, WEBAPP_URL)


async def post_shutdown(app: Application):
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    handlers = {
        "start": cmd_start, "help": cmd_help, "menu": cmd_menu, "test": cmd_test,
        "login": cmd_login, "token": cmd_token, "calendar": cmd_calendar,
        "logout": cmd_logout, "sync": cmd_sync, "today": cmd_today, "week": cmd_week,
        "upcoming": cmd_upcoming, "overdue": cmd_overdue, "reminders": cmd_reminders,
        "digest": cmd_digest, "quiet": cmd_quiet, "timezone": cmd_timezone,
        "mute": cmd_mute, "unmute": cmd_unmute, "settings": cmd_settings, "style": cmd_style,
        "stats": cmd_stats, "fun": cmd_fun, "botstats": cmd_botstats, "addmeme": cmd_addmeme_help,
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
    if os.getenv("RENDER") and WEBAPP_URL:
        app.job_queue.run_repeating(ping_job, interval=600, first=120)
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
