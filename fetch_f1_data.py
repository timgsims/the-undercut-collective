#!/usr/bin/env python3
"""
Fetch F1 Fantasy league data (scores, picks, chips, budget) from the
official-but-unofficial fantasy.formula1.com backend and upsert it into
f1_data.db, replacing manual spreadsheet entry.

Auth: reads a session cookie from secrets/f1_cookie.txt (never committed —
see .gitignore). That cookie belongs to Tim's F1 Fantasy account; the
"opponent" endpoints are a real, sanctioned feature of the app for viewing
league-mates' teams, not an access-control bypass.

Safe to re-run any time: every round for every manager is refetched and
upserted, so a run with nothing new to report is a harmless no-op, and a
half-finished run never leaves partial data for the round it was mid-write on
(each round's writes happen inside a single DB transaction).
"""
import sys
import time
import json
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
COOKIE_PATH = BASE_DIR / "secrets" / "f1_cookie.txt"
DB_PATH = BASE_DIR / "f1_data.db"
STATUS_PATH = BASE_DIR / "secrets" / "last_run_status.txt"

# Bump if the season adds more rounds than this.
MAX_ROUNDS = 22

# (name, team_name, social_id, guid_base, is_self)
# guid_base is the user_guid WITHOUT the "-0-<social_id>" suffix (that's what
# the self "getteam" endpoint expects); opponent endpoints want the full guid
# ("{guid_base}-0-{social_id}"), built below.
MANAGERS = [
    ("Tim Stewart",            "Team Ferrari Hopium",       "35464543",  "291f57c0-11c3-11f1-a279-1fb9dc4f9d6c", True),
    ("Stu Henley-Minchington", "The Anthill Mob",           "217005621", "df66ce90-11c9-11f1-9e16-87ceca86287a", False),
    ("Grayson Mitchell",       "Reigning champppp",         "178587181", "bb22730e-1205-11f1-a400-e3533fbb437d", False),
    ("Lori Nalder",            "Piastri'd ma pants",        "220240592", "08475ae4-11db-11f1-8648-5b76ac87238e", False),
    ("Dan O'Connell",          "AutoBottas Roll Out",       "217071335", "c69bde1e-11c9-11f1-b5c1-fbbc0007d48b", False),
    ("Cain Hood",              "Hulken Burgen",             "78530967",  "df057962-1203-11f1-9f8b-c9c4bccfe15c", False),
    ("Mark Spivey",            "Valtteris Moustache Rides", "39191718",  "f770dc48-11ff-11f1-ac4e-07adeddb7966", False),
    ("Jaime Stewart",          "Cadil-lack of Data",        "177795497", "6ced61c2-11c8-11f1-8caf-3db62f01927f", False),
]

# API field names -> the chip names already used throughout the dashboard.
CHIP_FIELDS = [
    ("iswildcardtaken",   "wildcardtakengd",   "Wildcard"),
    ("islimitlesstaken",  "limitlesstakengd",  "Limitless"),
    ("isfinalfixtaken",   "finalfixtakengd",   "Final Fix"),
    ("isextradrstaken",   "extradrstakengd",   "Extra DRS"),
    ("isnonigativetaken", "nonigativetakengd", "No Negative"),
    ("isautopilottaken",  "autopilottakengd",  "Auto Pilot"),
]

