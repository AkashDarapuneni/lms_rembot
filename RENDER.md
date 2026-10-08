# Deploy on Render (free) + keep it awake + free database

Why three parts: Render's free web service **sleeps after 15 minutes without incoming web traffic**, and its **disk is wiped on every restart**. So you need (1) a keep-awake ping and (2) a free external database. Check Render's and your database host's current free-plan rules before you rely on them, they change.

## 1. Free database (stores Telegram ID + LMS user ID)
Use a free Postgres host, for example **Supabase** or **Neon**. Create a project and copy the connection string (it starts with `postgresql://`).
- Supabase: Project > Connect > **Session pooler** string (Render needs the IPv4-compatible pooler). Replace `[YOUR-PASSWORD]` with your database password.
- Do not use Render's own free Postgres: it expires after 30 days.
- Neon pauses its compute when idle and has a monthly compute-hours cap on the free plan. Because this bot checks reminders every minute, it may use up that cap. If you pick Neon, watch its usage page.

The bot creates its tables automatically on first start.

## 2. Create the Render service
1. Revoke any old bot token in @BotFather (`/revoke`) and keep the **new** one private.
2. On render.com: **New > Blueprint**, connect your GitHub repo `lms_rembot`. It reads `render.yaml`.
3. When asked, fill the secret values:
   - `TELEGRAM_BOT_TOKEN`: your new bot token
   - `SECRET_KEY`: generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and **save a copy somewhere safe** (if lost, every student must reconnect)
   - `DATABASE_URL`: the connection string from step 1
4. Deploy. When the log says "Mini App server on port...", it is running. Your address looks like `https://lms-rembot.onrender.com` (Render sets `RENDER_EXTERNAL_URL` automatically, so the Connect button works without extra settings).

## 3. Keep it awake (the cron job)
The bot already pings its own `/health` address every 10 minutes while awake. Add an outside ping as a backup, so it also wakes after a restart:
1. Make a free account at **cron-job.org**.
2. Create a job: URL `https://YOUR-APP.onrender.com/health`, schedule **every 10 minutes** (or 5).
3. Save. Test by opening the `/health` URL in a browser: it should show `ok`.

A free service has about 750 instance hours per month, which covers one service running all month, so keep just one service on the account.

## 4. Test
Open your bot in Telegram, send `/start`, tap **Connect**, log in. Then check `/settings`: it shows your Telegram ID and LMS user ID.

## Notes
- If a deploy or restart happens, reminders resume within a minute or two of the service waking, and anything that was due while it slept is sent once as the most urgent reminder.
- Free hosting can still miss reminders during outages. For something students depend on, a small always-on server is more reliable.
