#!/usr/bin/env python3
"""Email Tim when the F1 session cookie needs refreshing -- but only when it
actually matters, i.e. around a race weekend.

Background: the F1 cookie is a hard 96-hour JWT. There is no refresh endpoint,
and automated re-login is blocked by Imperva (both investigated 2026-08-25), so
a human has to paste a fresh one periodically. Nagging every 4 days would be
noise: the cookie only needs to be ALIVE around a race weekend. Between races
nothing is being scored, so an expired cookie is harmless.

Note a hard constraint: 96 hours does NOT stretch from FP1 to race+48h (~4.4
days). So we deliberately do not demand one cookie cover a whole weekend --
we nag at up to two useful moments instead, which is how Tim asked for it:

  pre    ~36h before FP1, if the cookie won't survive past the race.
  post   after the race, if the round still isn't finalised and the cookie is
         dead or about to die (F1 Fantasy can be slow to finalise scoring).
  urgent the fetch is actually failing inside a race weekend.

At most one mail per category per day. Silent the rest of the season.

  --status  print the decision, don't email
  --test    send a test email to prove msmtp still works
"""
import base64
import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
COOKIE_PATH = BASE_DIR / "secrets" / "f1_cookie.txt"
STATUS_PATH = BASE_DIR / "secrets" / "last_run_status.txt"
NTFY_TOKEN_PATH = BASE_DIR / "secrets" / "ntfy_token.txt"
DB_PATH = BASE_DIR / "f1_data.db"

SEASON = 2026
MAILTO = "timstewart308@outlook.com"
NZ = ZoneInfo("Pacific/Auckland")

# Push notifications via the self-hosted ntfy at /opt/docker/ntfy.
# Published through `docker exec` rather than over the network: ntfy follows the
# house convention of publishing no host ports, so it is only reachable on the
# `proxy` docker network -- but the phone reaches it via NPM on
# ntfy.simsey.co.nz. The token is read from secrets/ (gitignored) because this
# file is committed to a PUBLIC repo.
NTFY_CONTAINER = "ntfy"
NTFY_TOPIC_URL = "http://localhost:80/f1-fantasy"

LEAD_HOURS = 36        # start the pre-weekend nag this far before FP1
NEED_MARGIN_HOURS = 6  # cookie should outlive the race by at least this much
POST_GRACE_HOURS = 48  # how long after a race we still care about finalising
DYING_SOON_HOURS = 6   # "about to die" threshold for the post-race nag
THROTTLE_HOURS = 20    # at most ~one mail per category per day

CALENDAR_URL = (f"https://api.jolpi.ca/ergast/f1/{SEASON}/races/"
                "?format=json&limit=30")
CALENDAR_MAX_AGE_DAYS = 7


