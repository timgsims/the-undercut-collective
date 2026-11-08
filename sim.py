"""Race simulation for the Tools tab (TOOLS_PLAN.md step B), like F1FT's rhter sim.

For one round it works out each driver's pace, runs the weekend SIMS times
with random variation, scores every run with the F1 Fantasy rules, and keeps
each driver's and constructor's points distribution. The Team calculator uses
the averages (xPts) and the Budget builder the distributions (price odds).

Pace, in % off the fastest car, from what was known before the session the
prediction is made after:
  * qualifying: every 2026 qualifying so far, each driver's best time put on
    the Q3 scale (Q1/Q2 times corrected by that day's track evolution). Car
    pace (the team's average) counts recent rounds more than driver form;
  * practice: this weekend's best laps, blended in as sessions happen;
  * once qualifying has happened, the real grid is used.
Race: each run, a car's race pace plus noise, plus a cost per grid slot set so
the shuffle matches how hard the track is to pass on (2022-2026 history at
that circuit). Retirements by team rate, fastest lap and Driver of the Day by
how finishers have historically picked them up, overtakes from each driver's
2026 average scaled by the track, constructor pit stop points from the
team's recent pit stop points. Sprints the same way with sprint scoring.

Run: python3 sim.py [round]   (default: the round the Budget builder shows)
"""
import bisect
import collections
import json
import math
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent / "f1_data.db"
OVERRIDES = Path(__file__).resolve().parent / "sim_overrides.json"
SIMS = 10000

# Tuned by sim_backtest.py on 2026 rounds 5-18 (see TOOLS_PLAN.md).
P = {
    "car_decay": 0.5,      # weight of each older round in car pace
    "drv_decay": 0.9,      # weight of each older round in driver form
    "drv_prior": 1.5,       # rounds of "same as team-mate" a driver's form starts from
    "gap_cap": 4.0,        # % off pole a bad day counts as at most
    "sd_quali": 0.45,      # % noise per driver in qualifying
    "slot": 0.06,          # % of lap time a grid slot is worth in the race
    "sprint_sd": 0.7,      # sprint race noise, as a share of the race's
    "fp_weight": 0.25,     # weight of each practice session's pace (max 3)
    "q_weight": 0.3,       # weight of this weekend's qualifying in race pace
    "lr_weight": 0.0,      # weight of FP2 long runs vs one-lap pace: off, as 0.2-0.7 all
                           # made the 2026 backtest worse (12.74 -> 12.78-13.47)
    "dnf_prior": 20,       # starts of league-average reliability a team starts from
    "ot_prior": 4,         # races of field-average overtakes a driver starts from
    "ot_gain": 0.25,       # extra overtakes per place gained
    # Track history (2026-11-02): both off. On 2026 R5-18 every setting made the
    # pre-weekend error worse (12.81 -> 12.86-13.00) or no clearer than noise
    # (similar tracks 1.0: 12.79, but worse after FP3). 2026 is a new rule set,
    # so 2022-25 says little about these cars; retest in 2027 against 2026.
    "aff_weight": 0.0,     # share of a team's past over/under-performance at this circuit added to car pace
    "aff_age": 0.5,        # weight of each older season in that
    "aff_prior": 1.0,      # seasons of "no track effect" it starts from
    "sim_weight": 0.0,     # extra weight on recent rounds at circuits like this one (0 = off)
    # Tim's call, 7 Nov 2026: be right about real results, not match F1FT. Fitted on squared
    # error over R3-19 at all stages (see TOOLS_PLAN.md). Average miss alone favours a typical
    # weekend over expected points. dnf_mult is below the true rate on purpose: the real rate
    # needs about 0.8, but team records are noisy and 0.65 with dnf_prior 20 scored better.
    "fp_driver": 0.25,     # share of practice pace taken from the driver, the rest from the team average
    "pace_mult": 1.0,       # stretches the gaps between cars
    "sd_mult": 0.6,        # race noise against what the track history calibrates to
    "dnf_mult": 0.65,      # retirement rates
    "ot_mult": 0.6,        # overtakes
    "pit_add": 0.0,        # extra constructor points a race
}
# Old team names in hist_quali -> today's team ids.
TEAM_ALIAS = {"alfa": "2640", "alphatauri": "2636"}

