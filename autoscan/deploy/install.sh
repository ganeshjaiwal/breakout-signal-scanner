#!/usr/bin/env bash
# Run ON THE SERVER, from the unpacked bundle:
#   mkdir -p ~/day-trader && tar -xzf ~/day-trader-autoscan.tar.gz -C ~/day-trader
#   bash ~/day-trader/autoscan/deploy/install.sh
# Safe to run again (after an update): it replaces only its own cron line.
set -euo pipefail
A=$(cd "$(dirname "$0")/.." && pwd)                 # .../autoscan
ENVF="$HOME/.config/day-trader/telegram.env"
say() { printf '\n== %s\n' "$*"; }

say "Checking Python (3.9 or newer, for zoneinfo)"
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 is missing. Install it: sudo apt install -y python3"; exit 1; }
"$PY" - <<'PYEOF' || { echo "Install the time-zone data: sudo apt install -y tzdata"; exit 1; }
import sys
from zoneinfo import ZoneInfo
assert sys.version_info >= (3, 9), "Python 3.9+ needed"
ZoneInfo("Asia/Kolkata"); ZoneInfo("America/New_York")
print("ok", sys.version.split()[0])
PYEOF

say "Checking cron"
command -v crontab >/dev/null || { echo "cron is missing. Install it: sudo apt install -y cron && sudo systemctl enable --now cron"; exit 1; }
(systemctl is-active --quiet cron || systemctl is-active --quiet crond) 2>/dev/null \
  || echo "warning: the cron service does not look active. Run: sudo systemctl enable --now cron"

say "Telegram settings ($ENVF)"
mkdir -p "$(dirname "$ENVF")"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<'TEOF'
# Telegram alerts for Day trader autoscan.
# 1. In Telegram, talk to @BotFather, send /newbot, copy the token here.
# 2. Send any message to your new bot, then open https://api.telegram.org/bot<TOKEN>/getUpdates
#    and copy the number after "chat":{"id": here.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TEOF
fi
chmod 600 "$ENVF"
if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENVF" && grep -qE '^TELEGRAM_CHAT_ID=.+' "$ENVF"; then
  echo "filled in"
else
  echo "NOT filled in yet: edit it (nano $ENVF), then run this script again."
  echo "Without it the server scans but cannot tell you anything."
fi

say "Installing the cron job (every 5 minutes, every day; the runner skips weekends and holidays)"
LINE="*/5 * * * * cd $A && $PY autoscan.py tick >> $A/cron.log 2>&1"
{ crontab -l 2>/dev/null | grep -v 'autoscan.py tick' | grep -v '^# Day trader autoscan' || true
  echo "# Day trader autoscan (installed by deploy/install.sh)"; echo "$LINE"; } | crontab -
crontab -l | grep 'autoscan.py tick'

say "Test scan (daily setups, both markets; notifications are not sent)"
cd "$A"
AUTOSCAN_TEST_DIR=$(mktemp -d)
for M in NSE US; do
  AUTOSCAN_DATA="$AUTOSCAN_TEST_DIR" "$PY" autoscan.py run --market $M --mode daily --no-notify || true
done
rm -rf "$AUTOSCAN_TEST_DIR"

say "Test message"
"$PY" autoscan.py test-notify || echo "No message sent (Telegram not set up yet)."

say "What runs next"
"$PY" autoscan.py next
echo
echo "Done. Logs: $A/data/<market>/log/, cron output: $A/cron.log"
