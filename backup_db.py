"""Backs up f1_data.db, which is gitignored and otherwise exists only on the
sentinel. Runs at the top of every hourly pipeline run, *before* the fetch,
so a backup is never taken from data the same run may have just damaged.

Two sets, both on the 1TB data drive rather than the OS SSD the live database
sits on, so a dead SSD can't take the database and its backups with it:

  daily/   one copy per NZ calendar day, newest 30 kept
  rounds/  one copy per round, taken the first run after that round is
           finalised (round_finalized_at), newest 4 rounds kept

Copies use SQLite's backup API (consistent even mid-write) and are checked
with PRAGMA quick_check. Never allowed to fail the run.

Restore: stop the timer, copy a backup over f1_data.db, start the timer.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "f1_data.db"
BACKUP_DIR = Path("/mnt/data/backups/f1-fantasy")
DAILY_KEEP = 30
ROUNDS_KEEP = 4
NZ = ZoneInfo("Pacific/Auckland")


def copy_db(dest):
    """SQLite backup to a temp name, verify, then rename into place, so a
    half-written file never carries a valid-looking backup name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out)
        ok = out.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        out.close()
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"quick_check on {dest.name} returned {ok!r}")
    tmp.replace(dest)
    print(f"[backup] wrote {dest} ({dest.stat().st_size // 1024} KB)")


def prune(folder, keep, key):
    files = sorted(folder.glob("*.db"), key=key, reverse=True)
    for old in files[keep:]:
        old.unlink()
        print(f"[backup] pruned {old.name}")


def latest_finalised_round(conn):
    try:
        row = conn.execute(
            "SELECT season, MAX(round) FROM round_finalized_at "
            "WHERE season = (SELECT MAX(season) FROM round_finalized_at)"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row if row and row[1] is not None else None


def main():
    if not DB_PATH.exists():
        print("[backup] no f1_data.db yet -- nothing to back up")
        return 0

    daily = BACKUP_DIR / "daily"
    rounds = BACKUP_DIR / "rounds"

    today = datetime.now(NZ).strftime("%Y-%m-%d")
    dest = daily / f"f1_data-{today}.db"
    if not dest.exists():
        copy_db(dest)
        prune(daily, DAILY_KEEP, key=lambda p: p.name)

    conn = sqlite3.connect(DB_PATH)
    try:
        latest = latest_finalised_round(conn)
    finally:
        conn.close()
    if latest:
        season, rnd = latest
        dest = rounds / f"f1_data-{season}-R{rnd:02d}-final.db"
        if not dest.exists():
            copy_db(dest)
            prune(rounds, ROUNDS_KEEP, key=lambda p: p.name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[backup] FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
