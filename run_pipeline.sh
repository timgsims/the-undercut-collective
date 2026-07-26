#!/bin/bash
# Runs hourly on the sentinel server (see f1-fantasy-pipeline.timer). Always
# fetches — cheap, and it's what lets a new race weekend get noticed within
# an hour of the first session posting points. build_dashboard.py (which
# writes index.html and pushes it to GitHub) only runs when gate_check.py
# says we're inside a live-race-weekend-or-just-finished window, so the repo
# isn't spammed with hourly commits the rest of the season.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# Sync to latest origin/main before doing anything else. Tim also pushes
# manual fixes from his own machine between hourly runs — without this, this
# script's own commit always starts from a stale base and its push at the
# end gets rejected as a non-fast-forward, every single time, silently
# (build_dashboard.py's git_push() only logs the error, it doesn't fail the
# run). The sentinel never makes real source changes of its own — every
# commit it makes is just a regenerated index.html — so discarding whatever
# it has locally and resetting to origin/main is always safe.
git fetch origin
git reset --hard origin/main

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
