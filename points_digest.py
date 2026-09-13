"""Fingerprints the points of already-finalised rounds so the pipeline can
notice a retro-correction.

The hourly fetch is a *full* re-read — every round, every manager, all upserts
— so when the FIA reinstates a penalty weeks later (2026 Monaco/Gasly is the
case that prompted this) the corrected numbers land in f1_data.db on their own.
The dashboard, though, is only rebuilt while gate_check.py says we're in a live
race weekend or within 48h of the current round finalising, and gate_check only
ever looks at the *latest* round. So a correction to an old round would sit in
the DB behind a stale published page until the next race weekend rolled around.

This module closes that gap: store a digest of each finalised round's points,
compare every run, and let gate_check force a build when one moves.

Only genuinely points-bearing columns go into the digest. Anything that churns
on its own — fetched_at, team/market values, global ranks — would fire the gate
every hour and turn the hourly-commit spam back on, which is the exact thing
gate_check exists to prevent.
"""
import hashlib
import sqlite3

TABLE = "finalized_points_digest"


def ensure_table(conn):
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            season     INTEGER NOT NULL,
            round      INTEGER NOT NULL,
            digest     TEXT    NOT NULL,
            updated_at TEXT,
            PRIMARY KEY (season, round)
        )"""
    )
    conn.commit()


def _num(v):
    """Stable text for a nullable REAL, so float repr never shifts the hash."""
    return "~" if v is None else f"{float(v):.4f}"


def finalized_rounds(conn, season):
    return [
        r[0]
        for r in conn.execute(
            "SELECT round FROM race_results WHERE season=? "
            "GROUP BY round HAVING MAX(is_final)=1 ORDER BY round",
            (season,),
        )
    ]


def compute(conn, season):
    """-> {round: digest} over every finalised round's points."""
    digests = {}
    for rnd in finalized_rounds(conn, season):
        h = hashlib.sha256()

        # League scores -- what the leaderboard and standings are built from.
        for mgr, pts, total in conn.execute(
            "SELECT manager_id, points, season_total FROM race_results "
            "WHERE season=? AND round=? ORDER BY manager_id",
            (season, rnd),
        ):
            h.update(f"m|{mgr}|{_num(pts)}|{_num(total)}\n".encode())

        # Per-driver points -- a correction can move these even when no
        # manager in the league happened to own the driver.
        for pid, gd in conn.execute(
            "SELECT player_id, gameday_points FROM player_results "
            "WHERE season=? AND round=? ORDER BY player_id",
            (season, rnd),
        ):
            h.update(f"p|{pid}|{_num(gd)}\n".encode())

        # Session breakdown -- where a reinstated penalty actually shows up.
        for pid, sess, pts in conn.execute(
            "SELECT player_id, session_number, points FROM player_session_points "
            "WHERE season=? AND round=? ORDER BY player_id, session_number",
            (season, rnd),
        ):
            h.update(f"s|{pid}|{sess}|{_num(pts)}\n".encode())

        digests[rnd] = h.hexdigest()
    return digests


def load(conn, season):
    ensure_table(conn)
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT round, digest FROM {TABLE} WHERE season=?", (season,)
        )
    }


def store(conn, season, digests):
    from datetime import datetime, timezone

    ensure_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        f"INSERT INTO {TABLE} (season, round, digest, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(season, round) DO UPDATE SET "
        "digest=excluded.digest, updated_at=excluded.updated_at",
        [(season, rnd, d, now) for rnd, d in digests.items()],
    )
    conn.commit()


def drift(current, stored):
    """Rounds whose points changed since the last successful build.

    A round in `current` but not in `stored` is NOT drift -- it has simply
    just finalised, and gate_check's own 48h window already covers it. Only
    a round we have a baseline for and that no longer matches counts, plus
    one that has vanished from the data entirely.
    """
    changed = [r for r in sorted(current) if r in stored and current[r] != stored[r]]
    vanished = [r for r in sorted(stored) if r not in current]
    return changed + vanished