# F1 Fantasy 2026 scoring.
RACE_PTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
SPRINT_PTS = [8, 7, 6, 5, 4, 3, 2, 1]
QUALI_PTS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]


def pos_pts(table, pos):
    return table[pos - 1] if pos <= len(table) else 0


# ── Scoring ────────────────────────────────────────────────────────────────
def score_driver(q_pos, grid, finish, classified, overtakes, fl, dotd, sprint=None):
    """Points for one driver's weekend. q_pos 0 = no qualifying time (-5).
    sprint: same keys for the sprint, or None."""
    pts = pos_pts(QUALI_PTS, q_pos) if q_pos else (-5 if q_pos == 0 else 0)
    if classified:
        pts += pos_pts(RACE_PTS, finish) + (grid - finish)
    else:
        pts -= 20
    pts += overtakes + (10 if fl else 0) + (10 if dotd else 0)
    if sprint:
        if sprint["classified"]:
            # Places lost in a sprint count down to -10 at most.
            pts += pos_pts(SPRINT_PTS, sprint["finish"]) + max(-10, sprint["grid"] - sprint["finish"])
        else:
            pts -= 10
        pts += sprint["overtakes"] + (5 if sprint["fl"] else 0)
    return pts


def quali_bonus(positions):
    """Constructor bonus from both drivers' qualifying positions (22 cars: the
    top 16 reach Q2 and the top 10 Q3)."""
    q3 = sum(1 for p in positions if p and p <= 10)
    q2 = sum(1 for p in positions if p and p <= 16)
    if q3 == 2:
        return 10
    if q3 == 1:
        return 5
    return {2: 3, 1: 1, 0: -1}[q2]


# ── History ────────────────────────────────────────────────────────────────
def quali_gaps(conn, season, before_round):
    """{round: {code: gap %}} for every qualifying before `before_round`."""
    out = {}
    rows = conn.execute("SELECT round, code, team, q1, q2, q3 FROM hist_quali "
                        "WHERE season=? AND round<? ORDER BY round", (season, before_round)).fetchall()
    by = collections.defaultdict(list)
    for r in rows:
        by[r[0]].append(r[1:])
    for rnd, rs in by.items():
        evo12 = [q2 / q1 for _, _, q1, q2, _ in rs if q1 and q2]
        evo23 = [q3 / q2 for _, _, _, q2, q3 in rs if q2 and q3]
        e12 = statistics.median(evo12) if evo12 else 0.99
        e23 = statistics.median(evo23) if evo23 else 0.995
        t = {}
        for code, team, q1, q2, q3 in rs:
            v = q3 or (q2 * e23 if q2 else None) or (q1 * e12 * e23 if q1 else None)
            if v:
                t[(code, team)] = v
        if not t:
            continue
        best = min(t.values())
        out[rnd] = {k: min(P["gap_cap"], (v / best - 1) * 100) for k, v in t.items()}
    return out


def team_round_gaps(conn, season, before_round=99):
    """{round: {team: mean gap %}} for one season."""
    out = {}
    for r, g in quali_gaps(conn, season, before_round).items():
        by = collections.defaultdict(list)
        for (c, t), v in g.items():
            by[TEAM_ALIAS.get(t, t)].append(v)
        m = {t: sum(v) / len(v) for t, v in by.items()}
        mid = statistics.median(m.values())   # against the field, as low-drag tracks squeeze all gaps
        out[r] = {t: v - mid for t, v in m.items()}
    return out


def track_deviation(conn, season):
    """{(circuit, team): gap % at that circuit minus the team's form in the
    rounds around it (2 either side)}, for one past season."""
    trg = team_round_gaps(conn, season)
    circ = dict(conn.execute("SELECT round, circuit FROM hist_rounds WHERE season=?", (season,)))
    out = {}
    for r, g in trg.items():
        for t, v in g.items():
            near = [trg[x][t] for x in range(r - 2, r + 3) if x != r and x in trg and t in trg[x]]
            if len(near) >= 2 and circ.get(r):
                out[(circ[r], t)] = v - sum(near) / len(near)
    return out


_DEV = {}


