"""Grid penalties for upcoming races (Tim, 8 Nov 2026).

penalty_watch.py fills the `penalties` table from a daily web search by
Claude. sim.py adds the confirmed ones to its grid before qualifying (after
qualifying OpenF1's official grid already has them), the Tools tab lists them
at the top, and notify.py sends one message per new or newly confirmed
penalty.

kind:   places  -- drops `places` from the qualifying position
        back    -- starts from the back of the grid
        pitlane -- starts from the pit lane (behind the back-of-grid cars)
status: confirmed (used by the sim) or expected (shown, not used)
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season INTEGER, round INTEGER, code TEXT, kind TEXT, places INTEGER,
    status TEXT, reason TEXT, source_url TEXT, source_title TEXT,
    found_at TEXT, updated_at TEXT, removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS penalty_checks (
    season INTEGER, round INTEGER, checked_at TEXT, ok INTEGER, note TEXT, cost_usd REAL);
"""

KIND_TEXT = {"back": "Back of the grid", "pitlane": "Pit lane start"}


def ensure(conn):
    conn.executescript(SCHEMA)


def for_round(conn, season, rnd):
    """Live penalties for a round, oldest first, as dicts."""
    try:
        rows = conn.execute(
            "SELECT id, code, kind, places, status, reason, source_url, source_title, found_at, updated_at "
            "FROM penalties WHERE season=? AND round=? AND removed=0 ORDER BY id", (season, rnd)).fetchall()
    except sqlite3.OperationalError:
        return []
    keys = ("id", "code", "kind", "places", "status", "reason", "url", "title", "found", "updated")
    return [dict(zip(keys, r)) for r in rows]


def last_check(conn, season, rnd):
    try:
        r = conn.execute("SELECT checked_at FROM penalty_checks WHERE season=? AND round=? AND ok=1 "
                         "ORDER BY checked_at DESC LIMIT 1", (season, rnd)).fetchone()
    except sqlite3.OperationalError:
        return None
    return r[0] if r else None


def describe(p):
    """'10-place grid drop', 'Back of the grid', 'Pit lane start'."""
    if p["kind"] == "places":
        return f"{p['places']}-place grid drop" if p["places"] else "Grid drop"
    return KIND_TEXT.get(p["kind"], p["kind"])


def as_overrides(conn, season, rnd):
    """The confirmed penalties in sim_overrides.json's shape."""
    ov = {"grid_penalty": {}, "back": [], "pitlane": []}
    for p in for_round(conn, season, rnd):
        if p["status"] != "confirmed":
            continue
        if p["kind"] == "places" and p["places"]:
            ov["grid_penalty"][p["code"]] = ov["grid_penalty"].get(p["code"], 0) + int(p["places"])
        elif p["kind"] in ("back", "pitlane"):
            ov[p["kind"]].append(p["code"])
    return ov


def signature(conn, season, rnd):
    """What the site shows for a round's penalties, for gate_check.py."""
    return [[p["id"], p["status"], p["kind"], p["places"]] for p in for_round(conn, season, rnd)]
