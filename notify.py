"""Push notifications for the league (1 Nov 2026). Runs from run_pipeline.sh
every 15 minutes, including between race weekends, decides what's due and
posts it to the undercut-push service on Sentinel (push-service/), which
sends it to every phone that turned notifications on in the dashboard.

What it sends (each person picks which, and which manager they are):
  lock      2 hours before teams lock (qualifying, or sprint qualifying on a
            sprint weekend). If that's in the night, 7:30pm the evening before.
  sim       a new simulation on the site for the next round to lock: very
            early once the last round is final, then post FP1, FP2 and FP3.
            Replaced the fp1 message (7 Nov 2026), which also only reached
            devices with no manager picked.
  quali     when qualifying points come in: your points and place this weekend.
  final     when a round's points are final: score, weekend place, overall place.
  prices    when F1 publishes the next round's prices: your team's value.
  inactive  a driver in your team isn't racing next round (he'd cost points).
  chips     with 4, 2 and 1 races left: the chips you haven't used.

Nothing goes out between 10pm and 7:30am NZ time: anything due then waits
until 7:30am, unless it's stale by then. Each message is sent once
(notify_log). On a new database, run `python3 notify.py --mark-current` first
so old events aren't sent; `--dry-run` prints what would go out.
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DB = BASE / "f1_data.db"
TOOLS_BUILT = BASE / "secrets" / "tools_built.json"   # what the site shows (gate_check.py)
TOKEN_FILE = BASE / "secrets" / "push_token"
PUSH_URL = "http://127.0.0.1:3092/internal/send"
RACEDAY_FEED = "https://fantasy.formula1.com/feeds/schedule/raceday_en.json"
NZ = ZoneInfo("Pacific/Auckland")
QUIET_FROM, QUIET_TO = dt.time(22, 0), dt.time(7, 30)
CHIPS = ["Wildcard", "Limitless", "Extra DRS", "No Negative", "Auto Pilot", "Final Fix"]
SITE = "https://f1fantasy.simsey.co.nz/"
SIM_STAGE = {"pre": "very early", "fp1": "post FP1", "fp2": "post FP2", "fp3": "post FP3"}


def quiet(t):
    """Is this (UTC) moment in NZ quiet hours?"""
    lt = t.astimezone(NZ).time()
    return lt >= QUIET_FROM or lt < QUIET_TO


def next_morning(t):
    """7:30am NZ on or after t."""
    lt = t.astimezone(NZ)
    m = lt.replace(hour=7, minute=30, second=0, microsecond=0)
    if m < lt:
        m += dt.timedelta(days=1)
    return m.astimezone(dt.timezone.utc)


def when_nz(t, now):
    """'3:00pm', 'tomorrow 3:00am' or 'Sun 3:00am', in NZ time."""
    lt, ln = t.astimezone(NZ), now.astimezone(NZ)
    clock = lt.strftime("%-I:%M%p").lower()
    days = (lt.date() - ln.date()).days
    if days == 0:
        return clock
    if days == 1:
        return f"tomorrow {clock}"
    return f"{lt.strftime('%a')} {clock}"


def lock_reminder_time(lock):
    """2 hours before the lock, or 7:30pm the evening before a lock in the night."""
    t = lock - dt.timedelta(hours=2)
    if not quiet(t):
        return t
    ll = lock.astimezone(NZ)
    day = ll.date() - dt.timedelta(days=1) if ll.time() < dt.time(12, 0) else ll.date()
    return dt.datetime.combine(day, dt.time(19, 30), NZ).astimezone(dt.timezone.utc)


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def fetch_locks():
    """{round: lock time (UTC)} from F1 Fantasy's schedule: a round's first
    scored session (qualifying, or sprint qualifying) starts the lock."""
    req = urllib.request.Request(RACEDAY_FEED, headers={"User-Agent": "undercut-collective"})
    with urllib.request.urlopen(req, timeout=30) as r:
        sessions = json.load(r)["Data"]["Value"]
    locks = {}
    for s in sessions:
        try:
            rnd, start = int(s["GamedayId"]), parse_iso(s["SessionStartDateISO8601"])
        except (KeyError, ValueError, TypeError):
            continue
        if rnd not in locks or start < locks[rnd]:
            locks[rnd] = start
    return locks


class League:
    """What the messages need from f1_data.db."""

    def __init__(self, conn, season):
        self.c, self.season = conn, season
        self.managers = dict(conn.execute("SELECT id, name FROM managers"))
        self.races = dict(conn.execute("SELECT round, name FROM races WHERE season=?", (season,)))
        self.fp1 = {r: parse_iso(t) for r, t in conn.execute(
            "SELECT round, fp1_utc FROM race_schedule WHERE season=?", (season,)) if t}
        self.rounds = sorted(self.races)

    def name(self, rnd):
        return self.races.get(rnd, f"Round {rnd}")

    def results(self, rnd):
        return {mid: (pts, total, final) for mid, pts, total, final in self.c.execute(
            "SELECT manager_id, points, season_total, is_final FROM race_results WHERE season=? AND round=?",
            (self.season, rnd))}

    def picks(self, rnd, mid):
        return [p for (p,) in self.c.execute("SELECT player_id FROM team_picks WHERE season=? AND round=? "
                                             "AND manager_id=?", (self.season, rnd, mid))]

    def latest_round_with_picks(self):
        r = self.c.execute("SELECT MAX(round) FROM team_picks WHERE season=?", (self.season,)).fetchone()[0]
        return r or 0

    def prices(self, rnd):
        return dict(self.c.execute("SELECT player_id, value FROM player_results WHERE season=? AND round=? "
                                   "AND value IS NOT NULL", (self.season, rnd)))

    def chips_used(self, mid):
        return {c for (c,) in self.c.execute("SELECT chip_name FROM chips_used WHERE season=? AND manager_id=?",
                                             (self.season, mid))}


def published_stage(rnd):
    """The simulation stage the site shows for a round, or None: a message
    must not beat the rebuild that puts it on the site."""
    try:
        return dict(json.loads(TOOLS_BUILT.read_text())["sims"]).get(rnd)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def published_penalties(rnd):
    """[[id, status, kind, places]] the site shows for a round."""
    try:
        return json.loads(TOOLS_BUILT.read_text()).get("pen", {}).get(str(rnd), [])
    except (OSError, ValueError, AttributeError):
        return []


def place(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def ranks(values):
    """{key: 1-based rank, highest first} (ties share a rank)."""
    order = sorted(values.values(), reverse=True)
    return {k: order.index(v) + 1 for k, v in values.items()}


def due_messages(lg, locks, now):
    """[(key, expires (UTC), send_from (UTC), [messages])] for everything that
    could go out now or later; the caller sends what's due and not yet sent."""
    out = []
    mids = list(lg.managers)
    cur = lg.latest_round_with_picks()
    # The next round to lock: the first whose lock is still ahead.
    upcoming = sorted(r for r, t in locks.items() if t > now)
    nxt = upcoming[0] if upcoming else None

    if nxt:
        lock = locks[nxt]
        race = lg.name(nxt)
        # Lock reminder
        msgs = [{"type": "lock", "manager": None, "title": f"{race}: teams lock {when_nz(lock, now)}",
                 "body": "Last chance to make your changes. The Team Calculator has the latest predictions.",
                 "url": SITE + "#tools", "tag": f"lock-{nxt}"}]
        bank = dict(lg.c.execute("SELECT manager_id, team_balance FROM race_results WHERE season=? AND round=?",
                                 (lg.season, cur)))
        for mid in mids:
            extra = f" You have ${bank[mid]:.1f}M in the bank." if bank.get(mid) is not None else ""
            msgs.append({"type": "lock", "manager": lg.managers[mid], "tag": f"lock-{nxt}", "url": SITE + "#tools",
                         "title": f"{race}: teams lock {when_nz(lock, now)}",
                         "body": f"{lg.managers[mid]}, last chance to set your team.{extra} "
                                 "The Team Calculator has the latest predictions."})
        out.append((f"lock:{lg.season}:{nxt}", lock, lock_reminder_time(lock), msgs))

        # A new simulation on the site. Only the latest stage: ones that came
        # in overnight are skipped for the newest. "Very early" for the next
        # round waits for this round to be final, not its qualifying.
        stage = published_stage(nxt)
        final = lg.c.execute("SELECT 1 FROM round_finalized_at WHERE season=? AND round=?",
                             (lg.season, cur)).fetchone()
        if stage in SIM_STAGE and (stage != "pre" or final):
            body = ("Based on past races, before any practice." if stage == "pre"
                    else "Now using this weekend's practice pace.")
            msg = {"type": "sim", "tag": f"sim-{nxt}", "url": SITE + "#tools",
                   "title": f"{race} simulation: {SIM_STAGE[stage]}",
                   "body": f"{body} See the Team Calculator before teams lock {when_nz(lock, now)}."}
            out.append((f"sim:{lg.season}:{nxt}:{stage}", lock, now - dt.timedelta(days=30),
                        [{**msg, "manager": m} for m in [None] + [lg.managers[x] for x in mids]]))

        # Grid penalties (penalty_watch.py), once the site shows them
        import penalties
        names = dict(lg.c.execute("SELECT tla, name FROM players WHERE type='driver'"))
        shown = [tuple(x) for x in published_penalties(nxt)]
        for p in penalties.for_round(lg.c, lg.season, nxt):
            if (p["id"], p["status"], p["kind"], p["places"]) not in shown:
                continue
            who = names.get(p["code"], p["code"]).split(". ", 1)[-1]   # "M. Verstappen" -> "Verstappen"
            will = "to" if p["status"] == "confirmed" else "expected to"
            what = {"back": "start from the back", "pitlane": "start from the pit lane"}.get(
                p["kind"], f"get a {penalties.describe(p).lower()}")
            why = p["reason"].strip()
            if why and why[-1] not in ".!?":
                why += "."
            tail = ("The simulation includes it." if p["status"] == "confirmed"
                    else "Not in the simulation until it's confirmed.")
            msg = {"type": "penalty", "tag": f"penalty-{nxt}-{p['code']}", "url": SITE + "#tools",
                   "title": f"{race}: {who} {will} {what}", "body": f"{why} {tail}".strip()}
            out.append((f"penalty:{lg.season}:{nxt}:{p['id']}:{p['status']}:{p['kind']}:{p['places']}", lock,
                        now - dt.timedelta(days=30),
                        [{**msg, "manager": m} for m in [None] + [lg.managers[x] for x in mids]]))

        # A driver who isn't racing, in someone's team
        inactive = {p for (p,) in lg.c.execute("SELECT id FROM players WHERE active=0")}
        tla = dict(lg.c.execute("SELECT id, COALESCE(tla, name) FROM players"))
        if cur and nxt > cur:
            msgs = []
            for mid in mids:
                gone = [tla.get(p, p) for p in lg.picks(cur, mid) if p in inactive]
                if gone:
                    who = " and ".join(gone)
                    msgs.append({"type": "inactive", "manager": lg.managers[mid], "tag": f"inactive-{nxt}",
                                 "url": SITE + "#tools", "title": f"{who} isn't racing in {race}",
                                 "body": f"{lg.managers[mid]}, swap {'them' if len(gone) > 1 else 'him'} out before "
                                         f"teams lock {when_nz(lock, now)}, or it'll cost you points."})
            if msgs:
                out.append((f"inactive:{lg.season}:{nxt}", lock, lock - dt.timedelta(days=3), msgs))

        # New prices for the next round
        if cur and nxt > cur:
            new, old = lg.prices(nxt), lg.prices(cur)
            if new:
                msgs = [{"type": "prices", "manager": None, "tag": f"prices-{nxt}", "url": SITE + "#tools",
                         "title": f"Prices for {race} are out",
                         "body": "See what's likely to rise next in the Budget Builder."}]
                for mid in mids:
                    p = lg.picks(cur, mid)
                    if p and all(x in new and x in old for x in p):
                        v, d = sum(new[x] for x in p), sum(new[x] - old[x] for x in p)
                        msgs.append({"type": "prices", "manager": lg.managers[mid], "tag": f"prices-{nxt}",
                                     "url": SITE + "#tools", "title": f"Prices for {race} are out",
                                     "body": f"{lg.managers[mid]}, your team is now worth ${v:.1f}M "
                                             f"({'+' if d >= 0 else '-'}${abs(d):.1f}M). "
                                             "See what's likely to rise next in the Budget Builder."})
                out.append((f"prices:{lg.season}:{nxt}", lock, now - dt.timedelta(days=30), msgs))

    # This round's results: qualifying points, then final points
    if cur:
        race = lg.name(cur)
        res = lg.results(cur)
        quali_done = lg.c.execute("SELECT 1 FROM session_status WHERE season=? AND round=? AND session_type="
                                  "'Qualifying' AND is_done=1", (lg.season, cur)).fetchone()
        race_done = lg.c.execute("SELECT 1 FROM session_status WHERE season=? AND round=? AND session_type="
                                 "'Race' AND is_done=1", (lg.season, cur)).fetchone()
        nxt_lock = locks.get(cur + 1, now + dt.timedelta(days=7))
        if quali_done and res and not race_done:
            wk = ranks({m: r[0] or 0 for m, r in res.items()})
            msgs = [{"type": "quali", "manager": None, "tag": f"quali-{cur}", "url": SITE,
                     "title": f"{race}: qualifying points are in", "body": "See how the league stands this weekend."}]
            for mid, (pts, _, _) in res.items():
                if mid in lg.managers and pts is not None:
                    msgs.append({"type": "quali", "manager": lg.managers[mid], "tag": f"quali-{cur}", "url": SITE,
                                 "title": f"{race}: qualifying points are in",
                                 "body": f"{lg.managers[mid]}, you're on {pts:.0f} pts, "
                                         f"{place(wk[mid])} in the league this weekend."})
            out.append((f"quali:{lg.season}:{cur}", nxt_lock, now - dt.timedelta(days=30), msgs))

        final = lg.c.execute("SELECT 1 FROM round_finalized_at WHERE season=? AND round=?",
                             (lg.season, cur)).fetchone()
        if final and res:
            wk = ranks({m: r[0] or 0 for m, r in res.items()})
            ov = ranks({m: r[1] or 0 for m, r in res.items()})
            prev = lg.results(cur - 1) if cur > 1 else {}
            ov_prev = ranks({m: r[1] or 0 for m, r in prev.items()}) if prev else {}
            msgs = [{"type": "final", "manager": None, "tag": f"final-{cur}", "url": SITE,
                     "title": f"{race} points are final", "body": "The leaderboard is up to date."}]
            for mid, (pts, total, _) in res.items():
                if mid not in lg.managers or pts is None:
                    continue
                move = ""
                if mid in ov_prev and ov_prev[mid] != ov[mid]:
                    d = ov_prev[mid] - ov[mid]
                    move = f", {'up' if d > 0 else 'down'} {abs(d)}"
                msgs.append({"type": "final", "manager": lg.managers[mid], "tag": f"final-{cur}", "url": SITE,
                             "title": f"{race} points are final",
                             "body": f"{lg.managers[mid]}: {pts:.0f} pts, {place(wk[mid])} this weekend. "
                                     f"You're {place(ov[mid])} overall{move}."})
            out.append((f"final:{lg.season}:{cur}", nxt_lock, now - dt.timedelta(days=30), msgs))

            # Chips left, with 4, 2 and 1 races to go
            left = len([r for r in lg.rounds if r > cur])
            if left in (4, 2, 1):
                msgs = []
                for mid in mids:
                    unused = [ch for ch in CHIPS if ch not in lg.chips_used(mid)]
                    if unused:
                        msgs.append({"type": "chips", "manager": lg.managers[mid], "tag": f"chips-{cur}",
                                     "url": SITE, "title": f"{left} race{'s' if left > 1 else ''} left",
                                     "body": f"{lg.managers[mid]}, you still have "
                                             f"{', '.join(unused[:-1]) + ' and ' + unused[-1] if len(unused) > 1 else unused[0]}."})
                if msgs:
                    out.append((f"chips:{lg.season}:{cur}", nxt_lock, now - dt.timedelta(days=30), msgs))
    return out