def track_affinity(conn, season, circuit, teams):
    """{team: how much faster (-) or slower (+) than its usual form this team
    has qualified at `circuit` in past seasons, shrunk toward 0}."""
    out = {}
    for t in teams:
        num, den = 0.0, P["aff_prior"]
        for y in range(season - 1, season - 5, -1):
            if y not in _DEV:
                _DEV[y] = track_deviation(conn, y)
            d = _DEV[y].get((circuit, TEAM_ALIAS.get(t, t)))
            if d is not None:
                w = P["aff_age"] ** (season - 1 - y)
                num += w * d
                den += w
        out[t] = num / den
    return out


_SIM = {}


def circuit_similarity(conn, season):
    """{(a, b): correlation of teams' track deviations at circuits a and b},
    from the seasons before `season`."""
    if season in _SIM:
        return _SIM[season]
    devs = collections.defaultdict(dict)   # circuit -> {(year, team): dev}
    for y in range(season - 4, season):
        if y not in _DEV:
            _DEV[y] = track_deviation(conn, y)
        for (c, t), d in _DEV[y].items():
            devs[c][(y, t)] = d
    out = {}
    cs = list(devs)
    for a in cs:
        for b in cs:
            k = [x for x in devs[a] if x in devs[b]]
            if a == b or len(k) < 12:
                continue
            xa, xb = [devs[a][x] for x in k], [devs[b][x] for x in k]
            try:
                out[(a, b)] = statistics.correlation(xa, xb)
            except statistics.StatisticsError:
                pass
    _SIM[season] = out
    return out


def pace_ratings(conn, season, rnd, drivers):
    """% off the fastest car for each (code, team) in `drivers`."""
    gaps = quali_gaps(conn, season, rnd)
    rounds = sorted(gaps, reverse=True)
    car, form = {}, {}
    teams = {t for _, t in drivers}
    circ = dict(conn.execute("SELECT round, circuit FROM hist_rounds WHERE season=?", (season,)))
    sim_c = circuit_similarity(conn, season) if P["sim_weight"] else {}
    for team in teams:
        num = den = 0.0
        for i, r in enumerate(rounds):
            g = [v for (c, t), v in gaps[r].items() if t == team]
            if g:
                w = P["car_decay"] ** i
                if sim_c:
                    w *= 1 + P["sim_weight"] * max(0.0, sim_c.get((circ.get(r), circ.get(rnd)), 0.0))
                num += w * sum(g) / len(g)
                den += w
        car[team] = num / den if den else 2.0
    if P["aff_weight"] and circ.get(rnd):
        aff = track_affinity(conn, season, circ[rnd], teams)
        for team in teams:
            car[team] += P["aff_weight"] * aff[team]
    for code, team in drivers:
        num, den = 0.0, P["drv_prior"]
        for i, r in enumerate(rounds):
            g = gaps[r]
            if (code, team) in g:
                mates = [v for (c, t), v in g.items() if t == team and c != code]
                if mates:
                    w = P["drv_decay"] ** i
                    num += w * (g[(code, team)] - sum(mates) / len(mates)) / 2
                    den += w
        form[(code, team)] = num / den
    return {d: car[d[1]] + form[d] for d in drivers}


def practice_pace(conn, season, rnd, sessions):
    """Team pace (% off fastest) from this weekend's best laps, per session."""
    out = []
    for s in sessions:
        rows = conn.execute("SELECT code, best FROM practice_laps WHERE season=? AND round=? "
                            "AND session=? AND best IS NOT NULL", (season, rnd, s)).fetchall()
        if len(rows) < 15:
            continue
        best = min(b for _, b in rows)
        out.append({c: min(P["gap_cap"], (b / best - 1) * 100) for c, b in rows})
    return out


def long_run_adjust(conn, season, rnd, sessions, keys):
    """Per team, % by which its practice long-run pace (gap to the best team)
    is worse than its best-lap pace, averaged over the sessions given. Fuel
    loads and tyres differ between teams' runs, so this is noisy; the weight
    on it (P["lr_weight"]) is set by the backtest."""
    teams = {t for _, t in keys}
    code_team = {c: t for c, t in keys}
    out = collections.defaultdict(list)
    for s in sessions:
        rows = conn.execute("SELECT code, best, long_run FROM practice_laps WHERE season=? AND round=? "
                            "AND session=?", (season, rnd, s)).fetchall()
        lr, bl = collections.defaultdict(list), collections.defaultdict(list)
        for code, best, lrun in rows:
            t = code_team.get(code)
            if t and best:
                bl[t].append(best)
                if lrun:
                    lr[t].append(lrun)
        both = [t for t in teams if lr.get(t) and bl.get(t)]
        if len(both) < 9:
            continue
        lr_t = {t: sum(lr[t]) / len(lr[t]) for t in both}
        bl_t = {t: min(bl[t]) for t in both}
        lr0, bl0 = min(lr_t.values()), min(bl_t.values())
        d = {t: (lr_t[t] / lr0 - 1) * 100 - (bl_t[t] / bl0 - 1) * 100 for t in both}
        mid = statistics.median(d.values())   # relative to the field, not the best team
        for t in both:
            out[t].append(max(-1.0, min(1.0, d[t] - mid)))
    return {t: sum(v) / len(v) for t, v in out.items()}


