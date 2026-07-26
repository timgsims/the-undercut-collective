"""Decides whether the sentinel server's hourly pipeline should bother
building+pushing the dashboard this run. fetch_f1_data.py always runs hourly
regardless (cheap, and it's what lets a new race weekend get noticed within
an hour of the first session posting points) — this gate only guards the
visible, GitHub-commit-producing build+push step, per Tim's call: hourly
during an in-progress race weekend, continuing for 48 hours after the
current round is first observed as finalised (mds==3), to cover F1 Fantasy
sometimes being slow to finalise scoring. Outside that window, exits nonzero
so the wrapper script skips build+push for this run.

Exit 0 = proceed with build+push. Exit 1 = skip this run.
"""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent / "f1_data.db"
FINALIZED_WINDOW_HOURS = 48


def main():
    if not DB_PATH.exists():
        sys.exit(0)  # no DB yet — let the pipeline run so one gets created

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(season) AS s FROM races").fetchone()
    season = row[0] if row else None
    if season is None:
        sys.exit(0)

    row = conn.execute(
        "SELECT MAX(round) FROM race_results WHERE season=?", (season,)
    ).fetchone()
    latest_round = row[0] if row else None
    if latest_round is None:
        sys.exit(0)  # nothing fetched yet this season — proceed

    row = conn.execute(
        "SELECT MAX(is_final) FROM race_results WHERE season=? AND round=?",
        (season, latest_round),
    ).fetchone()
    is_final = bool(row[0]) if row and row[0] is not None else False
    if not is_final:
        sys.exit(0)  # weekend still live — proceed

    row = conn.execute(
        "SELECT finalized_at FROM round_finalized_at WHERE season=? AND round=?",
        (season, latest_round),
    ).fetchone()
    if row is None:
        # Final per race_results but no timestamp recorded yet (e.g. upgrading
        # from a version of fetch_f1_data.py that predates this table) — proceed
        # once so fetch_f1_data.py's own logic gets a chance to record one.
        sys.exit(0)

    finalized_at = datetime.fromisoformat(row[0])
    hours_since = (datetime.now(timezone.utc) - finalized_at).total_seconds() / 3600
    sys.exit(0 if hours_since < FINALIZED_WINDOW_HOURS else 1)


if __name__ == "__main__":
    main()
