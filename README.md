# Moodle Assignment Reminder Bot (Telegram)

Sends Telegram reminders before Moodle deadlines. Built for lms.kluniversity.in (set `DEFAULT_SITE` for another site).

## How students connect
1. `/start` shows a **Connect** button. It opens a login page inside Telegram (a Mini App).
2. They sign in once. The server fetches their calendar link and stores it with their Telegram ID (encrypted). The password is never stored.
3. Fallbacks: `/calendar <link>` (paste the link from `/calendar/export.php`), `/token <token>`, `/login <username> <password>`.

## Features
- Default reminders at 24h, 6h, 2h, 1h, 50m and 10m before each deadline, changeable per user via `/reminders`
- Telugu "mass hero" style punchlines that get more intense as the deadline nears (`/style on|off`)
- Daily digest, quiet hours, timezone, mute courses
- Buttons on each reminder: Open in Moodle, Submitted, Snooze 1h, Tomorrow, Mute course
- Auto sync every 15 min with alerts for new and changed deadlines
- Views: `/today /week /upcoming /overdue /settings`

## Hosting
See `RENDER.md` (free Render + cron ping + external Postgres) or `DEPLOY.md` (Oracle Cloud server).

## Setup
1. Create a bot with @BotFather, copy the token.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`; fill `TELEGRAM_BOT_TOKEN` and generate `SECRET_KEY` (command inside the file).
4. For the Connect button, set `WEBAPP_URL` to a public **HTTPS** address that reaches this server's `PORT` (8080).
   For testing: `cloudflared tunnel --url http://localhost:8080` and paste the https URL. For production use a VPS with a domain and a reverse proxy (Caddy/nginx).
5. `python bot.py`

Before going live, run `python check_login.py` with your own account to confirm the link fetch works on your site.

## Notes
- Calendar mode cannot see submissions, so students tap **Submitted**. With `/token` or `/login` (Moodle web service) submissions are detected automatically.
- Mini App login requests are verified with Telegram's signature, rate limited (5 tries per 10 min), and only accept the configured site.
- Run 24/7 with systemd, pm2, Docker, or a host like Railway/Render.