def track_shuffle(conn, circuit, kind="race"):
    """How much finishing order moves from the grid at this circuit, against
    the average circuit: mean |finish - grid| of classified cars, 2022 on."""
    def mean_abs(where, args):
        r = conn.execute("SELECT AVG(ABS(pos - CASE WHEN grid=0 THEN 22 ELSE grid END)), COUNT(*) "
                         f"FROM hist_results WHERE kind=? AND classified=1 {where}", (kind,) + args).fetchone()
        return r
    allm, _ = mean_abs("", ())
    trk, n = mean_abs("AND circuit=?", (circuit,))
    if not trk or n < 30:
        return 1.0, allm
    w = n / (n + 60)          # shrink thin histories toward average
    return (w * trk + (1 - w) * allm) / allm, allm


def fl_weights(conn):
    """P(fastest lap | finishing position), 2022 on."""
    c = collections.Counter(r[0] for r in conn.execute(
        "SELECT pos FROM hist_results WHERE kind='race' AND fl_rank=1 AND classified=1"))
    return [c.get(p, 0) + 0.5 for p in range(1, 23)]


def dnf_rates(conn, season, rnd):
    rows = conn.execute("SELECT team, classified FROM hist_results WHERE season=? AND round<? "
                        "AND kind='race'", (season, rnd)).fetchall()
    n = len(rows) or 1
    avg = sum(1 for _, c in rows if not c) / n
    by = collections.defaultdict(lambda: [0, 0])
    for t, c in rows:
        by[t][0] += 1 - c
        by[t][1] += 1
    k = P["dnf_prior"]
    return {t: (a + k * avg) / (s + k) for t, (a, s) in by.items()}, avg


def event_history(conn, season, rnd):
    """From F1 Fantasy's per-round breakdown: overtakes per driver per race and
    sprint, and pit stop points per constructor."""
    ot, sot, pit = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    types = dict(conn.execute("SELECT id, type FROM players"))
    rows = conn.execute("SELECT round, player_id, event, frequency, points FROM player_event_points "
                        "WHERE season=? AND round<? ORDER BY round", (season, rnd)).fetchall()
    by = collections.defaultdict(dict)
    for r, pid, ev, f, pts in rows:
        by[(r, pid)][ev] = (f, pts)
    for (r, pid), ev in sorted(by.items()):
        raced = "Race Position" in ev or "Race not classified" in ev
        if types.get(pid) == "constructor":
            if raced:
                pit[pid].append((r, sum(v[1] for k, v in ev.items() if "pitstop" in k.lower())))
            continue
        if raced:
            ot[pid].append((r, ev.get("race overtake bonus", (0, 0))[1]))
        if "Sprint Position" in ev or "Sprint Not Classified" in ev:
            sot[pid].append((r, ev.get("Sprint overtake bonus", (0, 0))[1]))
    return ot, sot, pit


def load_overrides(season, rnd, conn=None):
    """sim_overrides.json for this round, on top of the confirmed penalties
    penalty_watch.py found (penalties.py). The file wins where both set one."""
    ov = {}
    if conn is not None:
        import penalties
        ov = penalties.as_overrides(conn, season, rnd)
    try:
        hand = json.loads(OVERRIDES.read_text()).get(f"{season}-{rnd}", {})
    except (OSError, ValueError):
        hand = {}
    for k, v in hand.items():
        if isinstance(v, dict) and isinstance(ov.get(k), dict):
            ov[k] = {**ov[k], **v}
        elif isinstance(v, list) and isinstance(ov.get(k), list):
            ov[k] = sorted(set(ov[k]) | set(v))
        else:
            ov[k] = v
    return ov


