# Put the bot online (free, Oracle Cloud + GitHub)

## 0. Token safety (do this first)
Never put your bot token in GitHub or share it in chats. If it was ever shared, open @BotFather, send `/revoke`, pick your bot, and use the **new** token below. `.env` is already in `.gitignore`, so it will not be uploaded.

## 1. Push the code to GitHub (on your Windows PC)
1. Install Git from git-scm.com. Create an **empty** repo on github.com (no README). A private repo is best.
2. In the project folder, open Command Prompt:
```
git init
git add .
git status
```
Check that `.env` is NOT in the list. Then:
```
git commit -m "Moodle reminder bot"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/YOUR_REPO.git
git push -u origin main
```

## 2. Create the free server (Oracle Cloud)
1. Sign up at cloud.oracle.com/free. A card is used for identity verification. Pick your home region carefully, it cannot be changed later.
2. Menu > Compute > Instances > **Create instance**:
   - Image: **Ubuntu 22.04**
   - Shape: an **Always Free eligible** one (VM.Standard.E2.1.Micro, or Ampere A1 if available)
   - Download the SSH private key when asked.
3. Open the web ports: instance page > Subnet > **Security List** > Add Ingress Rules: source `0.0.0.0/0`, TCP, destination port `80`, and another rule for `443`.
4. Note the instance's **public IP**.

## 3. Free web address (DuckDNS)
Go to duckdns.org, sign in, create a name (e.g. `kluremind`), and set its IP to the server's public IP. Your address is `kluremind.duckdns.org`.

## 4. Install on the server
From your PC (replace the key file and IP):
```
ssh -i path\to\key.key ubuntu@SERVER_IP
```
On the server:
```
git clone https://github.com/YOUR_NAME/YOUR_REPO.git
cd YOUR_REPO
./deploy/setup.sh kluremind.duckdns.org
```
It asks for your bot token (hidden), creates the encryption key, sets up HTTPS and starts the bot. It restarts by itself after crashes or reboots.

**Back up the `SECRET_KEY` line from `.env`** (`cat .env`). If it is lost, every student has to reconnect.

## 5. Check and use
- Telegram: send `/start` to your bot and tap **Connect**.
- Logs: `sudo journalctl -u moodlebot -f`
- After you push code changes to GitHub, on the server run `./deploy/update.sh`.

## Database
Students are stored in `data/moodle_bot.sqlite3` on the server disk: Telegram ID (primary key), LMS user ID, and the encrypted calendar link. Copy that file now and then as a backup, e.g. `cp data/moodle_bot.sqlite3 ~/backup-$(date +%F).sqlite3`.

## Keeping the free server
Oracle can reclaim Always Free servers that sit nearly idle for 7 days (see Oracle's Always Free docs). Keep backups of `.env` and the database.
