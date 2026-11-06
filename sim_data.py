"""Data for the race simulation (sim.py, TOOLS_PLAN.md step B).

Fetches into f1_data.db, cached so each run only asks for what's new:
  hist_results   every race and sprint result 2022 onwards (Jolpica): grid,
                 finish, classified or not, fastest-lap rank
  hist_quali     every qualifying session 2022 onwards (Jolpica): Q1/Q2/Q3
  practice_laps  this season's practice and qualifying laps per driver
                 (OpenF1): best lap and long-run average per session
  starting_grid  the race's starting grid with penalties applied (OpenF1,
                 published after qualifying and refetched until the race)

2022 is the start of the current car rules, so older seasons say little about
how hard a track is to pass on. Run on its own: python3 sim_data.py
"""
import calendar
import json
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parent / "f1_data.db"
JOLPICA = "https://api.jolpi.ca/ergast/f1"
OPENF1 = "https://api.openf1.org/v1"
FIRST_SEASON = 2022

# Jolpica constructorId -> F1 Fantasy team id (a constructor's player id).
TEAM_IDS = {
    "mclaren": "27", "mercedes": "28", "red_bull": "29", "ferrari": "25",
    "alpine": "23", "aston_martin": "24", "williams": "210", "rb": "2636",
    "haas": "26", "audi": "2640", "sauber": "2640", "cadillac": "2641",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_results (
    season INTEGER, round INTEGER, kind TEXT, circuit TEXT, driver TEXT,
    code TEXT, team TEXT, grid INTEGER, pos INTEGER, classified INTEGER,
    status TEXT, laps INTEGER, fl_rank INTEGER,
    PRIMARY KEY (season, round, kind, driver));
CREATE TABLE IF NOT EXISTS hist_quali (
    season INTEGER, round INTEGER, circuit TEXT, driver TEXT, code TEXT,
    team TEXT, pos INTEGER, q1 REAL, q2 REAL, q3 REAL,
    PRIMARY KEY (season, round, driver));
CREATE TABLE IF NOT EXISTS hist_rounds (
    season INTEGER, round INTEGER, circuit TEXT, name TEXT, date TEXT,
    sprint INTEGER, PRIMARY KEY (season, round));
CREATE TABLE IF NOT EXISTS practice_laps (
    season INTEGER, round INTEGER, session TEXT, code TEXT, best REAL,
    long_run REAL, laps INTEGER, PRIMARY KEY (season, round, session, code));
CREATE TABLE IF NOT EXISTS starting_grid (
    season INTEGER, round INTEGER, code TEXT, pos INTEGER, fetched_at TEXT,
    PRIMARY KEY (season, round, code));
CREATE TABLE IF NOT EXISTS fetch_marks (key TEXT PRIMARY KEY, at TEXT);
"""


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "undercut-collective"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(4 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"gave up on {url}")


def jolpica_all(path, key):
    """Every page of a Jolpica list (100 rows a page)."""
    races, offset = {}, 0
    while True:
        d = get(f"{JOLPICA}/{path}.json?limit=100&offset={offset}")["MRData"]
        for race in d["RaceTable"]["Races"]:
            r = races.setdefault(race["round"], {**race, key: []})
            r[key].extend(race.get(key, []))
        offset += 100
        if offset >= int(d["total"]):
            return list(races.values())
        time.sleep(0.3)


def laptime(s):
    if not s:
        return None
    try:
        m, rest = s.split(":") if ":" in s else ("0", s)
        return int(m) * 60 + float(rest)
    except ValueError:
        return None


def code_of(drv):
    return drv.get("code") or drv["familyName"][:3].upper()


def fetch_season(conn, season):
    sched = get(f"{JOLPICA}/{season}.json?limit=100")["MRData"]["RaceTable"]["Races"]
    for r in sched:
        conn.execute("INSERT OR REPLACE INTO hist_rounds VALUES (?,?,?,?,?,?)",
                     (season, int(r["round"]), r["Circuit"]["circuitId"], r["raceName"],
                      r["date"], int("Sprint" in r)))
    for kind, path, key in (("race", f"{season}/results", "Results"),
                            ("sprint", f"{season}/sprint", "SprintResults")):
        for race in jolpica_all(path, key):
            for x in race[key]:
                classified = x.get("positionText", "").isdigit()
                fl = x.get("FastestLap", {}).get("rank")
                conn.execute(
                    "INSERT OR REPLACE INTO hist_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (season, int(race["round"]), kind, race["Circuit"]["circuitId"],
                     x["Driver"]["driverId"], code_of(x["Driver"]),
                     TEAM_IDS.get(x["Constructor"]["constructorId"], x["Constructor"]["constructorId"]),
                     int(x["grid"]), int(x["position"]), int(classified), x.get("status"),
                     int(x.get("laps") or 0), int(fl) if fl else None))
        time.sleep(0.5)
    for race in jolpica_all(f"{season}/qualifying", "QualifyingResults"):
        for x in race["QualifyingResults"]:
            conn.execute(
                "INSERT OR REPLACE INTO hist_quali VALUES (?,?,?,?,?,?,?,?,?,?)",
                (season, int(race["round"]), race["Circuit"]["circuitId"],
                 x["Driver"]["driverId"], code_of(x["Driver"]),
                 TEAM_IDS.get(x["Constructor"]["constructorId"], x["Constructor"]["constructorId"]),
                 int(x["position"]), laptime(x.get("Q1")), laptime(x.get("Q2")), laptime(x.get("Q3"))))
    conn.commit()


def mark(conn, key):
    conn.execute("INSERT OR REPLACE INTO fetch_marks VALUES (?, datetime('now'))", (key,))
    conn.commit()


def marked(conn, key):
    return conn.execute("SELECT 1 FROM fetch_marks WHERE key=?", (key,)).fetchone() is not None


def long_run_average(laps):
    """Mean of the laps in the longest green-flag run of 5+ laps on one set of
    tyres (no pit in/out laps, nothing over 107% of that run's median)."""
    best = None
    run = []
    for lap in laps + [None]:
        if lap is None or lap["pit_out"] or lap["t"] is None:
            if len(run) >= 5:
                med = statistics.median(run)
                ok = [t for t in run if t <= med * 1.07]
                if len(ok) >= 5 and (best is None or len(ok) > best[0]):
                    best = (len(ok), sum(ok) / len(ok))
            run = []
            continue
        run.append(lap["t"])
    return best[1] if best else None


def fetch_practice(conn, season, rnd, date):
    """OpenF1 laps for a round's practice and qualifying sessions."""
    meetings = get(f"{OPENF1}/meetings?year={season}")
    # The race weekend's meeting starts 0-3 days before the race date.
    race_day = calendar.timegm(time.strptime(date, "%Y-%m-%d"))
    m = None
    for x in meetings:
        start = calendar.timegm(time.strptime(x["date_start"][:10], "%Y-%m-%d"))
        if 0 <= race_day - start <= 4 * 86400 and "test" not in x.get("meeting_name", "").lower():
            m = x
    if not m:
        return 0
    n = 0
    for s in get(f"{OPENF1}/sessions?meeting_key={m['meeting_key']}"):
        name = s["session_name"]
        if not (name.startswith("Practice") or name in ("Qualifying", "Sprint Qualifying", "Sprint Shootout")):
            continue
        if name == "Qualifying":
            fetch_grid(conn, season, rnd, s["session_key"])
        key = f"practice:{season}:{rnd}:{name}"
        if marked(conn, key):
            continue
        drivers = {d["driver_number"]: d["name_acronym"]
                   for d in get(f"{OPENF1}/drivers?session_key={s['session_key']}")}
        laps = get(f"{OPENF1}/laps?session_key={s['session_key']}")
        if not laps:
            continue   # not run yet, or not published yet
        by = {}
        for lap in sorted(laps, key=lambda l: (l["driver_number"], l["lap_number"])):
            by.setdefault(lap["driver_number"], []).append(
                {"t": lap.get("lap_duration"), "pit_out": bool(lap.get("is_pit_out_lap"))})
        for num, ls in by.items():
            times = [l["t"] for l in ls if l["t"]]
            if not times or num not in drivers:
                continue
            conn.execute("INSERT OR REPLACE INTO practice_laps VALUES (?,?,?,?,?,?,?)",
                         (season, rnd, name, drivers[num], min(times),
                          long_run_average(ls), len(times)))
        conn.commit()
        # A session is final a few hours after it ends; until then refetch.
        end = calendar.timegm(time.strptime(s["date_end"][:19], "%Y-%m-%dT%H:%M:%S"))
        if time.time() - end > 3 * 3600:
            mark(conn, key)
        n += 1
        time.sleep(0.4)
    return n


def fetch_grid(conn, season, rnd, quali_key):
    """OpenF1's starting grid (keyed by the qualifying session): qualifying
    order with grid penalties applied. Refetched every run until the race,
    as penalties can land late. Checked on Austin 2026: matches the real grid
    for 21 of 22 cars (the miss was a pit-lane start decided on race day)."""
    try:
        grid = get(f"{OPENF1}/starting_grid?session_key={quali_key}")
    except urllib.error.HTTPError:
        return
    if not grid:
        return
    num = {d["driver_number"]: d["name_acronym"] for d in get(f"{OPENF1}/drivers?session_key={quali_key}")}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("DELETE FROM starting_grid WHERE season=? AND round=?", (season, rnd))
    for g in grid:
        if g.get("driver_number") in num and g.get("position"):
            conn.execute("INSERT OR REPLACE INTO starting_grid VALUES (?,?,?,?,?)",
                         (season, rnd, num[g["driver_number"]], int(g["position"]), now))
    conn.commit()


def update(conn, current_season, practice_rounds=None):
    conn.executescript(SCHEMA)
    for season in range(FIRST_SEASON, current_season):
        if not marked(conn, f"season:{season}"):
            fetch_season(conn, season)
            mark(conn, f"season:{season}")
    fetch_season(conn, current_season)   # the current season every time (cheap)
    rounds = conn.execute("SELECT round, date FROM hist_rounds WHERE season=? ORDER BY round",
                          (current_season,)).fetchall()
    today = time.strftime("%Y-%m-%d")
    for rnd, date in rounds:
        if practice_rounds is not None and rnd not in practice_rounds:
            continue
        # Practice starts two days before the race date.
        if date <= time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3 * 86400)) and \
           not marked(conn, f"practice_done:{current_season}:{rnd}"):
            try:
                fetch_practice(conn, current_season, rnd, date)
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    raise
                # OpenF1 shuts its free API while a session is live, from about
                # 40 minutes before it; the next hourly run picks it up.
                print(f"OpenF1 locked for a live session; R{rnd} practice waits for the next run")
                continue
            if date < today and conn.execute(
                    "SELECT COUNT(*) FROM fetch_marks WHERE key LIKE ?",
                    (f"practice:{current_season}:{rnd}:%",)).fetchone()[0] >= 3:
                mark(conn, f"practice_done:{current_season}:{rnd}")


if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else int(time.strftime("%Y"))
    conn = sqlite3.connect(DB)
    update(conn, season)
    for t in ("hist_rounds", "hist_results", "hist_quali", "practice_laps"):
        print(t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
