#!/usr/bin/env bash
# Run on YOUR computer. Packs the scanner and the autoscan runner into one file
# for the server: day-trader-autoscan.tar.gz (no logs, results or secrets).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)          # .../Day-trader
OUT=${1:-$ROOT/day-trader-autoscan.tar.gz}
[ -f "$ROOT/day-trader-skill/skills/trader-scanner/scan.py" ] || { echo "scan.py not found under $ROOT/day-trader-skill"; exit 1; }
tar -C "$ROOT" -czf "$OUT" \
  --exclude='__pycache__' --exclude='autoscan/data' --exclude='autoscan/cron.log' --exclude='*.tar.gz' \
  --exclude='day-trader-skill/trader' --exclude='autoscan/trader' \
  day-trader-skill autoscan
echo "Bundle: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Copy it to the server:  scp \"$OUT\" USER@SERVER:~/"