# ── The simulation ─────────────────────────────────────────────────────────
def field(conn, season, rnd, live):
    """Drivers taking part: [(player_id, code, team)] and constructors."""
    q = ("SELECT p.id, p.tla, p.team_id, p.type FROM players p " +
         ("WHERE p.active=1" if live else
          "JOIN player_gameday_stats g ON g.player_id=p.id AND g.season=? AND g.round=? AND g.played=1"))
    rows = conn.execute(q, () if live else (season, rnd)).fetchall()
    drivers = [(pid, tla, team) for pid, tla, team, t in rows if t == "driver"]
    cons = [pid for pid, tla, team, t in rows if t == "constructor"]
    # A team that changed drivers mid-season: use the code it raced with here.
    if not live:
        raced = {(c, t) for c, t in conn.execute(
            "SELECT code, team FROM hist_results WHERE season=? AND round=? AND kind='race'", (season, rnd))}
        if raced:
            drivers = [d for d in drivers if (d[1], d[2]) in raced] or drivers
    return drivers, cons


def simulate(conn, season, rnd, after="pre", sims=SIMS, seed=1, live=False):
    """Returns {player_id: [points per run]} plus meta. after: 'pre', 'fp1',
    'fp2', 'fp3' or 'quali' (what's known when the prediction is made)."""
    rng = random.Random(seed)
    circuit, sprint = conn.execute("SELECT circuit, sprint FROM hist_rounds WHERE season=? AND round=?",
                                   (season, rnd)).fetchone()
    drivers, cons = field(conn, season, rnd, live)
    keys = [(c, t) for _, c, t in drivers]
    pace = pace_ratings(conn, season, rnd, keys)

    # Practice: blend each session's team pace into car pace.
    sess = {"pre": [], "fp1": ["Practice 1"], "fp2": ["Practice 1", "Practice 2"],
            "fp3": ["Practice 1", "Practice 2", "Practice 3"],
            "quali": ["Practice 1", "Practice 2", "Practice 3"]}[after]
    if sprint and after != "pre":
        sess = ["Practice 1"]
    for fp in practice_pace(conn, season, rnd, sess):
        team_fp = collections.defaultdict(list)
        for c, t in keys:
            if c in fp:
                team_fp[t].append(fp[c])
        tm = {t: sum(v) / len(v) for t, v in team_fp.items()}
        if tm:
            base = min(tm.values())
            own_base = min(fp[c] for c, _ in keys if c in fp)
            hist_team = collections.defaultdict(list)
            for k in keys:
                hist_team[k[1]].append(pace[k])
            a = P["fp_driver"]
            new = {}
            for k in keys:
                if k[1] in tm:
                    team_gap = (tm[k[1]] - base) - sum(hist_team[k[1]]) / len(hist_team[k[1]])
                    own_gap = fp[k[0]] - own_base - pace[k] if k[0] in fp else team_gap
                    new[k] = pace[k] + P["fp_weight"] * ((1 - a) * team_gap + a * own_gap)
            pace.update(new)

    if P["pace_mult"] != 1.0:
        top = min(pace.values())
        pace = {k: top + (v - top) * P["pace_mult"] for k, v in pace.items()}

    ov = load_overrides(season, rnd, conn)
    for code, delta in ov.get("pace", {}).items():
        for k in keys:
            if k[0] == code:
                pace[k] += delta

    # Known qualifying and grid, once qualifying has happened.
    known_q, known_grid = None, None
    if after == "quali":
        qrows = dict(((c, t), p) for c, t, p in conn.execute(
            "SELECT code, team, pos FROM hist_quali WHERE season=? AND round=?", (season, rnd)))
        if qrows:
            known_q = {k: qrows.get(k, 0) for k in keys}
            # This weekend's qualifying pace is the best guide to race pace.
            now = quali_gaps(conn, season, rnd + 1).get(rnd, {})
            for k in keys:
                if k in now:
                    pace[k] = (1 - P["q_weight"]) * pace[k] + P["q_weight"] * now[k]
            grows = dict(((c, t), g) for c, t, g in conn.execute(
                "SELECT code, team, grid FROM hist_results WHERE season=? AND round=? AND kind='race'",
                (season, rnd)))
            if grows and not live:
                known_grid = {k: (grows.get(k) or 22) for k in keys}
                known_grid = {k: (22 if g == 0 else g) for k, g in known_grid.items()}
            else:
                known_grid = official_grid(conn, season, rnd, keys, ov) or grid_from_quali(known_q, ov, keys)

    # Race pace: one-lap pace, adjusted by how much better or worse each team
    # looked on practice long runs than on its best laps.
    rpace = dict(pace)
    # Only FP2: it's where teams run their race simulations. FP1/FP3 "long
    # runs" are mostly light-fuel and made the backtest worse.
    adj = long_run_adjust(conn, season, rnd, [s for s in sess if s == "Practice 2"], keys)
    for k in keys:
        rpace[k] += P["lr_weight"] * adj.get(k[1], 0.0)

    shuffle, avg_abs = track_shuffle(conn, circuit)
    dnf, dnf_avg = dnf_rates(conn, season, rnd)
    dnf = {t: r * P["dnf_mult"] for t, r in dnf.items()}
    dnf_avg *= P["dnf_mult"]
    flw = fl_weights(conn)
    ot, sot, pit = event_history(conn, season, rnd)
    field_ot = statistics.mean([o for v in ot.values() for _, o in v]) if ot else 5.0
    field_sot = statistics.mean([o for v in sot.values() for _, o in v]) if sot else 2.0

    def shrunk(hist, prior_mean):
        v = [o for _, o in hist[-12:]]
        return (sum(v) + P["ot_prior"] * prior_mean) / (len(v) + P["ot_prior"])

    did = {k: pid for (pid, c, t), k in zip(drivers, keys)}
    ot_mean = {k: shrunk(ot.get(did[k], []), field_ot) * shuffle * P["ot_mult"] for k in keys}
    sot_mean = {k: shrunk(sot.get(did[k], []), field_sot) * shuffle for k in keys}
    pit_hist = {c: [p for _, p in pit.get(c, [])[-10:]] or [3] for c in cons}

    # Race noise: set so the simulated shuffle (mean places moved from the
    # grid) matches the track's history.
    target = avg_abs * shuffle
    slot = P["slot"]
    sd = calibrate_sd(pace, rpace, keys, target, rng, slot, known_grid) * P["sd_mult"]

    out = {pid: [] for pid, _, _ in drivers}
    out.update({c: [] for c in cons})
    team_of = {k: k[1] for k in keys}
    for _ in range(sims):
        # Qualifying
        if known_q:
            qpos = known_q
        else:
            qt = sorted(keys, key=lambda k: pace[k] + rng.gauss(0, P["sd_quali"]))
            qpos = {k: i + 1 for i, k in enumerate(qt)}
        grid = known_grid or grid_from_quali(qpos, ov, keys)
        res = race(keys, rpace, grid, slot, sd, dnf, dnf_avg, ot_mean, flw, rng, team_of)
        sres = None
        if sprint:
            sq = sorted(keys, key=lambda k: pace[k] + rng.gauss(0, P["sd_quali"]))
            sgrid = {k: i + 1 for i, k in enumerate(sq)}
            sres = race(keys, rpace, sgrid, slot, sd * P["sprint_sd"], dnf, dnf_avg, sot_mean, flw, rng, team_of,
                        sprint=True)
        team_pts = collections.defaultdict(float)
        team_q = collections.defaultdict(list)
        for (pid, c, t), k in zip(drivers, keys):
            r = res[k]
            s = sres[k] if sres else None
            pts = score_driver(qpos.get(k), grid[k], r["finish"], r["classified"], r["overtakes"],
                               r["fl"], r["dotd"], s)
            out[pid].append(pts)
            team_pts[t] += pts - (10 if r["dotd"] else 0)
            team_q[t].append(qpos.get(k))
        for cid in cons:
            p = team_pts[cid] + quali_bonus(team_q[cid]) + rng.choice(pit_hist[cid]) + P["pit_add"]
            out[cid].append(p)
    meta = {"circuit": circuit, "sprint": bool(sprint), "after": after, "shuffle": round(shuffle, 2),
            "sd": round(sd, 3), "pace": {f"{k[0]}": round(v, 3) for k, v in pace.items()}}
    return out, meta


