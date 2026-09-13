"""Records the current finalised-round points as the baseline for
points_digest.drift(). Run by run_pipeline.sh only after build_dashboard.py
has succeeded, so a failed build leaves the old baseline in place and the next
run retries the rebuild rather than silently swallowing the correction.
"""
import sqlite3
import sys
from pathlib import Path

import points_digest

DB_PATH = Path(__file__).resolve().parent / "f1_data.db"


def main():
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(season) FROM races").fetchone()
    season = row[0] if row else None
    if season is None:
        return
    digests = points_digest.compute(conn, season)
    if digests:
        points_digest.store(conn, season, digests)
        print(f"points digest: baseline updated for {len(digests)} finalised round(s)")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