# Driver/constructor IDs confirmed by cross-referencing three of Tim's teams
# against the F1 Fantasy UI. Unknown IDs are stored with a NULL name and
# filled in later (see players table) — never guessed.
KNOWN_PLAYERS = {
    "11161": ("K. Antonelli", "driver"),
    "11032": ("I. Hadjar", "driver"),
    "11149": ("A. Lindblad", "driver"),
    "11051": ("G. Bortoleto", "driver"),
    "111":   ("N. Hulkenberg", "driver"),
    "110":   ("L. Hamilton", "driver"),
    "114":   ("L. Lawson", "driver"),
    "25":    ("Ferrari", "constructor"),
    "28":    ("Mercedes", "constructor"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS managers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    team_name TEXT NOT NULL,
    guid TEXT NOT NULL UNIQUE,
    is_self INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT
);
CREATE TABLE IF NOT EXISTS race_results (
    round INTEGER NOT NULL,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    points REAL,
    season_total REAL,
    team_value REAL,
    team_balance REAL,
    captain_player_id TEXT,
    fetched_at TEXT,
    PRIMARY KEY (round, manager_id)
);
CREATE TABLE IF NOT EXISTS team_picks (
    round INTEGER NOT NULL,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    player_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (round, manager_id, position)
);
CREATE TABLE IF NOT EXISTS chips_used (
    manager_id TEXT NOT NULL REFERENCES managers(id),
    chip_name TEXT NOT NULL,
    round_taken INTEGER,
    PRIMARY KEY (manager_id, chip_name)
);
CREATE TABLE IF NOT EXISTS races (
    round INTEGER PRIMARY KEY,
    name TEXT
);
"""


def load_cookie():
    if not COOKIE_PATH.exists():
        sys.exit(
            f"ERROR: cookie file not found at {COOKIE_PATH}\n"
            "Save your F1 Fantasy session cookie there (see project notes) before running this."
        )
    cookie = COOKIE_PATH.read_text(encoding="utf-8").strip()
    if not cookie:
        sys.exit(f"ERROR: cookie file at {COOKIE_PATH} is empty.")
    return cookie


def fetch_json(url, cookie):
    req = urllib.request.Request(url, headers={"Cookie": cookie, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "401 Unauthorized (cookie expired or invalid)"
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, "Invalid JSON response"
    if payload.get("Meta", {}).get("Success") is False:
        return None, f"API reported failure: {payload.get('Meta', {}).get('Message')}"
    return payload, None


def team_url(guid_base, round_no):
    return (
        f"https://fantasy.formula1.com/services/user/gameplay/{guid_base}"
        f"/getteam/1/1/{round_no}/1?buster={int(time.time() * 1000)}"
    )


def opponent_url(full_guid, round_no):
    return (
        "https://fantasy.formula1.com/services/user/opponentteam/opponentgamedayplayerteamget/1/"
        f"{full_guid}/1/{round_no}/1?buster={int(time.time() * 1000)}"
    )


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def seed_managers(conn):
    for name, team_name, social_id, guid_base, is_self in MANAGERS:
        full_guid = f"{guid_base}-0-{social_id}"
        conn.execute(
            "INSERT INTO managers (id, name, team_name, guid, is_self) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, team_name=excluded.team_name, "
            "guid=excluded.guid, is_self=excluded.is_self",
            (social_id, name, team_name, full_guid, int(is_self)),
        )
    conn.commit()


def seed_known_players(conn):
    for pid, (name, ptype) in KNOWN_PLAYERS.items():
        conn.execute(
            "INSERT INTO players (id, name, type) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type",
            (pid, name, ptype),
        )
    conn.commit()


def pick_team_entry(user_team, is_self, expected_team_name):
    """user_team can contain multiple entries (Tim plays other F1 Fantasy
    leagues under the same account) — always select the one matching this
    private league's team name, never just the first entry."""
    if not user_team:
        return None
    if is_self:
        for t in user_team:
            if urllib.parse.unquote(t.get("teamname", "")) == expected_team_name:
                return t
        return None  # don't guess — if the expected team isn't present, skip this round
    return user_team[0]


def process_manager_round(conn, social_id, team_name, guid_for_url, is_self, round_no, cookie):
    url = team_url(guid_for_url, round_no) if is_self else opponent_url(guid_for_url, round_no)
    payload, err = fetch_json(url, cookie)
    if err:
        return err

    try:
        value = payload["Data"]["Value"]
    except (KeyError, TypeError):
        return "Unexpected response shape"

    entry = pick_team_entry(value.get("userTeam") or [], is_self, team_name)
    if entry is None:
        return None  # nothing usable for this round — quiet no-op, not an error

    gdpoints = entry.get("gdpoints")
    if gdpoints is None:
        return None  # race not played yet — quiet no-op

    with conn:
        conn.execute(
            "INSERT INTO race_results (round, manager_id, points, season_total, team_value, "
            "team_balance, captain_player_id, fetched_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(round, manager_id) DO UPDATE SET points=excluded.points, "
            "season_total=excluded.season_total, team_value=excluded.team_value, "
            "team_balance=excluded.team_balance, captain_player_id=excluded.captain_player_id, "
            "fetched_at=excluded.fetched_at",
            (
                round_no, social_id, gdpoints, entry.get("ovpoints"),
                (entry.get("team_info") or {}).get("teamVal"),
                (entry.get("team_info") or {}).get("teamBal"),
                entry.get("capplayerid"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        conn.execute("DELETE FROM team_picks WHERE round=? AND manager_id=?", (round_no, social_id))
        for p in entry.get("playerid", []):
            pid, pos = p.get("id"), p.get("playerpostion")
            if pid is None or pos is None:
                continue
            conn.execute(
                "INSERT INTO players (id, name, type) VALUES (?, NULL, NULL) "
                "ON CONFLICT(id) DO NOTHING",
                (pid,),
            )
            conn.execute(
                "INSERT INTO team_picks (round, manager_id, player_id, position) VALUES (?,?,?,?)",
                (round_no, social_id, pid, pos),
            )

        for taken_field, gd_field, chip_name in CHIP_FIELDS:
            if entry.get(taken_field):
                conn.execute(
                    "INSERT INTO chips_used (manager_id, chip_name, round_taken) VALUES (?,?,?) "
                    "ON CONFLICT(manager_id, chip_name) DO UPDATE SET round_taken=excluded.round_taken",
                    (social_id, chip_name, entry.get(gd_field)),
                )

    return None


def main():
    cookie = load_cookie()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    seed_managers(conn)
    seed_known_players(conn)

    fatal = False
    for name, team_name, social_id, guid_base, is_self in MANAGERS:
        full_guid = f"{guid_base}-0-{social_id}"
        guid_for_url = guid_base if is_self else full_guid
        for round_no in range(1, MAX_ROUNDS + 1):
            err = process_manager_round(conn, social_id, team_name, guid_for_url, is_self, round_no, cookie)
            if err:
                if "401" in err:
                    print(f"FATAL: {err} — stopping, no partial data written for remaining rounds/managers.")
                    fatal = True
                    break
                print(f"  [warn] {name} round {round_no}: {err}")
            time.sleep(0.15)
        if fatal:
            break

    conn.close()

    STATUS_PATH.write_text(
        f"{'FAILED' if fatal else 'OK'} at {datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )

    if fatal:
        sys.exit(1)
    print("Fetch complete.")


if __name__ == "__main__":
    main()
