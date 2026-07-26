#!/bin/bash
# Runs hourly on the sentinel server (see f1-fantasy-pipeline.timer). Always
# fetches — cheap, and it's what lets a new race weekend get noticed within
# an hour of the first session posting points. build_dashboard.py (which
# writes index.html and pushes it to GitHub) only runs when gate_check.py
# says we're inside a live-race-weekend-or-just-finished window, so the repo
# isn't spammed with hourly commits the rest of the season.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

python3 fetch_f1_data.py
fetch_status=$?
if [ "$fetch_status" -ne 0 ]; then
    echo "fetch_f1_data.py failed (exit $fetch_status) — continuing with last-good f1_data.db."
fi

if python3 gate_check.py; then
    python3 build_dashboard.py
else
    echo "Outside the race-weekend tracking window — skipping build/push this run."
fi