# ---------------------------------------------------------------- storage

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS race_schedule (
            season INTEGER NOT NULL,
            round  INTEGER NOT NULL,
            name   TEXT,
            fp1_utc  TEXT,
            race_utc TEXT,
            PRIMARY KEY (season, round)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    return conn


def state_get(conn, key):
    row = conn.execute("SELECT value FROM watch_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def state_set(conn, key, value):
    conn.execute("INSERT INTO watch_state(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


# ---------------------------------------------------------------- calendar

def parse_dt(date_s, time_s):
    if not date_s:
        return None
    time_s = (time_s or "00:00:00Z").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(f"{date_s}T{time_s}")
    except ValueError:
        return None


def refresh_calendar(conn, force=False):
    """Cached for a week -- dates rarely move, and a stale calendar beats a
    crash on a flaky network."""
    last = state_get(conn, "calendar_fetched_at")
    if not force and last:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last) < \
                    timedelta(days=CALENDAR_MAX_AGE_DAYS):
                return True, "cached"
        except ValueError:
            pass

    req = urllib.request.Request(
        CALENDAR_URL, headers={"User-Agent": "f1-fantasy-homelab/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    if not races:
        return False, "calendar response had no races"

    for rc in races:
        try:
            rnd = int(rc["round"])
        except (KeyError, ValueError):
            continue
        race = parse_dt(rc.get("date"), rc.get("time"))
        if race is None:
            continue
        fp1 = parse_dt(rc.get("FirstPractice", {}).get("date"),
                       rc.get("FirstPractice", {}).get("time"))
        if fp1 is None:                      # sprint/odd formats: assume Friday
            fp1 = race - timedelta(days=2)
        conn.execute(
            "INSERT INTO race_schedule(season, round, name, fp1_utc, race_utc) "
            "VALUES(?,?,?,?,?) ON CONFLICT(season, round) DO UPDATE SET "
            "name=excluded.name, fp1_utc=excluded.fp1_utc, race_utc=excluded.race_utc",
            (SEASON, rnd, rc.get("raceName"), fp1.isoformat(), race.isoformat()))
    conn.commit()
    state_set(conn, "calendar_fetched_at", datetime.now(timezone.utc).isoformat())
    return True, f"fetched {len(races)} races"


def current_or_next_race(conn, now):
    """The race we care about: the next one whose post-race grace hasn't run out."""
    rows = conn.execute(
        "SELECT round, name, fp1_utc, race_utc FROM race_schedule "
        "WHERE season=? ORDER BY round", (SEASON,)).fetchall()
    for rnd, name, fp1_s, race_s in rows:
        try:
            fp1 = datetime.fromisoformat(fp1_s)
            race = datetime.fromisoformat(race_s)
        except (TypeError, ValueError):
            continue
        if now <= race + timedelta(hours=POST_GRACE_HOURS):
            return {"round": rnd, "name": name, "fp1": fp1, "race": race}
    return None


def is_finalized(conn, rnd):
    row = conn.execute(
        "SELECT 1 FROM round_finalized_at WHERE season=? AND round=?",
        (SEASON, rnd)).fetchone()
    return row is not None


# ---------------------------------------------------------------- cookie

def cookie_expiry():
    """Earliest expiry among the session JWTs in the cookie file."""
    if not COOKIE_PATH.exists():
        return None
    raw = COOKIE_PATH.read_text(encoding="utf-8-sig").strip()
    best = None
    for pair in raw.split(";"):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        if name.strip() not in ("login-session", "F1_FANTASY_007"):
            continue
        parts = value.strip().split(".")
        if len(parts) < 2:
            continue
        s = parts[1].replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        try:
            claims = json.loads(base64.b64decode(s))
        except Exception:
            continue
        if "exp" not in claims:
            continue
        exp = datetime.fromtimestamp(claims["exp"], timezone.utc)
        if best is None or exp < best:
            best = exp
    return best


def last_run_failed():
    if not STATUS_PATH.exists():
        return False
    return STATUS_PATH.read_text(encoding="utf-8", errors="replace").startswith("FAILED")


# ---------------------------------------------------------------- email

def send_mail(subject, body):
    msg = f"Subject: {subject}\nTo: {MAILTO}\n\n{body}"
    try:
        p = subprocess.run(["msmtp", MAILTO], input=msg, text=True,
                           capture_output=True, timeout=60)
        if p.returncode != 0:
            print(f"[watch] msmtp failed ({p.returncode}): {p.stderr[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[watch] msmtp error: {type(e).__name__}: {e}")
        return False


def send_push(title, message, priority="default", tags="racing_car"):
    """Push to the phone via ntfy. Best-effort: a push failure must never stop
    the email going out, since email is the backstop."""
    if not NTFY_TOKEN_PATH.exists():
        print("[watch] no ntfy token -- skipping push")
        return False
    token = NTFY_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        print("[watch] ntfy token file empty -- skipping push")
        return False
    cmd = ["docker", "exec", NTFY_CONTAINER, "ntfy", "publish",
           "--token", token, "--title", title,
           "--priority", priority, "--tags", tags,
           NTFY_TOPIC_URL, message]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if p.returncode != 0:
            print(f"[watch] ntfy publish failed ({p.returncode}): "
                  f"{(p.stderr or p.stdout)[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[watch] ntfy error: {type(e).__name__}: {e}")
        return False


# iOS shows the title prominently, so keep the body short and actionable --
# the full instructions live in the email.
PUSH_PRIORITY = {"urgent": ("urgent", "rotating_light"),
                 "post": ("high", "checkered_flag"),
                 "pre": ("default", "racing_car")}


def throttled(conn, category, now):
    last = state_get(conn, f"last_mail_{category}")
    if not last:
        return False
    try:
        return (now - datetime.fromisoformat(last)) < timedelta(hours=THROTTLE_HOURS)
    except ValueError:
        return False


def nz(dt):
    if dt is None:
        return "unknown"
    return dt.astimezone(NZ).strftime("%a %d %b %Y, %I:%M %p NZT").replace(" 0", " ")


REFRESH_HOWTO = """How to refresh (about 60 seconds):
  1. Open https://fantasy.formula1.com in your browser, logged in.
  2. F12 -> Network tab -> filter box: services/user
  3. Click any 200 row -> Headers -> Request Headers ->
     right-click "Cookie:" -> Copy value.
  4. From your PC:
       ssh sentinel-admin@100.69.34.6 'cat > /home/sentinel-admin/f1-fantasy/secrets/f1_cookie.txt'
     paste, press Enter, then Ctrl+D.

That buys another 96 hours."""


# ---------------------------------------------------------------- main

def decide(conn, now, race, exp, failed):
    """Returns (category, subject, body) or (None, None, None)."""
    dead = exp is None or exp <= now
    need_by = race["race"] + timedelta(hours=NEED_MARGIN_HOURS)
    covers_race = exp is not None and exp >= need_by
    finalized = is_finalized(conn, race["round"])
    name = race["name"]

    # urgent: fetching is actually broken while the weekend is under way
    if failed and race["fp1"] - timedelta(hours=LEAD_HOURS) <= now:
        return ("urgent",
                f"[F1] Fetch FAILING during {name} weekend - refresh cookie",
                f"The hourly fetch is failing during the {name} weekend, so "
                f"scores are not being collected.\n\n"
                f"  cookie expiry : {nz(exp)}\n"
                f"  last fetch    : FAILED\n"
                f"  race          : {nz(race['race'])}\n\n"
                f"{REFRESH_HOWTO}\n")

    # post-race: round hasn't finalised yet and the cookie won't see it through
    if now > race["race"] and not finalized and \
            (dead or exp <= now + timedelta(hours=DYING_SOON_HOURS)):
        return ("post",
                f"[F1] Refresh cookie - {name} scores not final yet",
                f"{name} has finished but the round is not finalised in the "
                f"database yet, and the cookie is dead or nearly dead.\n\n"
                f"  cookie expiry : {nz(exp)}\n"
                f"  race finished : {nz(race['race'])}\n\n"
                f"Refresh so the final scores get picked up.\n\n"
                f"{REFRESH_HOWTO}\n")

    # pre-weekend: nag once we're close, if the cookie won't outlive the race
    if not covers_race and now >= race["fp1"] - timedelta(hours=LEAD_HOURS) \
            and now <= race["race"]:
        return ("pre",
                f"[F1] Refresh cookie before {name} (FP1 {race['fp1']:%a %d %b})",
                f"{name} is coming up and the current cookie will not outlive "
                f"the race.\n\n"
                f"  FP1           : {nz(race['fp1'])}\n"
                f"  race          : {nz(race['race'])}\n"
                f"  cookie expires: {nz(exp)}\n\n"
                f"Refresh any time before FP1 and the weekend is covered. "
                f"(A cookie only lasts 96h, so if the round is slow to "
                f"finalise you may get one more nudge after the race.)\n\n"
                f"{REFRESH_HOWTO}\n")

    return (None, None, None)


def main():
    conn = db()

    if "--test" in sys.argv:
        mailed = send_mail("F1 cookie watch: test",
                           "Test from cookie_watch.py on mako-sentinel.")
        print("test email sent" if mailed else "test email FAILED")
        pushed = send_push("F1 cookie watch",
                           "Test push from mako-sentinel.",
                           priority="default", tags="white_check_mark")
        print("test push sent" if pushed else "test push FAILED")
        return 0 if (mailed and pushed) else 1

    now = datetime.now(timezone.utc)
    _, detail = refresh_calendar(conn)
    print(f"[watch] calendar: {detail}")

    race = current_or_next_race(conn, now)
    if race is None:
        print("[watch] no upcoming race in the calendar -- silent.")
        return 0

    exp = cookie_expiry()
    failed = last_run_failed()

    print(f"[watch] race     R{race['round']} {race['name']}")
    print(f"[watch]   FP1        {nz(race['fp1'])}")
    print(f"[watch]   race       {nz(race['race'])}")
    print(f"[watch]   finalised  {is_finalized(conn, race['round'])}")
    print(f"[watch]   cookie exp {nz(exp)}")
    print(f"[watch]   last run   {'FAILED' if failed else 'ok'}")

    category, subject, body = decide(conn, now, race, exp, failed)

    if category is None:
        print("[watch] decision: silent")
        return 0

    print(f"[watch] decision: {category.upper()} -- {subject}")
    if "--status" in sys.argv:
        print("[watch] --status set, not emailing.")
        return 0
    if throttled(conn, category, now):
        print(f"[watch] throttled (mailed '{category}' within {THROTTLE_HOURS}h)")
        return 0
    # Push and email both go out. Email is the backstop: if ntfy or the phone
    # is unavailable, the notification still lands somewhere.
    priority, tags = PUSH_PRIORITY.get(category, ("default", "racing_car"))
    push_title = subject.replace("[F1] ", "")
    push_msg = {
        "urgent": f"Fetch is FAILING during {race['name']}. Scores aren't being "
                  f"collected. Refresh the cookie.",
        "post": f"{race['name']} finished but scores aren't final and the "
                f"cookie is dying. Refresh to capture them.",
        "pre": f"Cookie expires {nz(exp)}, before {race['name']} finishes. "
               f"FP1 is {nz(race['fp1'])}. Refresh before then.",
    }.get(category, subject)

    mailed = send_mail(subject, body)
    pushed = send_push(push_title, push_msg, priority=priority, tags=tags)
    print(f"[watch] email={'ok' if mailed else 'FAILED'} "
          f"push={'ok' if pushed else 'FAILED'}")

    # Throttle on any successful delivery -- otherwise a broken push would
    # re-send the email every hour.
    if mailed or pushed:
        state_set(conn, f"last_mail_{category}", now.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
