"""Backtest the race simulation (sim.py) on this season's finished rounds.

For each round it predicts with only what was known at the time (before the
weekend, after practice, after qualifying) and compares with the points
everyone actually scored. The yardstick is the plain 12-race average the Team
calculator used before the simulation (tools.FORM_ROUNDS).

  python3 sim_backtest.py [first_round last_round] [sims] [key=value ...]
  e.g. python3 sim_backtest.py 5 18 1500 sd_quali=0.3 slot=0.08
"""
import sqlite3
import statistics
import sys
import time

import sim


def actuals(conn, season):
    played = {(r, p) for r, p in conn.execute(
        "SELECT round, player_id FROM player_gameday_stats WHERE season=? AND played=1", (season,))}
    pts = {(r, p): g for r, p, g in conn.execute(
        "SELECT round, player_id, gameday_points FROM player_results WHERE season=? AND gameday_points IS NOT NULL",
        (season,))}
    return played, pts


def baseline(pts, played, pid, rnd, n=12):
    h = [pts[(r, pid)] for r in range(rnd - 1, 0, -1) if (r, pid) in played and (r, pid) in pts][:n]
    return sum(h) / len(h) if h else 0.0


def run(first, last, sims, stages=("pre", "fp3", "quali"), season=2026, verbose=True):
    conn = sqlite3.connect(sim.DB)
    played, pts = actuals(conn, season)
    types = dict(conn.execute("SELECT id, type FROM players"))
    err = {s: {"driver": [], "constructor": []} for s in stages + ("baseline",)}
    cover = {s: [] for s in stages}
    for rnd in range(first, last + 1):
        line = []
        for stage in stages:
            out, meta = sim.simulate(conn, season, rnd, stage, sims=sims, seed=rnd)
            e_round = []
            for pid, samples in out.items():
                if (rnd, pid) not in pts or (rnd, pid) not in played:
                    continue
                a = pts[(rnd, pid)]
                m = sum(samples) / len(samples)
                err[stage][types[pid]].append(abs(m - a))
                e_round.append(abs(m - a))
                srt = sorted(samples)
                lo, hi = srt[int(0.1 * len(srt))], srt[int(0.9 * len(srt)) - 1]
                cover[stage].append(lo <= a <= hi)
            line.append(f"{stage} {statistics.mean(e_round):5.2f}")
        eb = []
        for pid in out:
            if (rnd, pid) in pts and (rnd, pid) in played:
                e = abs(baseline(pts, played, pid, rnd) - pts[(rnd, pid)])
                err["baseline"][types[pid]].append(e)
                eb.append(e)
        if verbose:
            print(f"R{rnd:>2}  " + "  ".join(line) + f"  baseline {statistics.mean(eb):5.2f}", flush=True)
    summary = {}
    for s, d in err.items():
        allv = d["driver"] + d["constructor"]
        summary[s] = (statistics.mean(allv), statistics.mean(d["driver"]), statistics.mean(d["constructor"]))
        cov = f"  10-90% range holds {100 * sum(cover[s]) / len(cover[s]):.0f}%" if s in cover else ""
        if verbose:
            print(f"{s:>8}: all {summary[s][0]:5.2f}  drivers {summary[s][1]:5.2f}  "
                  f"constructors {summary[s][2]:5.2f}{cov}")
    return summary


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if "=" not in a]
    for a in sys.argv[1:]:
        if "=" in a:
            k, v = a.split("=")
            sim.P[k] = float(v)
    first, last = (int(args[0]), int(args[1])) if len(args) >= 2 else (5, 18)
    sims = int(args[2]) if len(args) >= 3 else 1500
    t0 = time.time()
    run(first, last, sims)
    print(f"{time.time() - t0:.0f}s")
