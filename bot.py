"""Moodle assignment reminder bot for Telegram."""
import html
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

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

esc = html.escape

HELP = (
    "<b>Moodle Reminder Bot</b>\n\n"
    "<b>Connect (takes 1 minute)</b>\n"
    f"1. Open https://{DEFAULT_SITE}/calendar/export.php and log in\n"
    "2. Events to export: <b>All events</b>. Time period: <b>Recent and next 60 days</b> (or custom range)\n"
    "3. Click <b>Get calendar URL</b>, copy the link\n"
    "4. Send it here: /calendar &lt;your link&gt;  (I delete your message right away)\n\n"
    "Other ways: /token &lt;token&gt; or /login &lt;username&gt; &lt;password&gt; (needs Moodle web service)\n"
    "/logout - remove your data\n\n"
    "<b>View</b>\n"
    "/today  /week  /upcoming  /overdue  /sync\n\n"
    "<b>Customize</b>\n"
    "/reminders 1d,6h,2h,1h,50m,10m - when to remind before a deadline (this is the default)\n"
    "/style on|off - Telugu mass-style punchlines in reminders\n"
    "/digest 08:00 (or /digest off) - daily summary time\n"
    "/quiet 23:00 07:00 (or /quiet off) - no reminders at night\n"
    "/timezone Asia/Kolkata\n"
    "/mute &lt;text&gt; - mute a course by name  |  /unmute &lt;text&gt;\n"
    "/settings - show current settings"
)


# ---------- helpers ----------
def get_token(user) -> str:
    return fernet.decrypt(user["token_enc"].encode()).decode()


def event_text(e, tz: str, header: str = "") -> str:
    left = fmt_delta(e["due_ts"] - int(time.time()))
    return (
        f"{header}<b>{esc(e['name'])}</b>\n"
        f"{esc(e['course'])}\n"
        f"Due: {fmt_due(e['due_ts'], tz)} ({left})"
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
                    line = punchlines.pick(e["due_ts"] - now) if user["style"] else ""
                    await send_reminder(context, chat_id, e, tz, "Snooze over: ", line)
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
            line = punchlines.pick(e["due_ts"] - now) if user["style"] else ""
            await send_reminder(context, chat_id, e, tz, f"Reminder ({fmt_offset(to_send)} before): ", line)


async def send_reminder(context, chat_id, e, tz, header, line=""):
    try:
        text = event_text(e, tz, header)
        if line:
            text += f"\n\n<i>{esc(line)}</i>"
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=event_keyboard(e),
        )
    except Exception:
        log.exception("could not send reminder to %s", chat_id)


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
        await bot.send_message(chat_id, f"{title}\nNothing here. Enjoy the free time.")
        return
    lines = [f"<b>{esc(title)}</b>", ""]
    for i, e in enumerate(rows, 1):
        lines.append(
            f"{i}. <b>{esc(e['name'])}</b>\n   {esc(e['course'])}\n"
            f"   {fmt_due(e['due_ts'], user['tz'])} ({fmt_delta(e['due_ts'] - now)})"
        )
    await bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"Connect {DEFAULT_SITE}", web_app=WebAppInfo(url=f"{WEBAPP_URL}/connect"))]]
        )
        await update.message.reply_text(
            f"Welcome! Tap the button, sign in to {DEFAULT_SITE}, and I'll fetch your deadlines automatically.",
            reply_markup=kb,
        )
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def link_calendar(app: Application, chat_id: int, url: str):
    """Called by the Mini App: store the calendar link with the Telegram id and run the first sync."""
    url = ical.validate_url(url, None if ALLOW_ANY_SITE else DEFAULT_SITE)
    site = "https://" + url.split("/")[2]
    db.save_login(chat_id, site, fernet.encrypt(url.encode()).decode(), "ics", ical.userid_from_url(url))
    count = await sync_user(app, db.get_user(chat_id), notify=False)
    await app.bot.send_message(
        chat_id,
        f"Connected to {DEFAULT_SITE}. Found {count} pending task(s). Reminders are on.\n"
        "Try /upcoming. Tap Submitted on a reminder when you finish a task.",
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
    await context.bot.send_message(
        chat_id,
        f"Connected as {who}. Found {count} pending task(s).\n"
        f"Default reminders: {user['offsets']}. Change with /reminders.{note}",
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
    db.delete_user(update.effective_chat.id)
    await update.message.reply_text("Done. Your token and all stored data were deleted.")


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
    if arg not in ("on", "off"):
        await update.message.reply_text(
            f"Mass-style Telugu punchlines are {'on' if user['style'] else 'off'}. Use /style on or /style off"
        )
        return
    db.set_field(user["chat_id"], "style", 1 if arg == "on" else 0)
    await update.message.reply_text("Mass mode ON. Reminders will come with punchlines." if arg == "on" else "Punchlines off. Plain reminders only.")


async def cmd_settings(update, context):
    user = await require_login(update)
    if not user:
        return
    quiet = f"{user['quiet_start']} to {user['quiet_end']}" if user["quiet_start"] else "off"
    muted = ", ".join(m["course"] for m in db.muted_courses(user["chat_id"])) or "none"
    await update.message.reply_text(
        f"Telegram ID: {user['chat_id']}\nLMS user ID: {user['moodle_userid'] or 'unknown'}\n"
        f"Site: {user['site_url']}\nTimezone: {user['tz']}\nReminders: {user['offsets']}\n"
        f"Daily digest: {user['digest_time'] or 'off'}\nQuiet hours: {quiet}\nMuted courses: {muted}\n"
        f"Punchlines: {'on' if user['style'] else 'off'}"
    )


# ---------- inline buttons ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    parts = q.data.split(":")
    action = parts[0]
    if action == "done":
        db.mark_done(chat_id, int(parts[1]))
        await q.answer("Marked as submitted")
        await q.edit_message_text(q.message.text + "\n\nMarked as submitted.")
    elif action == "snz":
        until = int(time.time()) + int(parts[2]) * 60
        db.set_snooze(chat_id, int(parts[1]), until)
        await q.answer("Snoozed")
        await q.edit_message_reply_markup(None)
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
        "start": cmd_start, "help": cmd_start, "login": cmd_login, "token": cmd_token, "calendar": cmd_calendar,
        "logout": cmd_logout, "sync": cmd_sync, "today": cmd_today, "week": cmd_week,
        "upcoming": cmd_upcoming, "overdue": cmd_overdue, "reminders": cmd_reminders,
        "digest": cmd_digest, "quiet": cmd_quiet, "timezone": cmd_timezone,
        "mute": cmd_mute, "unmute": cmd_unmute, "settings": cmd_settings, "style": cmd_style,
    }
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))

    app.job_queue.run_repeating(sync_job, interval=SYNC_MINUTES * 60, first=30)
    app.job_queue.run_repeating(reminder_job, interval=60, first=45)
    app.job_queue.run_repeating(digest_job, interval=60, first=50)
    if os.getenv("RENDER") and WEBAPP_URL:
        app.job_queue.run_repeating(ping_job, interval=600, first=120)
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
