#!/usr/bin/env bash
# Pull the latest code from GitHub, check it, and roll back if it is broken.
#
# Runs on the server from a git clone of breakout-signal-scanner:
#   bash autoscan/deploy/update.sh            # update now
#   bash autoscan/deploy/update.sh --check    # only say whether an update is waiting
#
# install.sh adds a cron line that runs it every day at 07:00 IST (01:30 UTC), when
# both markets are closed, so a scan is never interrupted by new code.
# Your logs, results and signal_config.local.json are not in git and are never touched.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
BRANCH=${UPDATE_BRANCH:-main}
cd "$REPO"
stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }

[ -d .git ] || { echo "$(stamp) not a git clone: $REPO"; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "$(stamp) local changes to tracked files, not updating (see: git -C $REPO status)"
  exit 1
fi

git fetch --quiet origin "$BRANCH"
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
  echo "$(stamp) up to date at ${OLD:0:7}"
  exit 0
fi
if [ "${1:-}" = "--check" ]; then
  echo "$(stamp) update waiting: ${OLD:0:7} -> ${NEW:0:7}"
  git log --oneline "$OLD..$NEW"
  exit 0
fi

git merge --ff-only --quiet "origin/$BRANCH" || { echo "$(stamp) cannot fast-forward to ${NEW:0:7}; not updating"; exit 1; }

# Check the new code before the next scan runs it; roll back if anything fails.
ok=1
python3 -m py_compile autoscan/*.py day-trader-skill/skills/*/*.py 2>/tmp/breakout-update.err || ok=0
if [ $ok = 1 ]; then
  (cd autoscan && python3 -c "import json, signals, swing; signals.load_config(); json.load(open('signal_config.json'))") \
    2>>/tmp/breakout-update.err || ok=0
fi
find autoscan day-trader-skill -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
if [ $ok = 0 ]; then
  git reset --quiet --hard "$OLD"
  echo "$(stamp) update to ${NEW:0:7} FAILED its checks, rolled back to ${OLD:0:7}:"
  sed 's/^/    /' /tmp/breakout-update.err | tail -20
  exit 1
fi
echo "$(stamp) updated ${OLD:0:7} -> ${NEW:0:7}"
git log --oneline "$OLD..$NEW" | sed 's/^/    /'
