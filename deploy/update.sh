#!/usr/bin/env bash
# Pull the latest code from GitHub and restart. Usage: ./deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull
./venv/bin/pip install -q -r requirements.txt
sudo systemctl restart moodlebot
echo "Updated and restarted."
