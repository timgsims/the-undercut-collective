"""Looks for grid penalties for the next race with Claude (Tim, 8 Nov 2026).

Runs from run_pipeline.sh every 15 minutes and decides for itself whether to
search: in the week before teams lock, once a day (from 10am NZ), then every
3 hours in the last 48 hours. It stops at the lock: after qualifying the sim
uses OpenF1's official grid, which has every penalty in it.

Claude (headless Claude Code on Tim's Pro plan, ~/.f1-claude, token copied
from HomeBase's on 8 Nov 2026, so it expires with it: renew both
together) searches the web and answers in JSON. A run
costs about $0.07 at API prices, so ~10 a race week is a small slice of the
Pro limits. New or changed penalties go in the `penalties` table
(penalties.py) and leave secrets/force_full, so this run of the pipeline
reruns the sim and rebuilds the site; notify.py then tells everyone.

  python3 penalty_watch.py            # search if due
  python3 penalty_watch.py --force    # search now
  python3 penalty_watch.py --dry-run  # search now, print, change nothing
  python3 penalty_watch.py --add VER back [places] [round]
  python3 penalty_watch.py --remove VER [round]
  python3 penalty_watch.py --list [round]
"""
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import penalties as pen
from notify import fetch_locks

BASE = Path(__file__).resolve().parent
DB = BASE / "f1_data.db"
FORCE_FULL = BASE / "secrets" / "force_full"
LOG = BASE / "secrets" / "penalty_watch.log"
CLAUDE_DIR = Path.home() / ".f1-claude"
NZ = ZoneInfo("Pacific/Auckland")
WINDOW = dt.timedelta(days=7)
CLOSE = dt.timedelta(hours=48)
EVERY_CLOSE = dt.timedelta(hours=3)
RETRY = dt.timedelta(hours=1)
DAILY_FROM_HOUR = 10


def log(msg):
    line = f"{dt.datetime.now(NZ):%Y-%m-%d %H:%M} {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def next_round(now):
    """(round, lock time UTC) of the next round to lock, or (None, None)."""
    locks = fetch_locks()
    ahead = sorted((t, r) for r, t in locks.items() if t > now)
    return (ahead[0][1], ahead[0][0]) if ahead else (None, None)


def due(conn, season, rnd, lock, now):
    if lock - now > WINDOW:
        return False
    last = conn.execute("SELECT checked_at, ok FROM penalty_checks WHERE season=? AND round=? "
                        "ORDER BY checked_at DESC LIMIT 1", (season, rnd)).fetchone()
    if not last:
        return lock - now <= CLOSE or now.astimezone(NZ).hour >= DAILY_FROM_HOUR
    at = dt.datetime.fromisoformat(last[0])
    if not last[1]:
        return now - at >= RETRY
    if lock - now <= CLOSE:
        return now - at >= EVERY_CLOSE
    local = now.astimezone(NZ)
    return at.astimezone(NZ).date() < local.date() and local.hour >= DAILY_FROM_HOUR


def drivers(conn, season):
    """[(code, name, team)] for the drivers in the latest priced round."""
    r = conn.execute("SELECT MAX(round) FROM player_results WHERE season=? AND value IS NOT NULL",
                     (season,)).fetchone()[0]
    return conn.execute(
        "SELECT p.tla, p.name, COALESCE((SELECT c.name FROM players c WHERE c.type='constructor' "
        "AND c.team_id=p.team_id LIMIT 1), '') FROM players p JOIN player_results r "
        "ON r.player_id=p.id WHERE r.season=? AND r.round=? AND p.type='driver' AND p.tla IS NOT NULL "
        "ORDER BY p.tla", (season, r)).fetchall()


def prompt(race, season, race_day, field, known):
    lines = "\n".join(f"{c}: {n} ({t})" for c, n, t in field)
    have = "\n".join(f"- {p['code']}: {pen.describe(p)}, {p['status']}" for p in known) or "- none yet"
    return f"""Today is {dt.datetime.now(NZ):%A %d %B %Y} (New Zealand time). Search the web for grid
penalties for the {season} {race} Grand Prix (race day {race_day}): grid place drops,
back-of-grid starts and pit-lane starts, for example from power unit or gearbox
components over the season allowance, penalties carried over from the last race, or
stewards' decisions.

Drivers (use these codes):
{lines}

Already recorded:
{have}

Rules:
- "confirmed": announced by the FIA, the stewards or the team, or reported as fact by
  formula1.com, the FIA, Autosport, The Race, Motorsport.com, ESPN, PlanetF1 or similar.
- "expected": a team or reputable outlet says it will happen or is likely. Leave out
  pure speculation and opinion.
- One entry per driver, for this race only. Combine several drops into one total; if
  any part sends them to the back, use "back".
- source_url must be a URL your search actually returned. Never build or guess one.
- reason: one short plain sentence on the cause. Don't mention practice, qualifying
  or race results or positions.
- withdrawn: true only if a recorded penalty was cancelled or overturned.

Reply with JSON only, no other text:
{{"penalties":[{{"code":"VER","kind":"places|back|pitlane","places":10,"status":"confirmed|expected",
"reason":"","source_url":"","source_title":"","withdrawn":false}}],"note":"one line on what you checked"}}
"""