def official_grid(conn, season, rnd, keys, ov):
    """The starting grid OpenF1 publishes after qualifying (penalties
    applied), if it covers the whole field. sim_overrides.json "grid" (a list
    of codes in grid order) wins over it, for late changes."""
    if ov.get("grid"):
        order = {c: i + 1 for i, c in enumerate(ov["grid"])}
    else:
        try:
            order = dict(conn.execute("SELECT code, pos FROM starting_grid WHERE season=? AND round=?",
                                      (season, rnd)))
        except sqlite3.OperationalError:
            return None
    if not all(k[0] in order for k in keys):
        return None
    ranked = sorted(keys, key=lambda k: order[k[0]])
    return {k: i + 1 for i, k in enumerate(ranked)}


def grid_from_quali(qpos, ov, keys):
    """Qualifying order plus grid penalties (load_overrides):
    {"2026-19": {"grid_penalty": {"BOR": 5}, "back": ["BOT"], "pitlane": ["PER"]}}.
    Pit lane starters go behind the back-of-grid cars."""
    pen = ov.get("grid_penalty", {})
    back = set(ov.get("back", []))
    pit = set(ov.get("pitlane", []))
    order = sorted(keys, key=lambda k: (2 if k[0] in pit else 1 if k[0] in back else 0,
                                        (qpos.get(k) or 23) + pen.get(k[0], 0) + 0.01 * (k[0] in pen)))
    return {k: i + 1 for i, k in enumerate(order)}


