"""Decides whether the sentinel server's hourly pipeline should bother
building+pushing the dashboard this run. fetch_f1_data.py always runs hourly
regardless (cheap, and it's what lets a new race weekend get noticed within
an hour of the first session posting points) — this gate only guards the
visible, GitHub-commit-producing build+push step, per Tim's call: hourly
during an in-progress race weekend, continuing for 48 hours after the
current round is first observed as finalised (mds==3), to cover F1 Fantasy
sometimes being slow to finalise scoring. Outside that window, exits nonzero
so the wrapper script skips build+push for this run.

One exception overrides the window: if the points of an *already-finalised*
round have moved since the last successful build — a stewards' decision applied
retroactively, e.g. the penalties reinstated against Gasly for 2026 Monaco —
we rebuild regardless of how long ago that round finished. See points_digest.py.

Exit 0 = proceed with build+push. Exit 1 = skip this run.
"""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

import points_digest

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

    # Retro-correction check, ahead of the window logic below: if the points of
    # a round that already finalised have moved since the last successful build,
    # republish however long ago that round finished.
    current = points_digest.compute(conn, season)
    stored = points_digest.load(conn, season)
    if current and not stored:
        # First run after this check shipped — adopt today's numbers as the
        # baseline rather than forcing a rebuild that has nothing new to say.
        points_digest.store(conn, season, current)
        print(f"points digest: seeded baseline for {len(current)} finalised round(s)")
    else:
        moved = points_digest.drift(current, stored)
        if moved:
            print("points changed for already-finalised round(s): "
                  + ", ".join(str(r) for r in moved)
                  + " — forcing a rebuild")
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