def ask_claude(text):
    token = (CLAUDE_DIR / "token").read_text().strip()
    home = CLAUDE_DIR / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "CLAUDE_CODE_OAUTH_TOKEN": token}
    cmd = [str(CLAUDE_DIR / "claude"), "-p", text, "--output-format", "json", "--model", "sonnet",
           "--tools", "WebSearch", "WebFetch", "--allowedTools", "WebSearch", "WebFetch",
           "--max-turns", "12", "--no-session-persistence", "--strict-mcp-config"]
    p = subprocess.run(cmd, cwd=home, env=env, capture_output=True, text=True, timeout=600)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f"claude gave no JSON (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {str(out.get('result'))[:300]}")
    m = re.search(r"\{.*\}", out.get("result") or "", re.S)
    if not m:
        raise RuntimeError(f"no JSON in Claude's answer: {(out.get('result') or '')[:300]}")
    return json.loads(m.group(0)), out.get("total_cost_usd") or 0.0


def alert(race, err, now):
    """Two failed searches in a row: tell Tim through HomeLab, once a day.
    Usually the Claude token (copied from HomeBase's: renew both together)."""
    mark = BASE / "secrets" / "penalty_watch_alerted"
    today = now.astimezone(NZ).date().isoformat()
    try:
        if mark.read_text().strip() == today:
            return
    except OSError:
        pass
    subprocess.run([str(Path.home() / "hl-notify.py"), "--source", "f1", "--title",
                    "F1 grid penalty search isn't working",
                    "--body", f"Two searches for {race} failed in a row: {str(err)[:200]}"], timeout=60)
    mark.write_text(today)


def link_ok(url):
    """False only for a link that's plainly dead (404/410, bad host): sites
    that refuse scripts (403 etc.) are kept."""
    if not re.match(r"https?://", url or ""):
        return False
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (undercut-collective)"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
        return True
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        return False


def clean(items, codes):
    out = []
    for i in items or []:
        code = str(i.get("code", "")).upper().strip()
        kind = i.get("kind")
        status = i.get("status")
        if code not in codes or kind not in ("places", "back", "pitlane") or status not in ("confirmed", "expected"):
            continue
        try:
            places = int(i["places"]) if kind == "places" and i.get("places") is not None else None
        except (TypeError, ValueError):
            places = None
        if kind == "places" and not places:
            continue
        url = str(i.get("source_url") or "").strip()
        out.append({"code": code, "kind": kind, "places": places, "status": status,
                    "reason": str(i.get("reason") or "").strip()[:240],
                    "url": url if link_ok(url) else "", "title": str(i.get("source_title") or "").strip()[:120],
                    "withdrawn": bool(i.get("withdrawn"))})
    return out


def merge(conn, season, rnd, found, now):
    """Store what's new or changed; returns a list of change descriptions."""
    known = {p["code"]: p for p in pen.for_round(conn, season, rnd)}
    stamp = now.isoformat()
    changes = []
    for f in found:
        old = known.get(f["code"])
        if f["withdrawn"]:
            if old:
                conn.execute("UPDATE penalties SET removed=1, updated_at=? WHERE id=?", (stamp, old["id"]))
                changes.append(f"{f['code']} withdrawn")
            continue
        if old and old["status"] == "confirmed" and f["status"] == "expected":
            continue   # never downgrade on a vaguer article
        if old and (old["kind"], old["places"], old["status"]) == (f["kind"], f["places"], f["status"]):
            if f["url"] and not old["url"]:
                conn.execute("UPDATE penalties SET source_url=?, source_title=? WHERE id=?",
                             (f["url"], f["title"], old["id"]))
            continue
        if old:
            conn.execute("UPDATE penalties SET kind=?, places=?, status=?, reason=?, source_url=?, source_title=?, "
                         "updated_at=? WHERE id=?", (f["kind"], f["places"], f["status"], f["reason"],
                                                     f["url"] or old["url"], f["title"] or old["title"], stamp, old["id"]))
        else:
            conn.execute("INSERT INTO penalties (season, round, code, kind, places, status, reason, source_url, "
                         "source_title, found_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (season, rnd, f["code"], f["kind"], f["places"], f["status"], f["reason"], f["url"],
                          f["title"], stamp, stamp))
        changes.append(f"{f['code']} {pen.describe(f)} ({f['status']})")
    return changes


