# Deploy on Render (free) + keep it awake + free database

Why three parts: Render's free web service **sleeps after 15 minutes without incoming web traffic**, and its **disk is wiped on every restart**. So you need (1) a keep-awake ping and (2) a free external database. Check Render's and your database host's current free-plan rules before you rely on them, they change.

## 1. Free database: TiDB Cloud (stores Telegram ID + LMS user ID)
TiDB Cloud Starter has a free tier (per its docs: 5 GiB storage and 50 million request units a month, no credit card to start, no expiry stated). Check pingcap.com for current terms.
1. Sign up at tidbcloud.com and create a **Starter** instance.
2. Click **Connect**, choose **General** (or PyMySQL), and click **Generate password**. Save the password.
3. Note the **host** (like `gateway01.<region>.prod.aws.tidbcloud.com`), **port** (4000), **username** (like `abc123.root`) and database name (`test` by default).
4. Build your `DATABASE_URL` like this:
   `mysql://USERNAME:PASSWORD@HOST:4000/test`
   If the password has special characters (`@ : / # ?`), replace each with its URL code, e.g. `@` becomes `%40`, `:` becomes `%3A`, `/` becomes `%2F`.
The bot creates its tables automatically on first start and connects with TLS.

Postgres (Supabase, Neon) also works: use a `postgresql://...` string instead. Do not use Render's own free Postgres, which expires after 30 days.

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

## 5. Optional: GIFs and Gemini
**GIFs.** Open `punchlines.json`. Each reminder stage (1d, 6h, 2h, 1h, 50m, 10m) has lines with a `quote`, a `note` and a `gif`. A `gif` must be a **direct link to a .gif or .mp4 file** (open the link in a browser: it should show only the animation, not a web page). When the bot starts it checks every GIF link and writes `GIF link not usable` in the Render log for any that fail; those lines are then sent as text only. After you edit the file, commit and push to GitHub and Render redeploys.

**Gemini.** Get a free API key from Google AI Studio, then in Render > Environment add `GEMINI_API_KEY` (never put it in GitHub or in a chat). Optional: `GEMINI_MODEL` (default `gemini-2.5-flash`; change it if Google retires that name). Gemini writes one fresh line per stage and the bot reuses it for 30 minutes, so very few API calls are used. If Gemini is off, over its free quota or slow, the fixed lines from `punchlines.json` are used.

**Check it works:** in Telegram send `/test 1h` (or 1d, 6h, 2h, 50m, 10m) to preview a reminder right away.

## Memes, GIFs, stickers and admin tools
Add `ADMIN_IDS` (your numeric Telegram ID, from @userinfobot) in Render > Environment. Then send the bot any GIF, sticker or photo with the caption `meme any` (or `meme 1h`, `meme 10m` ...) and it will use it in reminders and in the 🎲 Fun button. Without any saved memes the bot sends Telegram's animated dice. `/botstats` shows users, dialogue pool size and saved memes.
Gemini answers use the student's pending task names as context; set `GEMINI_SHARE_DEADLINES=0` to disable that.