def main():
    dry = "--dry-run" in sys.argv
    mark_only = "--mark-current" in sys.argv
    now = dt.datetime.now(dt.timezone.utc)
    if os.environ.get("NOTIFY_NOW"):   # testing: pretend it's this time (with --dry-run)
        now = parse_iso(os.environ["NOTIFY_NOW"])
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("CREATE TABLE IF NOT EXISTS notify_log (key TEXT PRIMARY KEY, sent_at TEXT, messages INTEGER)")
    season = conn.execute("SELECT MAX(season) FROM races").fetchone()[0]
    try:
        locks = fetch_locks()
    except Exception as e:   # no schedule: results messages can still go
        print(f"notify: couldn't read the F1 Fantasy schedule ({e})")
        locks = {}
    lg = League(conn, season)
    sent = {k for (k,) in conn.execute("SELECT key FROM notify_log")}
    batch, keys = [], []
    for key, expires, send_from, msgs in due_messages(lg, locks, now):
        if key in sent or now >= expires or now < send_from:
            continue
        if quiet(now) and not mark_only:
            if next_morning(now) >= expires:
                print(f"notify: {key} would only go out after it's stale; dropping")
                conn.execute("INSERT OR REPLACE INTO notify_log VALUES (?, ?, 0)", (key, now.isoformat()))
            continue   # wait for 7:30am
        keys.append((key, len(msgs)))
        batch.extend(msgs)
    if not keys:
        conn.commit()
        return
    if dry:
        for m in batch:
            print(f"[{m['type']}] {m['manager'] or '(anyone)'}: {m['title']} — {m['body']}")
        return
    if not mark_only:
        token = TOKEN_FILE.read_text().strip()
        req = urllib.request.Request(PUSH_URL, data=json.dumps({"messages": batch}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.load(r)
        print(f"notify: sent {', '.join(k for k, _ in keys)} -> {result}")
    for key, n in keys:
        conn.execute("INSERT OR REPLACE INTO notify_log VALUES (?, ?, ?)", (key, now.isoformat(), 0 if mark_only else n))
    conn.commit()
    if mark_only:
        print(f"notify: marked as already sent: {', '.join(k for k, _ in keys)}")


if __name__ == "__main__":
    main()