def search(conn, season, rnd, lock, now, dry=False):
    race = conn.execute("SELECT name FROM races WHERE season=? AND round=?", (season, rnd)).fetchone()
    race = race[0] if race else f"Round {rnd}"
    day = conn.execute("SELECT race_utc FROM race_schedule WHERE season=? AND round=?", (season, rnd)).fetchone()
    race_day = (dt.datetime.fromisoformat(day[0]).astimezone(NZ).strftime("%d %B %Y, NZ time")
                if day and day[0] else "this weekend")
    field = drivers(conn, season)
    known = pen.for_round(conn, season, rnd)
    try:
        answer, cost = ask_claude(prompt(race, season, race_day, field, known))
    except Exception as e:
        log(f"R{rnd} {race}: search failed: {e}")
        if not dry:
            prev = conn.execute("SELECT ok FROM penalty_checks WHERE season=? AND round=? ORDER BY checked_at DESC "
                                "LIMIT 1", (season, rnd)).fetchone()
            conn.execute("INSERT INTO penalty_checks VALUES (?,?,?,0,?,0)", (season, rnd, now.isoformat(), str(e)[:300]))
            conn.commit()
            if prev and not prev[0]:
                alert(race, e, now)
        return 1
    found = clean(answer.get("penalties"), {c for c, _, _ in field})
    if dry:
        print(json.dumps({"found": found, "note": answer.get("note"), "cost": cost}, indent=1))
        return 0
    changes = merge(conn, season, rnd, found, now)
    conn.execute("INSERT INTO penalty_checks VALUES (?,?,?,1,?,?)",
                 (season, rnd, now.isoformat(), str(answer.get("note") or "")[:300], cost))
    conn.commit()
    log(f"R{rnd} {race}: {len(found)} found, ${cost:.3f}. " + ("; ".join(changes) if changes else "nothing new"))
    if changes:
        FORCE_FULL.parent.mkdir(exist_ok=True)
        FORCE_FULL.write_text(now.isoformat())
    return 0


def manual(conn, season, args, now):
    """--add / --remove / --list, for Tim's corrections (through Claude)."""
    stamp = now.isoformat()
    cmd = args[0]
    if cmd == "--list":
        rnd = int(args[1]) if len(args) > 1 else next_round(now)[0]
        for p in pen.for_round(conn, season, rnd):
            print(p)
        return 0
    if cmd == "--add":
        code, kind = args[1].upper(), args[2]
        places = int(args[3]) if kind == "places" else None
        rest = args[4:] if kind == "places" else args[3:]
        rnd = int(rest[0]) if rest else next_round(now)[0]
        conn.execute("UPDATE penalties SET removed=1, updated_at=? WHERE season=? AND round=? AND code=? AND removed=0",
                     (stamp, season, rnd, code))
        conn.execute("INSERT INTO penalties (season, round, code, kind, places, status, reason, source_url, "
                     "source_title, found_at, updated_at) VALUES (?,?,?,?,?,'confirmed','Added by hand','','',?,?)",
                     (season, rnd, code, kind, places, stamp, stamp))
    elif cmd == "--remove":
        code = args[1].upper()
        rnd = int(args[2]) if len(args) > 2 else next_round(now)[0]
        conn.execute("UPDATE penalties SET removed=1, updated_at=? WHERE season=? AND round=? AND code=? AND removed=0",
                     (stamp, season, rnd, code))
    else:
        print(__doc__)
        return 2
    conn.commit()
    FORCE_FULL.parent.mkdir(exist_ok=True)
    FORCE_FULL.write_text(stamp)
    print(f"done: {' '.join(args)} (round {rnd}); the next pipeline run reruns the sim and rebuilds")
    return 0


def main():
    args = sys.argv[1:]
    now = dt.datetime.now(dt.timezone.utc)
    conn = sqlite3.connect(DB, timeout=60)
    pen.ensure(conn)
    season = conn.execute("SELECT MAX(season) FROM races").fetchone()[0]
    if args and args[0] in ("--add", "--remove", "--list"):
        return manual(conn, season, args, now)
    try:
        rnd, lock = next_round(now)
    except Exception as e:
        print(f"penalty_watch: no F1 Fantasy schedule ({e})")
        return 0
    if rnd is None:
        return 0
    if "--force" in args or "--dry-run" in args or due(conn, season, rnd, lock, now):
        return search(conn, season, rnd, lock, now, dry="--dry-run" in args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
