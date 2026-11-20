"""Weather forecast for each session of the coming race weekends, for the
Tools tab (Tim, 21 Nov 2026).

Session times come from OpenF1 (it lists a weekend's timetable in advance),
the forecast from Open-Meteo (free, no key): hourly temperature, chance of
rain, rain, wind and sky at the circuit, summarised over each session.

Runs at the top of every run_pipeline.sh run and decides for itself whether a
round is due: every 12 hours from 10 days before FP1, every hour from 3 days
before FP1 until the race is over. The Tools tab shows the latest forecast;
gate_check.py rebuilds the site when it changes enough to matter (see
summary()). The simulation doesn't use the weather.

  python3 weather.py            (what's due)
  python3 weather.py --force    (every coming round, now)
"""
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / "f1_data.db"
OPENF1 = "https://api.openf1.org/v1"
METEO = "https://api.open-meteo.com/v1/forecast"
GEO = "https://geocoding-api.open-meteo.com/v1/search"
LOOKAHEAD = timedelta(days=10)     # Open-Meteo forecasts 16 days; further out is guesswork
CLOSE = timedelta(days=3)          # hourly from this long before FP1
FAR_EVERY = timedelta(hours=12)
CLOSE_EVERY = timedelta(minutes=55)
AFTER_RACE = timedelta(hours=3)

# Circuit (hist_rounds.circuit, Jolpica's id) -> latitude, longitude.
CIRCUITS = {
    "albert_park": (-37.8497, 144.9680), "shanghai": (31.3389, 121.2197), "suzuka": (34.8431, 136.5407),
    "miami": (25.9581, -80.2389), "villeneuve": (45.5000, -73.5228), "monaco": (43.7347, 7.4206),
    "catalunya": (41.5700, 2.2611), "red_bull_ring": (47.2197, 14.7647), "silverstone": (52.0786, -1.0169),
    "spa": (50.4372, 5.9714), "hungaroring": (47.5789, 19.2486), "zandvoort": (52.3888, 4.5409),
    "monza": (45.6156, 9.2811), "madring": (40.4637, -3.6163), "baku": (40.3725, 49.8533),
    "sepang": (2.7608, 101.7382), "marina_bay": (1.2914, 103.8640), "americas": (30.1328, -97.6411),
    "rodriguez": (19.4042, -99.0907), "interlagos": (-23.7036, -46.6997), "vegas": (36.1147, -115.1728),
    "losail": (25.4900, 51.4542), "yas_marina": (24.4672, 54.6031), "bahrain": (26.0325, 50.5106),
    "jeddah": (21.6319, 39.1044), "imola": (44.3439, 11.7167),
}
SHORT = {"Practice 1": "FP1", "Practice 2": "FP2", "Practice 3": "FP3", "Sprint Qualifying": "Sprint quali",
         "Sprint Shootout": "Sprint quali", "Qualifying": "Quali", "Sprint": "Sprint", "Race": "Race"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "undercut-collective/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS weather (
        season INTEGER, round INTEGER, session TEXT, start_utc TEXT, end_utc TEXT,
        temp REAL, rain_prob INTEGER, rain_mm REAL, wind REAL, code INTEGER, fetched_at TEXT,
        PRIMARY KEY (season, round, session))""")
    conn.execute("CREATE TABLE IF NOT EXISTS weather_checks (season INTEGER, round INTEGER, checked_at TEXT, "
                 "ok INTEGER, note TEXT, PRIMARY KEY (season, round))")


def coming(conn, season, now):
    """Rounds whose race isn't over and whose FP1 is within LOOKAHEAD."""
    out = []
    for rnd, fp1, race in conn.execute("SELECT round, fp1_utc, race_utc FROM race_schedule WHERE season=? "
                                       "AND fp1_utc IS NOT NULL AND race_utc IS NOT NULL ORDER BY round", (season,)):
        fp1, race = datetime.fromisoformat(fp1), datetime.fromisoformat(race)
        if race + AFTER_RACE > now and fp1 - LOOKAHEAD < now:
            out.append((rnd, fp1, race))
    return out


def sessions(season, race):
    """OpenF1's timetable for the weekend whose race starts at `race`."""
    meetings = get(f"{OPENF1}/meetings?year={season}")
    for m in meetings:
        start = datetime.fromisoformat(m["date_start"])
        if timedelta(0) <= race - start <= timedelta(days=5) and "test" not in m.get("meeting_name", "").lower():
            ss = get(f"{OPENF1}/sessions?meeting_key={m['meeting_key']}")
            return m, [s for s in ss if not s.get("is_cancelled")]
    return None, []


