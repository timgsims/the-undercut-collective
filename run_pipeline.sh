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

# The timer fires every 15 minutes. pipeline_mode.py decides what this run
# does: "full" about once an hour (everything below, as before), "quick"
# during a race weekend (a ~10-request fetch that saves stage snapshots for
# no-spoiler mode, no build), or "skip" (between race weekends).
mode=$(python3 pipeline_mode.py)
if [ "$mode" = "skip" ]; then
    exit 0
fi
if [ "$mode" = "quick" ]; then
    python3 fetch_f1_data.py --quick || echo "quick fetch failed (non-fatal)"
    # The quick fetch leaves this flag when a session has just started
    # scoring: carry on into a full run so the provisional points
    # (provisional.py) go live now rather than at the next hourly run.
    if [ -e secrets/new_stage_scored ]; then
        rm -f secrets/new_stage_scored
    else
        exit 0
    fi
fi
python3 pipeline_mode.py --mark-full

# Back up f1_data.db before the fetch touches it (30 daily + last 4 finalised
# rounds, on /mnt/data). Never allowed to fail the run.
python3 backup_db.py || echo "backup_db.py failed (non-fatal)"

# Re-mint the 96h session cookie from the ~30-day F1 account session before
# fetching (no-op unless it has <48h left or the last fetch failed). Exit 2
# means the account session is dead and a human must paste a fresh cookie --
# cookie_watch.py handles telling Tim. Never allowed to fail the run.
python3 renew_cookie.py || echo "renew_cookie.py failed (exit $?, non-fatal)"

python3 fetch_f1_data.py
fetch_status=$?
if [ "$fetch_status" -ne 0 ]; then
    echo "fetch_f1_data.py failed (exit $fetch_status) — continuing with last-good f1_data.db."
fi

# Cookie expiry watch. Emails Tim only around race weekends, when the ~30-day
# account session behind the cookie (or the 96h cookie itself, if renewal is
# failing) won't last. Never allowed to fail the run.
python3 cookie_watch.py || echo "cookie_watch.py failed (non-fatal)"

if python3 gate_check.py; then
    python3 build_dashboard.py
    build_status=$?
    if [ "$build_status" -eq 0 ]; then
        # Baseline for the retro-correction check, moved forward only on a
        # build that actually succeeded -- otherwise a failed build would
        # adopt the corrected numbers as "already published" and the fix
        # would never reach the site.
        python3 record_digest.py || echo "record_digest.py failed (non-fatal)"
    else
        echo "build_dashboard.py failed (exit $build_status) — points baseline left alone so the next run retries."
    fi
else
    echo "Outside the race-weekend tracking window — skipping build/push this run."
fi
