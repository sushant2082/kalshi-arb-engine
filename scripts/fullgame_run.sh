#!/bin/zsh
# Full-game cross-venue MLB recorder.
#
# Runs locally because it needs the Kalshi credentials in .env and the RSA key,
# neither of which is in the repo — a cloud agent would clone the code, fail
# auth, and record nothing.
set -u
cd /Users/susha/kalshi-arb-engine || exit 1

STAMP=$(date +%Y%m%d-%H%M)
LOG="logs/fullgame-${STAMP}.log"
mkdir -p logs

# 11 hours: covers the 1:10pm CDT afternoon block through the last 8:40pm CDT
# game reaching a final. Interval 12s is the honest cadence for a 9-15 game
# slate at the measured ~3.6 req/s Kalshi ceiling (2 orderbook calls per game).
exec .venv/bin/arbengine crossrec --duration 39600 --interval 12 >> "$LOG" 2>&1
