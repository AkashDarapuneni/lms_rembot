#!/usr/bin/env bash
# One-time setup on an Ubuntu server (Oracle Cloud). Usage: ./deploy/setup.sh yourname.duckdns.org
set -euo pipefail
DOMAIN="${1:?Usage: ./deploy/setup.sh yourname.duckdns.org}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

echo "==> Installing packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl gpg debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y caddy
fi

echo "==> Python environment"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "==> Creating .env"
  read -rsp "Paste your Telegram bot token (hidden): " BOT_TOKEN; echo
  KEY="$(./venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  umask 077
  cat > .env <<EOT
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
SECRET_KEY=$KEY
DB_PATH=$APP_DIR/data/moodle_bot.sqlite3
SYNC_MINUTES=15
DEFAULT_SITE=lms.kluniversity.in
ALLOW_ANY_SITE=0
WEBAPP_URL=https://$DOMAIN
PORT=8080
EOT
  echo "IMPORTANT: back up the SECRET_KEY line in $APP_DIR/.env somewhere safe. If it is lost, every student must reconnect."
fi
mkdir -p data

echo "==> Opening ports 80 and 443 in the server firewall"
sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT
sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
sudo apt-get install -y iptables-persistent >/dev/null 2>&1 || true
sudo netfilter-persistent save >/dev/null 2>&1 || true

echo "==> HTTPS reverse proxy (Caddy)"
printf '%s {\n    reverse_proxy localhost:8080\n}\n' "$DOMAIN" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl enable --now caddy
sudo systemctl reload caddy

echo "==> Bot service"
sed -e "s|__USER__|$(whoami)|" -e "s|__APP_DIR__|$APP_DIR|g" deploy/moodlebot.service | sudo tee /etc/systemd/system/moodlebot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now moodlebot
sleep 3
sudo systemctl --no-pager status moodlebot | head -n 8
echo
echo "Done. Open Telegram, send /start to your bot. Logs: sudo journalctl -u moodlebot -f"
