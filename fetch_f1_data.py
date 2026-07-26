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

# Bump each year.
SEASON = 2026

# (name, team_name, social_id, guid_base, is_self)
# guid_base is the user_guid WITHOUT the "-0-<social_id>" suffix (that's what
# the self "getteam" endpoint expects); opponent endpoints want the full guid
# ("{guid_base}-0-{social_id}"), built below.
MANAGERS = [
    # Short first names — must match MANAGER_COLOURS keys in build_dashboard.py,
    # not the manager's full legal name as returned by the API.
    ("Tim",     "Team Ferrari Hopium",       "35464543",  "291f57c0-11c3-11f1-a279-1fb9dc4f9d6c", True),
    ("Stu",     "The Anthill Mob",           "217005621", "df66ce90-11c9-11f1-9e16-87ceca86287a", False),
    ("Grayson", "Reigning champppp",         "178587181", "bb22730e-1205-11f1-a400-e3533fbb437d", False),
    ("Lori",    "Piastri'd ma pants",        "220240592", "08475ae4-11db-11f1-8648-5b76ac87238e", False),
    ("Dan",     "AutoBottas Roll Out",       "217071335", "c69bde1e-11c9-11f1-b5c1-fbbc0007d48b", False),
    ("Cain",    "Hulken Burgen",             "78530967",  "df057962-1203-11f1-9f8b-c9c4bccfe15c", False),
    ("Mark",    "Valtteris Moustache Rides", "39191718",  "f770dc48-11ff-11f1-ac4e-07adeddb7966", False),
    ("Jaime",   "Cadil-lack of Data",        "177795497", "6ced61c2-11c8-11f1-8caf-3db62f01927f", False),
]

