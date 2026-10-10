"""Decides what this run of run_pipeline.sh does. The timer fires every 15
minutes; prints one of:

  full   -- at least FULL_EVERY_MINUTES since the last full run: the whole
            pipeline (backup, full fetch, cookie watch, gated build+push).
  quick  -- otherwise, during a race weekend (FP1 - 1h to race + 8h, from
            race_schedule): a light fetch that saves stage snapshots.
  skip   -- otherwise.

`--mark-full` records that a full run is starting. It is recorded at the
start, so a full run that fails doesn't make every 15-minute run a full one.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "f1_data.db"
MARK_PATH = BASE_DIR / "secrets" / "last_full_run.txt"
FULL_EVERY_MINUTES = 50
WEEKEND_BEFORE_FP1 = timedelta(hours=1)
WEEKEND_AFTER_RACE = timedelta(hours=8)


def last_full_run():
    try:
        return datetime.fromisoformat(MARK_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def in_race_weekend(now):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT fp1_utc, race_utc FROM race_schedule WHERE fp1_utc IS NOT NULL AND race_utc IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return False
    return any(datetime.fromisoformat(fp1) - WEEKEND_BEFORE_FP1 <= now
               <= datetime.fromisoformat(race) + WEEKEND_AFTER_RACE
               for fp1, race in rows)


def main():
    now = datetime.now(timezone.utc)
    if "--mark-full" in sys.argv:
        MARK_PATH.write_text(now.isoformat())
        return
    last = last_full_run()
    if last is None or now - last >= timedelta(minutes=FULL_EVERY_MINUTES):
        print("full")
    elif in_race_weekend(now):
        print("quick")
    else:
        print("skip")


if __name__ == "__main__":
    main()