def race(keys, pace, grid, slot, sd, dnf, dnf_avg, ot_mean, flw, rng, team_of, sprint=False):
    perf = {k: pace[k] + rng.gauss(0, sd) + slot * (grid[k] - 1) for k in keys}
    out_ = {k for k in keys if rng.random() < dnf.get(team_of[k], dnf_avg) * (0.4 if sprint else 1)}
    fin = sorted((k for k in keys if k not in out_), key=perf.get)
    res = {}
    for i, k in enumerate(fin):
        gained = grid[k] - (i + 1)
        lam = ot_mean[k] + P["ot_gain"] * max(0, gained)
        res[k] = {"grid": grid[k], "finish": i + 1, "classified": True, "overtakes": poisson(rng, lam), "fl": False, "dotd": False}
    for k in out_:
        res[k] = {"grid": grid[k], "finish": 99, "classified": False, "overtakes": poisson(rng, ot_mean[k] * 0.5),
                  "fl": False, "dotd": False}
    if fin:
        w = [flw[i] if i < len(flw) else 0.5 for i in range(len(fin))]
        res[rng.choices(fin, weights=w)[0]]["fl"] = True
        if not sprint:
            dw = [math.exp(0.22 * (grid[k] - (i + 1)) + 0.12 * (11 - (i + 1))) for i, k in enumerate(fin)]
            res[rng.choices(fin, weights=dw)[0]]["dotd"] = True
    return res


def poisson(rng, lam):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def calibrate_sd(pace, rpace, keys, target, rng, slot, known_grid=None):
    """Race noise (in % of lap time) that makes the mean |finish - grid| of
    finishers match the track's history."""
    def mean_abs(sd, n=150):
        tot = cnt = 0
        for _ in range(n):
            if known_grid:
                grid = known_grid
            else:
                qt = sorted(keys, key=lambda k: pace[k] + rng.gauss(0, P["sd_quali"]))
                grid = {k: i + 1 for i, k in enumerate(qt)}
            perf = sorted(keys, key=lambda k: rpace[k] + rng.gauss(0, sd) + slot * (grid[k] - 1))
            tot += sum(abs(grid[k] - (i + 1)) for i, k in enumerate(perf))
            cnt += len(perf)
        return tot / cnt
    lo, hi = 0.05, 4.0
    for _ in range(12):
        mid = (lo + hi) / 2
        if mean_abs(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ── Saving ─────────────────────────────────────────────────────────────────
def summarise(samples):
    n = len(samples)
    hist = collections.Counter(int(round(x)) for x in samples)
    mean = sum(samples) / n
    nn = sum(max(0, x) for x in samples) / n
    sd = statistics.pstdev(samples)
    return {"xpts": round(mean, 2), "xnn": round(nn, 2), "sd": round(sd, 2),
            "hist": {str(k): v for k, v in sorted(hist.items())}}


def save(conn, season, rnd, after, out, meta):
    conn.execute("""CREATE TABLE IF NOT EXISTS sim_predictions (
        season INTEGER, round INTEGER, player_id TEXT, after TEXT, xpts REAL, xnn REAL,
        sd REAL, hist TEXT, made_at TEXT, PRIMARY KEY (season, round, player_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sim_runs (
        season INTEGER, round INTEGER, after TEXT, meta TEXT, made_at TEXT,
        PRIMARY KEY (season, round))""")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("DELETE FROM sim_predictions WHERE season=? AND round=?", (season, rnd))
    for pid, samples in out.items():
        s = summarise(samples)
        conn.execute("INSERT INTO sim_predictions VALUES (?,?,?,?,?,?,?,?,?)",
                     (season, rnd, pid, after, s["xpts"], s["xnn"], s["sd"], json.dumps(s["hist"]), now))
    conn.execute("INSERT OR REPLACE INTO sim_runs VALUES (?,?,?,?,?)",
                 (season, rnd, after, json.dumps(meta), now))
    # The latest prediction at each stage, kept for sim_accuracy.py and the
    # Team Calculator's Simulation menu (very early, post FP1, ...).
    conn.execute("""CREATE TABLE IF NOT EXISTS sim_history (
        season INTEGER, round INTEGER, after TEXT, player_id TEXT, xpts REAL, made_at TEXT, xnn REAL,
        PRIMARY KEY (season, round, after, player_id))""")
    if "xnn" not in {r[1] for r in conn.execute("PRAGMA table_info(sim_history)")}:
        conn.execute("ALTER TABLE sim_history ADD COLUMN xnn REAL")
    for pid, samples in out.items():
        conn.execute("INSERT OR REPLACE INTO sim_history (season, round, after, player_id, xpts, made_at, xnn) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (season, rnd, after, pid, round(sum(samples) / len(samples), 2), now,
                      round(sum(max(0, x) for x in samples) / len(samples), 2)))
    conn.commit()