# Confirmed against Tim's existing workbook — API round numbers line up 1:1
# with this calendar (round 10's API data matched "Belgian Grand Prix" in the
# UI exactly), so there's no sprint-weekend offset to account for.
RACE_CALENDAR = [
    (1, "Australia"), (2, "China"), (3, "Japan"), (4, "Miami"), (5, "Canada"),
    (6, "Monaco"), (7, "Barcelona"), (8, "Austria"), (9, "Great Britain"),
    (10, "Belgium"), (11, "Hungary"), (12, "Netherlands"), (13, "Italy"),
    (14, "Spain"), (15, "Azerbaijan"), (16, "Singapore"), (17, "Austin"),
    (18, "Mexico"), (19, "Brazil"), (20, "Las Vegas"), (21, "Qatar"),
    (22, "Abu Dhabi"),
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
CREATE TABLE IF NOT EXISTS player_results (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(id),
    gameday_points REAL,
    overall_points REAL,
    value_change REAL,
    PRIMARY KEY (season, round, player_id)
);
CREATE TABLE IF NOT EXISTS session_status (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    session_number INTEGER NOT NULL,
    session_type TEXT,
    is_done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, round, session_number)
);
CREATE TABLE IF NOT EXISTS player_session_points (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(id),
    session_number INTEGER NOT NULL,
    session_type TEXT,
    points REAL,
    PRIMARY KEY (season, round, player_id, session_number)
);
CREATE TABLE IF NOT EXISTS race_results (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    points REAL,
    season_total REAL,
    team_value REAL,
    team_balance REAL,
    captain_player_id TEXT,
    is_final INTEGER NOT NULL DEFAULT 0,
    gameday_rank INTEGER,
    overall_rank INTEGER,
    transfers_made INTEGER,
    inactive_driver_penalty INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (season, round, manager_id)
);
CREATE TABLE IF NOT EXISTS team_picks (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    player_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (season, round, manager_id, position)
);
CREATE TABLE IF NOT EXISTS chips_used (
    season INTEGER NOT NULL,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    chip_name TEXT NOT NULL,
    round_taken INTEGER,
    PRIMARY KEY (season, manager_id, chip_name)
);
CREATE TABLE IF NOT EXISTS races (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    name TEXT,
    PRIMARY KEY (season, round)
);
CREATE TABLE IF NOT EXISTS nz_leaderboard (
    season INTEGER NOT NULL,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    nz_rank INTEGER,
    nz_points REAL,
    PRIMARY KEY (season, manager_id)
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


def gameday_status_url(full_guid):
    # Same "opponent" endpoint family works for a manager's own full guid too —
    # returns every round's status in one call, so this isn't a per-round hit.
    return (
        "https://fantasy.formula1.com/services/user/opponentteam/opponentgamedayget/1/"
        f"{full_guid}/1?buster={int(time.time() * 1000)}"
    )


def fetch_gameday_statuses(full_guid, cookie):
    """Returns (rounds_with_data, rounds_final):
    - rounds_with_data: any round with a points value at all, including a race
      weekend that's only partway through (e.g. qualifying has posted points
      but the race hasn't run yet) — season totals/standings should include
      these live, per Tim's call: the leaderboard should track along in
      real time as a weekend unfolds.
    - rounds_final: the subset where mds==3, F1 Fantasy's own flag for "this
      gameday's scoring is fully finalised". Podium/race-result declarations
      are gated on this, not on rounds_with_data — a race isn't "won" until
      it's actually finished, ties included."""
    payload, err = fetch_json(gameday_status_url(full_guid), cookie)
    if err:
        return None, None, err
    try:
        md_details = payload["Data"]["Value"]["mdDetails"]
    except (KeyError, TypeError):
        return None, None, "Unexpected response shape (mdDetails missing)"
    with_data = {int(r) for r, info in md_details.items() if info.get("pts") is not None}
    final     = {int(r) for r, info in md_details.items() if info.get("mds") == 3}
    return with_data, final, None


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


def nz_leaderboard_url():
    return (
        "https://fantasy.formula1.com/feeds/leaderboard/public/country/"
        f"list_1_351_0_1.json?buster={int(time.time() * 1000)}"
    )


def fetch_and_store_nz_leaderboard(conn, cookie):
    """Public feed, no auth required — a single static snapshot of New
    Zealand's (country code 351) top 500 F1 Fantasy players. Confirmed via
    live testing this is NOT a true paginated API — list_2_351_0_1.json 403s
    — so any manager ranked below 500th nationally simply won't appear here.
    The only endpoint that reveals a rank beyond that is self-scoped to
    whichever cookie makes the request, so there's no legitimate way to get
    every manager's NZ rank without each of them handing over their own
    session cookie. Returns an error string, or None on success."""
    payload, err = fetch_json(nz_leaderboard_url(), cookie)
    if err:
        return err
    try:
        entries = payload["Value"]["leaderboard"]
    except (KeyError, TypeError):
        return "Unexpected response shape"

    # Keyed by (social_id, team_name) — Tim runs 3 teams under one social_id,
    # so team_name is needed to isolate the one team tracked everywhere else
    # in the dashboard, same disambiguation pick_team_entry() does.
    by_key = {}
    for e in entries:
        team_name = urllib.parse.unquote(e.get("team_name", ""))
        by_key[(e.get("social_id"), team_name)] = (e.get("cur_rank"), e.get("cur_points"))

    conn.execute("DELETE FROM nz_leaderboard WHERE season=?", (SEASON,))
    for name, team_name, social_id, guid_base, is_self in MANAGERS:
        found = by_key.get((social_id, team_name))
        if found:
            conn.execute(
                "INSERT INTO nz_leaderboard (season, manager_id, nz_rank, nz_points) VALUES (?,?,?,?)",
                (SEASON, social_id, found[0], found[1]),
            )
    conn.commit()
    return None


def drivers_feed_url(round_no):
    return f"https://fantasy.formula1.com/feeds/drivers/{round_no}_en.json?buster={int(time.time() * 1000)}"


def fetch_and_store_drivers(conn, round_no, cookie):
    """Public feed, no auth required — one call per round (not per manager)
    gives every driver/constructor's real name, per-round points, and a
    session-by-session breakdown (Qualifying/Sprint/Race, whichever apply
    that weekend). Returns an error string, or None on success/no-data."""
    payload, err = fetch_json(drivers_feed_url(round_no), cookie)
    if err:
        return err
    try:
        players = payload["Data"]["Value"]
    except (KeyError, TypeError):
        return "Unexpected response shape"
    if not players:
        return None

    def to_float(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    session_info = {}   # {session_number: {"type":..., "done": bool}}
    with conn:
        for p in players:
            pid = p.get("PlayerId")
            if pid is None:
                continue
            name  = p.get("DisplayName") or p.get("FUllName")
            ptype = "constructor" if p.get("PositionName") == "CONSTRUCTOR" else "driver"
            conn.execute(
                "INSERT INTO players (id, name, type) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type",
                (pid, name, ptype),
            )

            value, old_value = to_float(p.get("Value")), to_float(p.get("OldPlayerValue"))
            value_change = round(value - old_value, 2) if value is not None and old_value is not None else None

            conn.execute(
                "INSERT INTO player_results (season, round, player_id, gameday_points, overall_points, "
                "value_change) VALUES (?,?,?,?,?,?) ON CONFLICT(season, round, player_id) DO UPDATE SET "
                "gameday_points=excluded.gameday_points, overall_points=excluded.overall_points, "
                "value_change=excluded.value_change",
                (SEASON, round_no, pid, to_float(p.get("GamedayPoints")), to_float(p.get("OverallPpints")), value_change),
            )

            for sess in p.get("SessionWisePoints", []) or []:
                sn = sess.get("sessionnumber")
                if sn is None:
                    continue
                info = session_info.setdefault(sn, {"type": sess.get("sessiontype"), "done": False})
                if sess.get("points") is not None:
                    info["done"] = True
                if sess.get("sessiontype"):
                    info["type"] = sess["sessiontype"]

                conn.execute(
                    "INSERT INTO player_session_points (season, round, player_id, session_number, "
                    "session_type, points) VALUES (?,?,?,?,?,?) ON CONFLICT(season, round, player_id, "
                    "session_number) DO UPDATE SET session_type=excluded.session_type, points=excluded.points",
                    (SEASON, round_no, pid, sn, sess.get("sessiontype"), to_float(sess.get("points"))),
                )

        for sn, info in session_info.items():
            conn.execute(
                "INSERT INTO session_status (season, round, session_number, session_type, is_done) "
                "VALUES (?,?,?,?,?) ON CONFLICT(season, round, session_number) DO UPDATE SET "
                "session_type=excluded.session_type, is_done=excluded.is_done",
                (SEASON, round_no, sn, info["type"], int(info["done"])),
            )
    return None


def seed_races(conn):
    for round_no, name in RACE_CALENDAR:
        conn.execute(
            "INSERT INTO races (season, round, name) VALUES (?,?,?) "
            "ON CONFLICT(season, round) DO UPDATE SET name=excluded.name",
            (SEASON, round_no, name),
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


def fetch_team_entry(guid_for_url, is_self, team_name, round_no, cookie):
    """Fetch and return just the raw team entry for one manager/round, with no
    DB writes — used both for normal processing and for "peeking" one round
    ahead to read a not-yet-finished gameday's team_info (see below)."""
    url = team_url(guid_for_url, round_no) if is_self else opponent_url(guid_for_url, round_no)
    payload, err = fetch_json(url, cookie)
    if err:
        return None, err
    try:
        value = payload["Data"]["Value"]
    except (KeyError, TypeError):
        return None, "Unexpected response shape"
    entry = pick_team_entry(value.get("userTeam") or [], is_self, team_name)
    return entry, None


def process_manager_round(conn, social_id, round_no, entry, budget_entry, is_final):
    """entry = this round's own team data (points/roster/chips).
    budget_entry = round_no+1's team data, used only for its team_info —
    F1 Fantasy's team_info on round N reflects prices as of the START of
    that gameday (i.e. after race N-1, not race N), so the correct "budget
    after race N" figure is maxTeambal from round N+1's response, confirmed
    against Tim's own tracked numbers (round 10's snapshot matched his
    remembered post-race-9 budget exactly; round 11's matched post-race-10).
    Falls back to this round's own (lagged) figure if there's no next round
    yet, e.g. mid-way through the final race of the season."""
    gdpoints = entry.get("gdpoints")
    if gdpoints is None:
        return None  # race not played yet — quiet no-op

    own_info    = entry.get("team_info") or {}
    budget_info = (budget_entry or {}).get("team_info") or {}
    if "maxTeambal" in budget_info:
        team_value = budget_info["maxTeambal"]
    elif "maxTeambal" in own_info:
        team_value = own_info["maxTeambal"]   # last-race-of-season fallback, one race lagged
    else:
        team_value = own_info.get("teamVal")  # very old data / unexpected shape fallback

    with conn:
        conn.execute(
            "INSERT INTO race_results (season, round, manager_id, points, season_total, team_value, "
            "team_balance, captain_player_id, is_final, gameday_rank, overall_rank, transfers_made, "
            "inactive_driver_penalty, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(season, round, manager_id) DO UPDATE SET points=excluded.points, "
            "season_total=excluded.season_total, team_value=excluded.team_value, "
            "team_balance=excluded.team_balance, captain_player_id=excluded.captain_player_id, "
            "is_final=excluded.is_final, gameday_rank=excluded.gameday_rank, "
            "overall_rank=excluded.overall_rank, transfers_made=excluded.transfers_made, "
            "inactive_driver_penalty=excluded.inactive_driver_penalty, fetched_at=excluded.fetched_at",
            (
                SEASON, round_no, social_id, gdpoints, entry.get("ovpoints"),
                team_value,
                own_info.get("teamBal"),
                entry.get("capplayerid"),
                int(is_final),
                entry.get("gdrank"),
                entry.get("ovrank"),
                entry.get("usersubs"),
                entry.get("inactive_driver_penality_points"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        conn.execute(
            "DELETE FROM team_picks WHERE season=? AND round=? AND manager_id=?",
            (SEASON, round_no, social_id),
        )
        picks = entry.get("playerid", [])
        if len(picks) > 7:
            # F1's own backend can leave a stale leftover slot on a Wildcard
            # week (confirmed live: a reversed transfer left an 8th entry,
            # with the true final pick at the highest position number) —
            # keep the highest-numbered 7 rather than guess further.
            picks = sorted(picks, key=lambda p: p.get("playerpostion") or 0)[-7:]
        for p in picks:
            pid, pos = p.get("id"), p.get("playerpostion")
            if pid is None or pos is None:
                continue
            conn.execute(
                "INSERT INTO players (id, name, type) VALUES (?, NULL, NULL) "
                "ON CONFLICT(id) DO NOTHING",
                (pid,),
            )
            conn.execute(
                "INSERT INTO team_picks (season, round, manager_id, player_id, position) VALUES (?,?,?,?,?)",
                (SEASON, round_no, social_id, pid, pos),
            )

        for taken_field, gd_field, chip_name in CHIP_FIELDS:
            if entry.get(taken_field):
                conn.execute(
                    "INSERT INTO chips_used (season, manager_id, chip_name, round_taken) VALUES (?,?,?,?) "
                    "ON CONFLICT(season, manager_id, chip_name) DO UPDATE SET round_taken=excluded.round_taken",
                    (SEASON, social_id, chip_name, entry.get(gd_field)),
                )

    return None


def main():
    cookie = load_cookie()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    seed_managers(conn)
    seed_races(conn)

    fatal = False

    # Pass 1: figure out which rounds have data at all, per manager, and the
    # overall highest round with anything — needed before the drivers feed
    # (round-scoped, not per-manager) knows how far to fetch.
    manager_rounds = {}   # social_id -> (with_data, final_rounds)
    max_round = 0
    for name, team_name, social_id, guid_base, is_self in MANAGERS:
        full_guid = f"{guid_base}-0-{social_id}"
        with_data, final_rounds, err = fetch_gameday_statuses(full_guid, cookie)
        if err:
            if "401" in err:
                print(f"FATAL: {err} — stopping, no partial data written for remaining managers.")
                fatal = True
                break
            print(f"  [warn] {name}: couldn't get gameday status ({err}), skipping this run")
            continue
        time.sleep(0.15)
        manager_rounds[social_id] = (with_data, final_rounds)
        if with_data:
            max_round = max(max_round, max(with_data))

    # Every manager failing (e.g. a network blip) isn't a 401, so it never set
    # `fatal` above — but a run that silently touched zero data is exactly
    # the kind of failure the status file exists to surface, not hide behind
    # a false "OK".
    if not fatal and not manager_rounds:
        print("FATAL: no manager's gameday status could be fetched this run (see warnings above).")
        fatal = True

    # Pass 2: the drivers feed is public and round-scoped (not per-manager) —
    # one call per round gives every driver/constructor's real name, points,
    # and session breakdown for everyone at once.
    if not fatal:
        for round_no in range(1, max_round + 1):
            err = fetch_and_store_drivers(conn, round_no, cookie)
            if err:
                print(f"  [warn] drivers feed round {round_no}: {err}")
            time.sleep(0.15)

    # NZ country leaderboard: a public, single-snapshot feed independent of
    # any manager's gameday status. A failure here is non-fatal — it just
    # leaves last run's snapshot in place rather than wiping it.
    if not fatal:
        err = fetch_and_store_nz_leaderboard(conn, cookie)
        if err:
            print(f"  [warn] NZ leaderboard: {err}")
        time.sleep(0.15)

    # Pass 3: each manager's own scores/roster/chips/budget, as before.
    for name, team_name, social_id, guid_base, is_self in MANAGERS:
        if fatal:
            break
        if social_id not in manager_rounds:
            continue
        full_guid = f"{guid_base}-0-{social_id}"
        guid_for_url = guid_base if is_self else full_guid
        with_data, final_rounds = manager_rounds[social_id]

        # A round that used to have data but no longer does (shouldn't happen,
        # but the API is the single source of truth) gets removed rather than
        # left stale.
        if with_data:
            placeholders = ",".join("?" * len(with_data))
            conn.execute(
                f"DELETE FROM race_results WHERE season=? AND manager_id=? AND round NOT IN ({placeholders})",
                (SEASON, social_id, *with_data),
            )
            conn.execute(
                f"DELETE FROM team_picks WHERE season=? AND manager_id=? AND round NOT IN ({placeholders})",
                (SEASON, social_id, *with_data),
            )
        else:
            conn.execute("DELETE FROM race_results WHERE season=? AND manager_id=?", (SEASON, social_id))
            conn.execute("DELETE FROM team_picks WHERE season=? AND manager_id=?", (SEASON, social_id))
        conn.commit()

        entry_cache = {}

        def get_entry(round_no):
            if round_no not in entry_cache:
                e, e_err = fetch_team_entry(guid_for_url, is_self, team_name, round_no, cookie)
                entry_cache[round_no] = e
                time.sleep(0.15)
                if e_err:
                    return None, e_err
            return entry_cache[round_no], None

        for round_no in sorted(with_data):
            entry, err = get_entry(round_no)
            if err:
                if "401" in err:
                    print(f"FATAL: {err} — stopping, no partial data written for remaining rounds/managers.")
                    fatal = True
                    break
                print(f"  [warn] {name} round {round_no}: {err}")
                continue
            if entry is None:
                continue

            # Peek at round_no+1 purely for its team_info (see docstring on
            # process_manager_round) — errors here don't block the round's
            # own points/roster/chips, just fall back to this round's own
            # (one-race-lagged) budget figure.
            budget_entry, budget_err = get_entry(round_no + 1)
            if budget_err:
                budget_entry = None

            process_manager_round(conn, social_id, round_no, entry, budget_entry, round_no in final_rounds)
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