def where(conn, season, rnd, meeting):
    row = conn.execute("SELECT circuit FROM hist_rounds WHERE season=? AND round=?", (season, rnd)).fetchone()
    if row and row[0] in CIRCUITS:
        return CIRCUITS[row[0]]
    if meeting:   # a circuit not in the list: look its town up
        q = urllib.parse.quote(meeting.get("location") or meeting.get("circuit_short_name") or "")
        r = get(f"{GEO}?name={q}&count=1")
        if r.get("results"):
            return r["results"][0]["latitude"], r["results"][0]["longitude"]
    return None


def summarise(hourly, start, end):
    """One session's forecast: hours from the one it starts in to the one it ends in."""
    t0 = start.replace(minute=0, second=0, microsecond=0)
    idx = [i for i, t in enumerate(hourly["time"])
           if t0 <= datetime.fromisoformat(t).replace(tzinfo=timezone.utc) < end]
    if not idx:
        return None
    pick = lambda k: [hourly[k][i] for i in idx if hourly[k][i] is not None]
    temp, prob, rain, wind, code = (pick(k) for k in ("temperature_2m", "precipitation_probability",
                                                       "precipitation", "wind_speed_10m", "weather_code"))
    if not temp:
        return None
    return {"temp": round(sum(temp) / len(temp), 1), "rain_prob": max(prob) if prob else None,
            "rain_mm": round(sum(rain), 1) if rain else None, "wind": round(max(wind)) if wind else None,
            "code": max(code) if code else None}   # WMO codes rise with severity


def fetch_round(conn, season, rnd, race):
    meeting, ss = sessions(season, race)
    if not ss:
        raise RuntimeError("OpenF1 has no timetable for this weekend yet")
    loc = where(conn, season, rnd, meeting)
    if not loc:
        raise RuntimeError("don't know where the circuit is")
    first = min(datetime.fromisoformat(s["date_start"]) for s in ss)
    days = max(1, min(16, (race - datetime.now(timezone.utc)).days + 2))
    w = get(f"{METEO}?latitude={loc[0]}&longitude={loc[1]}&timezone=UTC&past_days=2&forecast_days={days}"
            "&hourly=temperature_2m,precipitation_probability,precipitation,wind_speed_10m,weather_code")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for s in ss:
        name = SHORT.get(s["session_name"], s["session_name"])
        start, end = datetime.fromisoformat(s["date_start"]), datetime.fromisoformat(s["date_end"])
        f = summarise(w["hourly"], start, end)
        if not f:
            continue   # too far out for the forecast
        conn.execute("INSERT OR REPLACE INTO weather VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (season, rnd, name, start.isoformat(), end.isoformat(), f["temp"], f["rain_prob"],
                      f["rain_mm"], f["wind"], f["code"], now))
        n += 1
    return n, first


def due(conn, season, rnd, fp1, now):
    row = conn.execute("SELECT checked_at, ok FROM weather_checks WHERE season=? AND round=?",
                       (season, rnd)).fetchone()
    if not row:
        return True
    every = CLOSE_EVERY if now >= fp1 - CLOSE or not row[1] else FAR_EVERY   # a failed check retries in an hour
    return now - datetime.fromisoformat(row[0]) >= every


def summary(conn, season):
    """Coarse version of the forecasts for gate_check.py: the site is rebuilt
    when one of these moves (10% rain chance, 2 degrees, 10 km/h, the sky)."""
    try:
        rows = conn.execute("SELECT round, session, rain_prob, temp, wind, code FROM weather WHERE season=? "
                            "ORDER BY round, start_utc", (season,)).fetchall()
    except sqlite3.OperationalError:
        return []
    sky = lambda c: None if c is None else 0 if c <= 1 else 1 if c <= 3 else 2 if c <= 48 else 3 if c < 95 else 4
    return [[r, s, None if p is None else p // 10, None if t is None else round(t / 2),
             None if w is None else int(w // 10), sky(c)] for r, s, p, t, w, c in rows]


def main():
    force = "--force" in sys.argv
    conn = sqlite3.connect(DB)
    init(conn)
    now = datetime.now(timezone.utc)
    season = now.year
    for rnd, fp1, race in coming(conn, season, now):
        if not force and not due(conn, season, rnd, fp1, now):
            continue
        try:
            n, _ = fetch_round(conn, season, rnd, race)
            ok, note = 1, f"{n} sessions"
        except Exception as e:   # never fatal: the box just shows the last forecast
            ok, note = 0, str(e)[:200]
        conn.execute("INSERT OR REPLACE INTO weather_checks VALUES (?,?,?,?,?)",
                     (season, rnd, now.isoformat(timespec="seconds"), ok, note))
        conn.commit()
        print(f"weather R{rnd}: {note}")
        time.sleep(1)


if __name__ == "__main__":
    main()
