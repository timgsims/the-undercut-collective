"""How close were the predictions? Scores a finished round's predictions
against the points everyone actually scored.

Compares, per driver and constructor: our simulation at each stage it ran
(sim_history; stages it didn't save are re-run as the backtest would), the
12-race average the Team calculator used before the simulation, and
f1fantasytools.com's numbers where a snapshot was saved by hand in
~/f1ft-snapshots/<season>-<round>.json ({"stage": ..., "xpts": {"VER": 25.3, ...}}).

  python3 sim_accuracy.py [round]     (default: the latest finished round)
"""
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import sim
import sim_backtest

SNAPSHOTS = Path.home() / "f1ft-snapshots"


def main():
    conn = sqlite3.connect(sim.DB)
    season = int(time.strftime("%Y"))
    played, pts = sim_backtest.actuals(conn, season)
    final = {r for (r,) in conn.execute(
        "SELECT round FROM race_results WHERE season=? GROUP BY round HAVING MIN(is_final)=1", (season,))}
    rnd = int(sys.argv[1]) if len(sys.argv) > 1 else max(final)
    if rnd not in final:
        print(f"R{rnd} isn't final yet; points can still change.")
    names = dict(conn.execute("SELECT id, COALESCE(tla, name) FROM players"))
    types = dict(conn.execute("SELECT id, type FROM players"))
    ids = [p for (r, p) in played if r == rnd and (r, p) in pts]

    preds = {}
    try:
        for after, pid, x in conn.execute(
                "SELECT after, player_id, xpts FROM sim_history WHERE season=? AND round=?", (season, rnd)):
            preds.setdefault(f"sim {after}", {})[pid] = x
    except sqlite3.OperationalError:
        pass
    for stage in ("pre", "fp3", "quali"):
        if f"sim {stage}" not in preds:
            out, _ = sim.simulate(conn, season, rnd, stage, sims=4000, seed=rnd)
            preds[f"sim {stage} (re-run)"] = {p: sum(v) / len(v) for p, v in out.items()}
    preds["12-race average"] = {p: sim_backtest.baseline(pts, played, p, rnd) for p in ids}
    snap = SNAPSHOTS / f"{season}-{rnd}.json"
    if snap.exists():
        s = json.loads(snap.read_text())
        by_code = {names[p]: p for p in ids}
        preds[f"F1FT {s.get('stage', '')}"] = {by_code[c]: v for c, v in s["xpts"].items() if c in by_code}

    print(f"R{rnd}: mean error per asset (points), lower is better")
    for label, pr in preds.items():
        both = [p for p in ids if p in pr]
        if not both:
            continue
        e = {t: [abs(pr[p] - pts[(rnd, p)]) for p in both if types[p] == t] for t in ("driver", "constructor")}
        allv = e["driver"] + e["constructor"]
        print(f"  {label:<22} all {statistics.mean(allv):5.1f}   drivers {statistics.mean(e['driver']):5.1f}"
              f"   constructors {statistics.mean(e['constructor']):5.1f}   ({len(both)} assets)")
    print("\nBiggest misses (latest sim stage):")
    latest = next((v for stage in ("quali", "fp3", "pre") for k, v in preds.items()
                   if k.startswith(f"sim {stage}")), None)
    if latest:
        for p in sorted(ids, key=lambda p: -abs(latest.get(p, 0) - pts[(rnd, p)]))[:8]:
            print(f"  {names[p]:>4}  predicted {latest.get(p, 0):5.1f}  scored {pts[(rnd, p)]:5.0f}")


if __name__ == "__main__":
    main()