def stage_known(conn, season, rnd):
    """What's happened so far this weekend: 'quali' if qualifying results are
    in, else the last practice session with laps, else 'pre'."""
    if conn.execute("SELECT 1 FROM hist_quali WHERE season=? AND round=? LIMIT 1", (season, rnd)).fetchone():
        return "quali"
    have = {s for (s,) in conn.execute("SELECT DISTINCT session FROM practice_laps WHERE season=? AND round=?",
                                       (season, rnd))}
    for s, tag in (("Practice 3", "fp3"), ("Practice 2", "fp2"), ("Practice 1", "fp1")):
        if s in have:
            return tag
    return "pre"


def run_live(conn, season, rnd):
    after = stage_known(conn, season, rnd)
    out, meta = simulate(conn, season, rnd, after, live=True)
    save(conn, season, rnd, after, out, meta)
    return after, meta


def rounds_to_run(conn, season):
    """The Budget builder's round (the newest with prices), until its race is
    in, and the next round once that one's weekend is under way (what the
    Team calculator plans for)."""
    T = conn.execute("SELECT MAX(round) FROM player_results WHERE season=? AND value IS NOT NULL",
                     (season,)).fetchone()[0]
    if T is None:
        return []
    out = []
    raced = conn.execute("SELECT 1 FROM hist_results WHERE season=? AND round=? AND kind='race' LIMIT 1",
                         (season, T)).fetchone()
    if not raced:
        out.append(T)
    under_way = conn.execute("SELECT 1 FROM race_results WHERE season=? AND round=? LIMIT 1",
                             (season, T)).fetchone()
    if under_way and conn.execute("SELECT 1 FROM hist_rounds WHERE season=? AND round=?",
                                  (season, T + 1)).fetchone():
        out.append(T + 1)
    return out


if __name__ == "__main__":
    conn = sqlite3.connect(DB)
    season = int(time.strftime("%Y"))
    todo = [int(sys.argv[1])] if len(sys.argv) > 1 else rounds_to_run(conn, season)
    names = dict(conn.execute("SELECT id, COALESCE(tla, name) FROM players"))
    for rnd in todo:
        t0 = time.time()
        after, meta = run_live(conn, season, rnd)
        top = conn.execute("SELECT player_id, xpts FROM sim_predictions WHERE season=? AND round=? "
                           "ORDER BY xpts DESC LIMIT 8", (season, rnd)).fetchall()
        print(f"R{rnd} after {after}: {time.time() - t0:.1f}s, shuffle {meta['shuffle']}, sd {meta['sd']}: "
              + " ".join(f"{names.get(p, p)} {x:.0f}" for p, x in top))
