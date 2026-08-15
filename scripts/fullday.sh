#!/bin/bash
#
# Full-day cross-venue test. Run once, walk away, read the report.
#
#   ./scripts/fullday.sh
#   ./scripts/fullday.sh 8          # 8 hours instead of the default 10
#
# Runs the event-driven recorder across an entire MLB slate, then prints a
# summary. Safe to start early — it discovers games as they begin and skips
# quietly when none are live.
#
# Everything is read-only and simulated. No order is placed on either venue.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

HOURS="${1:-10}"
SECONDS_TOTAL=$(python3 -c "print(int($HOURS * 3600))")
STAMP=$(date +%Y%m%d-%H%M)
LOG="logs/fullday-${STAMP}.log"
mkdir -p logs data

BIN=.venv/bin/arbengine
[ -x "$BIN" ] || { echo "Run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"; exit 1; }

echo "=============================================================="
echo " Cross-venue full-day test"
echo "=============================================================="
echo " duration : ${HOURS}h"
echo " log      : $LOG"
echo " started  : $(date)"
echo
echo " Databases written:"
echo "   crossevents.db  cross open/close times (50ms resolution)"
echo "   crossreads.db   every price observation, crossing or not"
echo "   crosspaper.db   simulated positions + settled P&L"
echo
echo " Leave this running. Ctrl-C stops it cleanly and still reports."
echo "=============================================================="
echo

# Event-driven recorder: measures how long each cross survives.
"$BIN" crossevent --duration "$SECONDS_TOTAL" 2>&1 | tee -a "$LOG"

echo
echo "=============================================================="
echo " REPORT"
echo "=============================================================="
.venv/bin/python scripts/report.py 2>&1 | tee -a "$LOG"
echo
echo " Full log: $LOG"
echo " Share the .db files or the report above."
