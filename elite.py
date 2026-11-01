"""Elite Data for the Tools tab (1 Nov 2026): what the global top 500 pick.

After a round locks, F1 Fantasy's public leaderboard feed lists the top 500
with the team each one locked in (one call, no login): that gives pick %.
DRS (2x), Extra DRS (3x) and chips need each player's own team call, the
same opponent endpoint the league fetch uses with Tim's cookie. That's 500
calls a round, so they're spread out: at most PER_RUN a run, PAUSE seconds
apart, from run_pipeline.sh's full runs (about hourly), done in a few hours.

Tables: elite_teams (one row per top-500 team per round) and the DRS/chip
columns filled in as the calls come back. Starts from Mexico 2026 (round
19): the feed only ever shows the current teams, so earlier rounds can't be
filled in without ~9,000 calls.

  python3 elite.py            snapshot if due, then fill in details
  python3 elite.py --status   counts per round
"""
import calendar
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import fetch_f1_data as f1
import notify

DB = Path(__file__).resolve().parent / "f1_data.db"
LIST_URL = "https://fantasy.formula1.com/feeds/leaderboard/public/global/list_1_0_1.json"
PER_RUN = 200
PAUSE = 1.2
# The round each chip was played in. Wildcard's is is_wildcard_taken_gd_id:
# wildcardtakengd is always 0 (found 1 Nov 2026).
CHIP_FIELDS = {"is_wildcard_taken_gd_id": "Wildcard", "limitlesstakengd": "Limitless", "extradrstakengd": "Extra DRS",
               "nonigativetakengd": "No Negative", "autopilottakengd": "Auto Pilot", "finalfixtakengd": "Final Fix"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS elite_teams (
    season INTEGER, round INTEGER, pos INTEGER, rank INTEGER, social_id TEXT, team_no INTEGER,
    guid TEXT, team_name TEXT, picks TEXT, season_points REAL,
    captain TEXT, mega_captain TEXT, chip TEXT, round_points REAL, detail_at TEXT, detail_err TEXT,
    chip_rounds TEXT,
    PRIMARY KEY (season, round, pos));
"""


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "undercut-collective", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def locked_round(now):
    """The round whose team lock passed most recently (and its lock time)."""
    locks = notify.fetch_locks()
    past = [(t, r) for r, t in locks.items() if t <= now]
    return (max(past)[1], max(past)[0]) if past else (None, None)


def snapshot(conn, season, now):
    rnd, lock = locked_round(now)
    if rnd is None or now.timestamp() - lock.timestamp() < 1800:
        return None   # give the feed half an hour after the lock to show locked teams
    if conn.execute("SELECT 1 FROM elite_teams WHERE season=? AND round=? LIMIT 1", (season, rnd)).fetchone():
        return None
    d = get_json(LIST_URL)
    feed_time = d.get("FeedTime", {}).get("UTCTime")
    lb = d["Value"]["leaderboard"]
    # The feed must have been made after the lock, or it still holds last round's teams.
    try:
        made = calendar.timegm(time.strptime(feed_time, "%m/%d/%Y %I:%M:%S %p"))
        if made < lock.timestamp():
            print(f"elite: feed is from before R{rnd}'s lock ({feed_time}); waiting")
            return None
    except (TypeError, ValueError):
        pass
    # pos = place in the feed: players tied on points share cur_rank.
    for pos, e in enumerate(lb, 1):
        conn.execute("INSERT OR REPLACE INTO elite_teams (season, round, pos, rank, social_id, team_no, guid, "
                     "team_name, picks, season_points) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (season, rnd, pos, int(e["cur_rank"]), str(e["social_id"]), int(e.get("team_no") or 1),
                      e["user_guid"], urllib.parse.unquote(e.get("team_name", "")),
                      json.dumps([str(p) for p in e.get("user_team", [])]), e.get("cur_points")))
    conn.commit()
    print(f"elite: R{rnd} snapshot, {len(lb)} teams (feed {feed_time})")
    return rnd


def detail_url(guid, team_no, rnd):
    return ("https://fantasy.formula1.com/services/user/opponentteam/opponentgamedayplayerteamget/1/"
            f"{guid}/{team_no}/{rnd}/1?buster={int(time.time() * 1000)}")


def fill_details(conn, season, limit=PER_RUN):
    rows = conn.execute("SELECT round, pos, guid, team_no FROM elite_teams WHERE season=? AND detail_at IS NULL "
                        "ORDER BY round DESC, pos LIMIT ?", (season, limit)).fetchall()
    if not rows:
        return 0
    cookie = f1.load_cookie()
    done = fails = 0
    for rnd, pos, guid, team_no in rows:
        payload, err = f1.fetch_json(detail_url(guid, team_no, rnd), cookie)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if err or not payload:
            fails += 1
            conn.execute("UPDATE elite_teams SET detail_at=?, detail_err=? WHERE season=? AND round=? AND pos=?",
                         (now, (err or "empty")[:200], season, rnd, pos))
            if "401" in str(err):
                print("elite: cookie refused; stopping until the next run")
                conn.commit()
                break
        else:
            t = (payload["Data"]["Value"].get("userTeam") or [{}])[0]
            chip = next((name for k, name in CHIP_FIELDS.items() if str(t.get(k)) == str(rnd)), None)
            # Every chip's round as F1 gave it, so a mapping fix never needs the calls again.
            raw = json.dumps({k: t.get(k) for k in t if "taken" in k.lower()})
            conn.execute("UPDATE elite_teams SET captain=?, mega_captain=?, chip=?, round_points=?, detail_at=?, "
                         "detail_err=NULL, chip_rounds=? WHERE season=? AND round=? AND pos=?",
                         (t.get("capplayerid"), t.get("mgcapplayerid"), chip, t.get("gdpoints"), now, raw,
                          season, rnd, pos))
            done += 1
        if (done + fails) % 20 == 0:
            conn.commit()
        time.sleep(PAUSE)
    conn.commit()
    print(f"elite: details for {done} teams ({fails} failed)")
    return done


def main():
    import datetime as dt
    conn = sqlite3.connect(DB, timeout=60)
    conn.executescript(SCHEMA)
    if "chip_rounds" not in {r[1] for r in conn.execute("PRAGMA table_info(elite_teams)")}:
        conn.execute("ALTER TABLE elite_teams ADD COLUMN chip_rounds TEXT")
    season = conn.execute("SELECT MAX(season) FROM races").fetchone()[0]
    if "--status" in sys.argv:
        for r in conn.execute("SELECT round, COUNT(*), SUM(detail_at IS NOT NULL), SUM(detail_err IS NOT NULL) "
                              "FROM elite_teams WHERE season=? GROUP BY round", (season,)):
            print(f"R{r[0]}: {r[1]} teams, {r[2]} with details ({r[3]} failed)")
        return
    snapshot(conn, season, dt.datetime.now(dt.timezone.utc))
    fill_details(conn, season)


if __name__ == "__main__":
    main()
