"""The Tools tab: Team Calculator, Hindsight and the Budget Builder (see
TOOLS_PLAN.md).

They work the way f1fantasytools.com's Team Calculator, Hindsight and Budget
Builder do. The
data is read here at build time and baked into the page as JSON; the team
search runs in the browser, so budget, chip and filters change instantly.

F1 Fantasy's price rule, worked out from the 2026 history (2026-10-31): after
each round, an asset's price moves by its average points per $M over the last
3 rounds it took part in (including this one), against its current price:

    avg PPM      < 0.6   0.6-0.9   0.9-1.2   1.2+
    $18.5M+       -0.3     -0.1      +0.1     +0.3
    under         -0.6     -0.2      +0.2     +0.6

Prices stay between $3.0M and $34.0M, and an asset that didn't take part in
the round doesn't move. That reproduces 605 of the 608 changes so far; the
other 3 are odd ones (Lawson twice, Gasly once)."""
import json
import math
import sqlite3

PRICE_MIN, PRICE_MAX = 3.0, 34.0
TIER_A_FROM = 18.5
PPM_CUTS = (0.6, 0.9, 1.2)
STEPS_A = (-0.3, -0.1, 0.1, 0.3)
STEPS_B = (-0.6, -0.2, 0.2, 0.6)
# Team Calculator's expected points: the plain average of the last 12 rounds
# an asset took part in. Backtested on 2026 rounds 5-18: longer windows beat
# shorter ones and weighting recent rounds more made it worse (mean error
# 13.4 pts with 12 rounds, 14.4 with 3).
FORM_ROUNDS = 12


def steps_for(price):
    return STEPS_A if price >= TIER_A_FROM else STEPS_B


def clamp_step(price, step):
    return round(min(PRICE_MAX, max(PRICE_MIN, price + step)) - price, 1)


def price_change(price, window_pts):
    """window_pts: points in each of the last 3 rounds the asset took part in,
    this one included. Returns the price change."""
    if not window_pts:
        return 0.0
    ppm = sum(window_pts) / len(window_pts) / price
    band = sum(ppm >= c for c in PPM_CUTS)
    return clamp_step(price, steps_for(price)[band])


def _norm_cdf(x, mean, sd):
    return 0.5 * (1 + math.erf((x - mean) / (sd * math.sqrt(2))))


# Team colours by F1 Fantasy team id (a constructor's id is its team id).
# F1's 2026 team colours; Audi and Cadillac picked to stay apart from
# Ferrari and Haas.
TEAM_COLOURS = {
    "27": "#F47600",    # McLaren
    "28": "#00D7B6",    # Mercedes
    "29": "#4781D7",    # Red Bull Racing
    "25": "#ED1131",    # Ferrari
    "23": "#00A1E8",    # Alpine
    "24": "#229971",    # Aston Martin
    "210": "#1868DB",   # Williams
    "2636": "#6C98FF",  # Racing Bulls
    "26": "#9C9FA2",    # Haas
    "2640": "#8A0F2A",  # Audi
    "2641": "#D8D8D8",  # Cadillac
}


def _ink(hex_colour):
    """Black or white text, whichever reads better on the colour."""
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111" if lum > 0.5 else "#fff"


def read_tools_data(conn, season, race_name_by_round):
    """Everything the Tools tab needs, as plain JSON-able data."""
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    has_tla = "tla" in pcols and "active" in pcols
    has_team = "team_id" in pcols
    players = {}
    for row in conn.execute(
        "SELECT id, name, type" + (", tla, active" if has_tla else ", NULL, 1")
        + (", team_id" if has_team else ", NULL") + " FROM players"
    ):
        pid, name, ptype, tla, active, team = row
        if ptype not in ("driver", "constructor"):
            continue
        code = tla or (name or pid).replace(".", "").split()[-1][:3].upper()
        players[pid] = {"code": code, "name": name, "type": ptype, "active": active != 0,
                        "team": team or (pid if ptype == "constructor" else None)}

    try:
        played = {(r, pid): bool(p) for r, pid, p in conn.execute(
            "SELECT round, player_id, played FROM player_gameday_stats WHERE season=?", (season,))}
    except Exception:
        played = {}

    res = {}   # (round, pid) -> (points, no-negative points, price)
    for r, pid, pts, nn, val in conn.execute(
        "SELECT round, player_id, gameday_points, no_negative_points, value "
        "FROM player_results WHERE season=?", (season,)
    ):
        res[(r, pid)] = (pts, nn, val)

    def took_part(r, pid):
        if (r, pid) in played:
            return played[(r, pid)]
        return res.get((r, pid), (None,))[0] not in (None, 0)  # old rounds, before the stats feed

    managers = {mid: name for mid, name in conn.execute("SELECT id, name FROM managers")}
    chips = {}
    for mid, chip, rnd in conn.execute(
        "SELECT manager_id, chip_name, round_taken FROM chips_used WHERE season=?", (season,)
    ):
        chips[(mid, rnd)] = chip

    # ── Hindsight: every round with league results ──────────────────────────
    rounds = [r for (r,) in conn.execute(
        "SELECT DISTINCT round FROM race_results WHERE season=? ORDER BY round", (season,))]
    final = {r: bool(f) for r, f in conn.execute(
        "SELECT round, MAX(is_final) FROM race_results WHERE season=? GROUP BY round", (season,))}
    team_value = {(r, mid): v for r, mid, v in conn.execute(
        "SELECT round, manager_id, team_value FROM race_results WHERE season=?", (season,))}
    hindsight = []
    for r in rounds:
        assets = []
        for pid, p in players.items():
            pts, nn, price = res.get((r, pid), (None, None, None))
            if pts is None or price is None or not took_part(r, pid):
                continue
            nxt = res.get((r + 1, pid), (None, None, None))[2]
            assets.append({
                "id": pid, "c": p["code"], "t": "C" if p["type"] == "constructor" else "D", "k": p["team"],
                "p": round(price, 1), "x": pts, "n": nn if nn is not None else max(0.0, pts),
                "d": round(nxt - price, 1) if nxt is not None else None,
            })
        teams = []
        for mid, pts, cap, mega in conn.execute(
            "SELECT manager_id, points, captain_player_id, mega_captain_player_id FROM race_results "
            "WHERE season=? AND round=?", (season, r)
        ):
            picks = [pid for (pid,) in conn.execute(
                "SELECT player_id FROM team_picks WHERE season=? AND round=? AND manager_id=? "
                "ORDER BY position", (season, r, mid))]
            budget = 100.0 if r == 1 else team_value.get((r - 1, mid))
            teams.append({"m": managers.get(mid, mid), "pts": pts, "picks": picks,
                          "cap": cap, "mega": mega, "chip": chips.get((mid, r)),
                          "budget": round(budget, 1) if budget else None})
        teams.sort(key=lambda t: t["m"])
        hindsight.append({"r": r, "name": race_name_by_round.get(r, f"Round {r}"),
                          "final": final.get(r, False), "assets": assets, "teams": teams})

    # ── The race simulation's predictions (sim.py), where it has run ───────
    sims, sim_after = {}, {}
    try:
        for r, pid, xp, xn, hist in conn.execute(
                "SELECT round, player_id, xpts, xnn, hist FROM sim_predictions WHERE season=?", (season,)):
            h = {int(k): v for k, v in json.loads(hist).items()}
            sims[(r, pid)] = (xp, xn, h, sum(h.values()))
        sim_after = dict(conn.execute("SELECT round, after FROM sim_runs WHERE season=?", (season,)))
    except sqlite3.OperationalError:
        pass   # the simulation hasn't run on this database yet

    # ── Budget Builder (price predictor): the newest round F1 has published prices for ───────
    priced = [r for (r,) in conn.execute(
        "SELECT DISTINCT round FROM player_results WHERE season=? AND value IS NOT NULL "
        "ORDER BY round", (season,))]
    predictor = None
    if priced:
        T = priced[-1]
        live = T in rounds
        rows = []
        for pid, p in players.items():
            price = res.get((T, pid), (None, None, None))[2]
            if price is None or not p["active"]:
                continue
            prev = [(r, res.get((r, pid), (None,))[0] if took_part(r, pid) else None)
                    for r in (T - 2, T - 1) if r >= 1]
            counted = [x for _, x in prev if x is not None]
            n = len(counted) + 1
            needs = [round(c * n * price - sum(counted), 1) for c in PPM_CUTS]
            steps = [clamp_step(price, s) for s in steps_for(price)]
            # Odds from recent form: a normal curve over the last 8 rounds the
            # asset took part in. A placeholder until the simulation (plan step B).
            hist = [res[(r, pid)][0] for r in range(T - 1, 0, -1)
                    if took_part(r, pid) and res.get((r, pid), (None,))[0] is not None][:8]
            mean = sum(hist) / len(hist) if hist else 0.0
            sd = (sum((h - mean) ** 2 for h in hist) / (len(hist) - 1)) ** 0.5 if len(hist) > 2 else 15.0
            sd = max(sd, 6.0)
            cdf = [_norm_cdf(x - 0.5, mean, sd) for x in needs]
            sim_t = sims.get((T, pid))
            if sim_t:   # the simulation's points distribution: P(points < need)
                _, _, h, n_s = sim_t
                cdf = [sum(v for k, v in h.items() if k < x) / n_s for x in needs]
                mean = sim_t[0]
            probs = [cdf[0], cdf[1] - cdf[0], cdf[2] - cdf[1], 1 - cdf[2]]
            # Expected points for the Team Calculator (plan step A, until the
            # simulation).
            played_r = [r for r in range(T - 1, 0, -1)
                        if took_part(r, pid) and res.get((r, pid), (None,))[0] is not None][:FORM_ROUNDS]
            n_r = len(played_r) or 1
            xp = sum(res[(r, pid)][0] for r in played_r) / n_r
            xn = sum(res[(r, pid)][1] if res[(r, pid)][1] is not None else max(0.0, res[(r, pid)][0])
                     for r in played_r) / n_r
            rows.append({
                "id": pid, "c": p["code"], "t": "C" if p["type"] == "constructor" else "D", "k": p["team"],
                "p": round(price, 1), "prev": prev, "needs": needs, "steps": steps,
                "probs": [round(x, 3) for x in probs], "mean": round(mean, 1),
                "xd": round(sum(pr * s for pr, s in zip(probs, steps)), 2),
                "xp": round(xp, 1), "xn": round(xn, 1),
            })
        rows.sort(key=lambda a: -a["p"])
        predictor = {"r": T, "name": race_name_by_round.get(T, f"Round {T}"), "live": live,
                     "prevRounds": [r for r in (T - 2, T - 1) if r >= 1], "assets": rows,
                     "src": sim_after.get(T) if any((T, a["id"]) in sims for a in rows) else None}

    # ── Team Calculator: each manager's team going into the next deadline ───
    # Before a round locks that's round T itself, starting from their T-1 team;
    # once T is under way it's T+1, starting from their T team (at T's prices
    # until F1 publishes the new ones).
    calc = None
    if predictor:
        T = predictor["r"]
        target = T + 1 if predictor["live"] else T
        start = target - 1
        price_now = {a["id"]: a["p"] for a in predictor["assets"]}
        rr = {(r, mid): (bal, subs, made, cost) for r, mid, bal, subs, made, cost in conn.execute(
            "SELECT round, manager_id, team_balance, subs_allowed, transfers_made, extra_sub_cost "
            "FROM race_results WHERE season=?", (season,))}
        used = {}
        for (mid, rnd), chip in chips.items():
            used.setdefault(mid, []).append(chip)
        mgrs = []
        for mid, name in managers.items():
            s = start
            if chips.get((mid, s)) == "LL":   # Limitless: the team goes back afterwards
                s -= 1
            picks = [pid for (pid,) in conn.execute(
                "SELECT player_id FROM team_picks WHERE season=? AND round=? AND manager_id=? "
                "ORDER BY position", (season, s, mid))]
            if not picks or (s, mid) not in rr:
                continue
            bal, subs, made, cost = rr[(s, mid)]
            if (target, mid) in rr and rr[(target, mid)][1] is not None:
                free = rr[(target, mid)][1]
            else:   # 2 a round, plus 1 carried over if any went unused
                free = min(3, 2 + (1 if (subs or 0) - (made or 0) > 0 else 0))
            mgrs.append({"m": name, "picks": picks, "bank": round(bal or 0.0, 1),
                         "budget": round((bal or 0.0) + sum(price_now.get(p, 0.0) for p in picks), 1),
                         "free": free, "cost": cost or 10.0, "used": sorted(used.get(mid, []))})
        mgrs.sort(key=lambda m: m["m"])
        # Expected points for the target round: the simulation's where it has
        # run, otherwise the 12-race average already on each asset.
        src = None
        for a in predictor["assets"]:
            s = sims.get((target, a["id"]))
            a["ax"], a["an"] = a["xp"], a["xn"]   # the 12-race average, for the Simulation menu
            if s:
                a["xp"], a["xn"] = round(s[0], 1), round(s[1], 1)
                src = sim_after.get(target)
        # Every stage the simulation has saved for this round, oldest first,
        # for the Simulation menu (the latest is what's shown by default).
        stages = []
        try:
            got = {}
            for after, pid, xp, xn, made in conn.execute(
                    "SELECT after, player_id, xpts, xnn, made_at FROM sim_history WHERE season=? AND round=?",
                    (season, target)):
                s = got.setdefault(after, {"after": after, "made": made, "x": {}})
                s["made"] = max(s["made"], made)
                s["x"][pid] = [round(xp, 1), round(xn if xn is not None else max(0.0, xp), 1)]
            order = ["pre", "fp1", "fp2", "fp3", "quali"]
            stages = sorted(got.values(), key=lambda s: order.index(s["after"]) if s["after"] in order else 9)
        except sqlite3.OperationalError:
            pass
        calc = {"r": target, "name": race_name_by_round.get(target, f"Round {target}"),
                "pricesFrom": T, "managers": mgrs, "src": src, "sims": stages}

    colours = {k: [c, _ink(c)] for k, c in TEAM_COLOURS.items()}
    # ── Elite Data: the global top 500 (elite.py), next to the league ───────
    elite = []
    try:
        rows = conn.execute("SELECT round, picks, captain, mega_captain, chip, detail_at FROM elite_teams "
                            "WHERE season=? ORDER BY round", (season,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    by_round = {}
    for r, picks, cap, mega, chip, det in rows:
        e = by_round.setdefault(r, {"n": 0, "nd": 0, "pick": {}, "drs": {}, "x3": {}, "chips": {}})
        e["n"] += 1
        for p in json.loads(picks):
            e["pick"][p] = e["pick"].get(p, 0) + 1
        if det and cap:
            e["nd"] += 1
            e["drs"][cap] = e["drs"].get(cap, 0) + 1
            if mega:
                e["x3"][mega] = e["x3"].get(mega, 0) + 1
            if chip:
                e["chips"][chip] = e["chips"].get(chip, 0) + 1
    for r, e in sorted(by_round.items()):
        own = {}
        mgr_n = 0
        for mid, in conn.execute("SELECT DISTINCT manager_id FROM team_picks WHERE season=? AND round=?", (season, r)):
            mgr_n += 1
            for (p,) in conn.execute("SELECT player_id FROM team_picks WHERE season=? AND round=? AND manager_id=?",
                                     (season, r, mid)):
                own[p] = own.get(p, 0) + 1
        # Every asset in the game, like F1FT, so a 0% pick still shows.
        ids = set(e["pick"]) | set(own) | {pid for pid, p in players.items() if p["active"]}
        assets = []
        for pid in ids:
            p = players.get(pid)
            if not p:
                continue
            assets.append({"id": pid, "c": p["code"], "t": "C" if p["type"] == "constructor" else "D",
                           "k": p["team"], "pk": round(100 * e["pick"].get(pid, 0) / e["n"], 1),
                           "drs": round(100 * (e["drs"].get(pid, 0) + e["x3"].get(pid, 0)) / e["nd"], 1) if e["nd"] else None,
                           "lg": round(100 * own.get(pid, 0) / mgr_n) if mgr_n else None})
        elite.append({"r": r, "name": race_name_by_round.get(r, f"Round {r}"), "n": e["n"], "nd": e["nd"],
                      "mgrs": mgr_n, "chips": e["chips"], "assets": assets})

    return {"hindsight": hindsight, "predictor": predictor, "calc": calc, "colours": colours, "elite": elite}


def _json(v):
    return json.dumps(v, separators=(",", ":")).replace("</", "<\\/")


def panel_tools(data):
    tools = data.get("tools") or {}
    if not tools.get("hindsight") and not tools.get("predictor") and not tools.get("calc"):
        return '<div class="card" style="padding:16px;color:#888">No data yet.</div>'
    return (TOOLS_HTML
            .replace("__TOOLS_DATA__", _json(tools))
            .replace("__MANAGER_COLOURS__", _json({m["name"]: m["color"] for m in data["managers"]})))


TOOLS_HTML = r"""
<style>
.tl-sub{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:4px 0 12px}
@media (max-width:520px){.tl-sub{grid-template-columns:repeat(2,1fr)}}
.tl-sub button{flex:1;font-size:13px;padding:8px 10px;border-radius:8px;border:0.5px solid #333;background:#1a1a1a;color:#aaa;cursor:pointer}
.tl-sub button.on{background:#2a2a2a;color:#eee;border-color:#555}
.tl-ctl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.tl-ctl label{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.04em;display:block;margin-bottom:3px}
#panel-tools button{font-family:inherit}
.tl-ctl select,.tl-ctl input{width:100%;box-sizing:border-box;font-family:inherit;font-size:16px;padding:7px 28px 7px 9px;border-radius:8px;border:0.5px solid #333;background-color:#1a1a1a;color:#e8e8e6;-webkit-appearance:none;appearance:none}
.tl-ctl input{padding-right:9px}
.tl-ctl select{background-image:linear-gradient(45deg,transparent 50%,#888 50%),linear-gradient(135deg,#888 50%,transparent 50%);background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;background-size:5px 5px;background-repeat:no-repeat}
.tl-note{font-size:11px;color:#777;line-height:1.45;margin:6px 0 10px}
.tl-team{display:grid;grid-template-columns:16px 1fr 44px 40px;gap:4px;align-items:center;padding:8px 0;border-bottom:0.5px solid #2a2a2a}
.tl-rank{font-size:11px;color:#666}
.tl-codes{display:flex;flex-wrap:wrap;gap:3px}
.tl-code{display:inline-block;font-variant-numeric:tabular-nums;font-size:11px;font-weight:600;padding:2px 5px;border-radius:5px;background:#252525;color:#ddd;white-space:nowrap}
.tl-code.x2{box-shadow:0 0 0 2px #88dd44}
.tl-code.x3{box-shadow:0 0 0 2px #ffaa44}
.tl-codes .tl-code.x2,.tl-codes .tl-code.x3{margin:0 2px}
.tl-num{text-align:right;font-size:13px;font-variant-numeric:tabular-nums}
.tl-num small{display:block;font-size:10px;color:#777}
.tl-assets{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px}
.tl-assets button{font-size:11px;font-weight:600;padding:4px 6px;border-radius:6px;border:0.5px solid #333;background:#1a1a1a;color:#aaa;cursor:pointer}
.tl-assets button.inc{box-shadow:0 0 0 2px #88dd44;margin:0 2px}
.tl-assets button.exc{opacity:.3;text-decoration:line-through}
.tl-assets .gap{flex-basis:100%;height:6px}
.tl-k{display:inline-block;width:2.4em;text-align:center}
.tl-v.b{width:1.5em}
.tl-team .tl-code{font-size:10px;padding:2px 4px}
.tl-team .tl-k{width:2.2em}
.tl-v{display:inline-block;width:2.3em;text-align:right;font-variant-numeric:tabular-nums}
.tl-assets button{font-variant-numeric:tabular-nums}
.tl-cmp{font-size:13px;line-height:1.5;padding:10px 0}
.tl-more{width:100%;margin:10px 0 4px;font-size:13px;padding:8px;border-radius:8px;border:0.5px solid #333;background:#1a1a1a;color:#aaa;cursor:pointer}
.tl-pp{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.tl-pp th{font-size:9px;color:#666;font-weight:500;text-align:right;padding:4px 2px;text-transform:uppercase}
.tl-pp th:first-child,.tl-pp td:first-child{text-align:left}
.tl-pp td{font-size:12px;text-align:right;padding:6px 2px;border-top:0.5px solid #2a2a2a;vertical-align:top}
.tl-pp td small{display:block;font-size:9px;color:#666;font-weight:400}
.tl-pp .lk{font-weight:600}
.tl-up{color:#88dd44}.tl-dn{color:#ff7a7a}
.tl-head{display:flex;align-items:center;gap:10px;margin:2px 0 6px}
.tl-head .rn{font-size:12px;font-weight:700;padding:4px 8px;border-radius:6px;background:#e10600;color:#fff;letter-spacing:.03em}
.tl-head .nm{font-size:20px;font-weight:700;color:#f2f2f0;line-height:1.1}
.tl-head small{display:block;font-size:11px;font-weight:400;color:#888;margin-top:2px}
.tl-io{flex-basis:100%;font-size:10px;color:#888;margin-top:2px}
.tl-sub button{padding:8px 4px}
</style>
<div class="tl-sub">
  <button id="tlBtnC" class="on" onclick="tlShow('c')">Team Calculator</button>
  <button id="tlBtnH" onclick="tlShow('h')">Hindsight</button>
  <button id="tlBtnP" onclick="tlShow('p')">Budget Builder</button>
  <button id="tlBtnE" onclick="tlShow('e')">Elite Data</button>
</div>
<div id="tlC">
  <div id="tcHead"></div>
  <div class="tl-note" id="tlCNote"></div>
  <div class="tl-ctl">
    <div style="grid-column:1/-1"><label>Simulation</label><select id="tcSim"></select></div>
    <div><label>Manager</label><select id="tcMgr"></select></div>
    <div><label>Free transfers</label><select id="tcFree">
      <option value="0">0</option><option value="1">1</option><option value="2">2</option>
      <option value="3">3</option><option value="99">Unlimited</option></select></div>
    <div><label>Max budget ($M)</label><input id="tcBudget" type="number" step="0.1" inputmode="decimal"></div>
    <div><label>Chip</label><select id="tcChip">
      <option value="">None</option><option value="X3">Extra DRS (3x)</option>
      <option value="WC">Wildcard</option><option value="LL">Limitless</option>
      <option value="NN">No Negative</option><option value="AP">Auto Pilot</option></select></div>
    <div style="grid-column:1/-1"><label>Count price changes as points</label><select id="tcW">
      <option value="0">No, points only</option><option value="10">Yes, $1M = 10 pts</option>
      <option value="20">Yes, $1M = 20 pts</option><option value="40">Yes, $1M = 40 pts</option></select></div>
  </div>
  <div class="tl-note" style="margin-bottom:2px">Expected points for the next race. Tap a driver or constructor to require it, again to leave it out, again to reset.</div>
  <div class="tl-assets" id="tcAssets"></div>
  <div id="tcCur" class="tl-cmp"></div>
  <div class="card" style="padding:4px 12px" id="tcTeams"></div>
  <button class="tl-more" id="tcMore">Show more teams</button>
</div>
<div id="tlH" style="display:none">
  <div class="tl-note">The best teams you could have picked for a past round, from the points every driver and constructor actually scored. Pick a manager to use their budget and compare with what they had.</div>
  <div class="tl-ctl">
    <div><label>Round</label><select id="tlRound"></select></div>
    <div><label>Manager</label><select id="tlMgr"></select></div>
    <div><label>Max budget ($M)</label><input id="tlBudget" type="number" step="0.1" inputmode="decimal"></div>
    <div><label>Chip</label><select id="tlChip">
      <option value="">None</option><option value="X3">Extra DRS (3x)</option>
      <option value="NN">No Negative</option><option value="LL">Limitless</option></select></div>
  </div>
  <div class="tl-note" style="margin-bottom:2px">Tap a driver or constructor to require it, again to leave it out, again to reset.</div>
  <div class="tl-assets" id="tlAssets"></div>
  <div id="tlCmp" class="tl-cmp"></div>
  <div class="card" style="padding:4px 12px" id="tlTeams"></div>
  <button class="tl-more" id="tlMore">Show more teams</button>
</div>
<div id="tlP" style="display:none"></div>
<div id="tlE" style="display:none"></div>
<script>
(function(){
const T=__TOOLS_DATA__, COL=__MANAGER_COLOURS__;
const $=id=>document.getElementById(id);
window.tlShow=function(w){
  for(const k of ['c','h','p','e']){$('tl'+k.toUpperCase()).style.display=w===k?'':'none';
    $('tlBtn'+k.toUpperCase()).classList.toggle('on',w===k);}
};
const fmt=(v,d=1)=>v==null?'—':(+v).toFixed(d);
const sgn=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(1);
const TC=T.colours||{};
// Same-width pills: the code and the number each get a fixed-width box.
const K=a=>`<span class="tl-k">${a.c}</span>`, V=v=>`<span class="tl-v">${v}</span>`;
const SIMWHEN={pre:'before practice',fp1:'after FP1',fp2:'after FP2',fp3:'after FP3',quali:'after qualifying'};
const SIMSTAGE={pre:'very early',fp1:'post FP1',fp2:'post FP2',fp3:'post FP3',quali:'post qualifying'};
const head=(r,name,sub)=>`<div class="tl-head"><span class="rn">R${r}</span><div class="nm">${name}<small>${sub}</small></div></div>`;
// Constructors first, then drivers; each highest points first.
const byPts=(a,b)=>(a.t>b.t?1:a.t<b.t?-1:b.x-a.x||b.p-a.p);
const tc=a=>{const c=TC[a.k];return c?`background:${c[0]};color:${c[1]}`:'';};

// ── Team search: every 5-driver + 2-constructor team under budget. DRS (2x)
// goes on the best driver; Extra DRS puts 3x on the best and 2x on the next.
function combos(arr,k){const out=[];const n=arr.length;const idx=[...Array(k).keys()];
  if(n<k)return out;
  while(true){out.push(idx.map(i=>arr[i]));let i=k-1;while(i>=0&&idx[i]===n-k+i)i--;if(i<0)break;idx[i]++;for(let j=i+1;j<k;j++)idx[j]=idx[j-1]+1;}
  return out;}
// With a starting team (o.start), transfers beyond o.free cost o.cost points
// each (none on Wildcard or Limitless); o.w counts $1M of price change as that
// many points when ranking.
function bestTeams(assets,o){
  const val=a=>o.chip==='NN'?a.n:a.x;
  const w=o.w||0;
  const ok=a=>!o.exc.has(a.id);
  const D=assets.filter(a=>a.t==='D'&&ok(a)), C=assets.filter(a=>a.t==='C'&&ok(a));
  const incD=D.filter(a=>o.inc.has(a.id)), incC=C.filter(a=>o.inc.has(a.id));
  const budget=o.chip==='LL'?1e9:o.budget;
  const ds=combos(D,5).filter(s=>incD.every(a=>s.includes(a))).map(s=>{
    const v=s.map(val).sort((a,b)=>b-a);
    const base=v.reduce((a,b)=>a+b,0);
    const pts=o.chip==='X3'?base+2*v[0]+v[1]:base+v[0];
    const d=s.reduce((a,b)=>a+(b.d||0),0);
    return {s,cost:s.reduce((a,b)=>a+b.p,0),pts,d,r:pts+w*d};
  }).sort((a,b)=>b.r-a.r);
  const cs=combos(C,2).filter(s=>incC.every(a=>s.includes(a)));
  const top=[];let floor=-1e9;const N=o.n;
  const free=o.chip==='WC'||o.chip==='LL'?99:(o.free==null?99:o.free);
  for(const c of cs){const cc=c[0].p+c[1].p, cp=val(c[0])+val(c[1]), cd=(c[0].d||0)+(c[1].d||0);
    const cr=cp+w*cd, left=budget-cc+1e-6;
    for(const d of ds){ if(d.r+cr<=floor&&top.length>=N)break;
      if(d.cost>left)continue;
      let tr=null,pen=0;
      if(o.start){tr=c.concat(d.s).filter(a=>!o.start.has(a.id)).length;pen=Math.max(0,tr-free)*(o.cost||10);}
      const score=d.r+cr-pen; if(score<=floor&&top.length>=N)continue;
      const t={c,d:d.s,pts:d.pts+cp-pen,score,cost:cc+d.cost,dp:cd+d.d,tr,pen};
      top.push(t);top.sort((a,b)=>b.score-a.score||a.cost-b.cost);
      if(top.length>N)top.pop();
      if(top.length>=N)floor=top[top.length-1].score;}}
  return top;}
function boosts(drivers,chip){const v=drivers.map(a=>[chip==='NN'?a.n:a.x,a.id]).sort((a,b)=>b[0]-a[0]);
  return chip==='X3'?{[v[0][1]]:'x3',[v[1][1]]:'x2'}:{[v[0][1]]:'x2'};}
function teamRow(t,i,chip,start,byId,dec){
  const b=boosts(t.d,chip);
  const codes=t.c.map(a=>`<span class="tl-code" style="${tc(a)}">${K(a)}</span>`).join('')+
    t.d.slice().sort((a,b)=>(chip==='NN'?b.n-a.n:b.x-a.x)).map(a=>`<span class="tl-code ${b[a.id]||''}" style="${tc(a)}">${K(a)}${b[a.id]?`<span class="tl-v b">${b[a.id]}</span>`:''}</span>`).join('');
  let io='';
  if(start){const ids=new Set(t.c.concat(t.d).map(a=>a.id));
    const inn=t.c.concat(t.d).filter(a=>!start.has(a.id)).map(a=>a.c);
    const out=[...start].filter(id=>!ids.has(id)).map(id=>byId[id]?byId[id].c:'?');
    io=`<div class="tl-io">${inn.length?`In ${inn.join(' ')} · Out ${out.join(' ')}`:'No transfers'}${t.pen?` · <span class="tl-dn">−${t.pen} pts</span>`:''}</div>`;}
  const pts=dec?t.pts.toFixed(1):Math.round(t.pts);
  return `<div class="tl-team"><div class="tl-rank">${i+1}</div><div class="tl-codes">${codes}${io}</div>
    <div class="tl-num">${pts}<small>pts</small></div><div class="tl-num">${fmt(t.cost)}<small>${sgn(Math.round(t.dp*10)/10)}</small></div></div>`;}

// ── Team calculator ────────────────────────────────────────────────────────
const C=T.calc, PA=(T.predictor||{}).assets||[];
const CA=PA.map(a=>({id:a.id,c:a.c,t:a.t,k:a.k,p:a.p,x:a.xp,n:a.xn,d:a.xd}));
const CById=Object.fromEntries(CA.map(a=>[a.id,a]));
const cst={inc:new Set(),exc:new Set(),n:10};
const cM=$('tcMgr'), cF=$('tcFree'), cB=$('tcBudget'), cC=$('tcChip'), cW=$('tcW');
const CHIP={X3:'Extra DRS',WC:'Wildcard',LL:'Limitless',NN:'No Negative',AP:'Auto Pilot'};
const CHIP_IDS={X3:['X3','DRS','EXTRA DRS','EXTRADRS'],WC:['WC','WILDCARD'],LL:['LL','LIMITLESS'],NN:['NN','NO NEGATIVE','NONEGATIVE'],AP:['AP','AUTO PILOT','AUTOPILOT']};
function cMgr(){return C&&C.managers.find(m=>m.m===cM.value);}
function cSetMgr(){const m=cMgr();
  cB.value=(m?m.budget:100).toFixed(1); cF.value=m?String(Math.min(3,m.free)):'99';
  [...cC.options].forEach(o=>{if(!o.value)return;
    const u=!!m&&m.used.some(x=>CHIP_IDS[o.value].includes(String(x).toUpperCase()));
    o.disabled=u; o.text=CHIP[o.value]+(o.value==='X3'?' (3x)':'')+(u?' (used)':'');});
  if(cC.selectedOptions[0]&&cC.selectedOptions[0].disabled)cC.value='';}
function cAssets(){
  const sorted=CA.slice().sort(byPts);
  $('tcAssets').innerHTML=sorted.map((a,i)=>(i&&a.t!==sorted[i-1].t?'<div class="gap"></div>':'')+
    `<button data-id="${a.id}" class="${cst.inc.has(a.id)?'inc':cst.exc.has(a.id)?'exc':''}" style="${tc(a)}">${K(a)}<span class="tl-v" style="font-weight:400;opacity:.75">${Math.round(a.x)}</span></button>`).join('');
  $('tcAssets').querySelectorAll('button').forEach(b=>b.onclick=()=>{const id=b.dataset.id;
    if(cst.inc.has(id)){cst.inc.delete(id);cst.exc.add(id);}else if(cst.exc.has(id)){cst.exc.delete(id);}else cst.inc.add(id);
    cAssets();runC();});}
function runC(){
  if(!C||!CA.length){$('tcTeams').innerHTML='<div class="tl-note">No prices yet.</div>';return;}
  const m=cMgr(); const start=m?new Set(m.picks):null;
  const o={budget:parseFloat(cB.value)||100,chip:cC.value,inc:cst.inc,exc:cst.exc,n:cst.n,
    w:+cW.value,start,free:+cF.value,cost:m?m.cost:10};
  const top=bestTeams(CA,o);
  $('tcTeams').innerHTML=top.length?`<div class="tl-team" style="padding:4px 0"><div></div><div class="tl-rank">TEAM</div><div class="tl-num"><small>xPTS</small></div><div class="tl-num"><small>COST / xΔ$</small></div></div>`+top.map((t,i)=>teamRow(t,i,o.chip,start,CById,true)).join('')
    :'<div class="tl-note">No team fits that budget and those filters.</div>';
  $('tcMore').style.display=top.length>=cst.n&&cst.n<50?'':'none';
  if(m){const mine=m.picks.map(id=>CById[id]).filter(Boolean);const col=COL[m.m]||'#ccc';
    const D=mine.filter(a=>a.t==='D'), Cn=mine.filter(a=>a.t==='C');
    let now=null,nowD=null;
    if(D.length===5&&Cn.length===2){const keep=bestTeams(mine,{...o,budget:1e9,inc:new Set(),exc:new Set(),n:1,w:0});
      if(keep[0]){now=keep[0].pts;nowD=keep[0].dp;}}
    const sd=v=>`${v>=0?'+':'−'}$${Math.abs(v).toFixed(1)}M`;
    const b=D.length?boosts(D,o.chip):{};
    $('tcCur').innerHTML=`<span style="color:${col};font-weight:600">${m.m}</span>'s team for ${C.name}: ${now==null?'':`<b>${now.toFixed(1)}</b> xPts, <b class="${nowD>0.04?'tl-up':nowD<-0.04?'tl-dn':''}">${sd(nowD)}</b> expected price change, `}$${fmt(m.bank)}M in the bank, ${m.free} free transfer${m.free===1?'':'s'}.`+
      `<div class="tl-codes" style="margin-top:6px">${Cn.concat(D).sort(byPts).map(a=>`<span class="tl-code ${b[a.id]||''}" style="${tc(a)}">${K(a)}${V(Math.round(a.x))}</span>`).join('')}</div>`+
      `<div style="margin-top:6px;color:#999">Green ring: the calculator's ${o.chip==='X3'?'3x and 2x picks':'2x pick'} for ${C.name}</div>`+
      (top.length&&now!=null?`<div style="margin-top:6px;color:#999">Best team below: ${top[0].pts-now>=0?'+':''}${(top[0].pts-now).toFixed(1)} xPts and ${sd(top[0].dp-nowD)} price change on keeping this one, after any transfer penalty.</div>`:'');
  } else $('tcCur').innerHTML='';
}
// Simulation menu: each stage the simulation saved for this round (F1FT-style
// "very early" before practice, then post FP1/FP2/FP3); the latest by default.
const cS=$('tcSim'), SIMS=(C&&C.sims)||[], CAavg=Object.fromEntries(PA.map(a=>[a.id,[a.ax??a.xp,a.an??a.xn]]));
function cSim(){const s=SIMS.find(x=>x.after===cS.value);
  CA.forEach(a=>{const v=s&&s.x[a.id]?s.x[a.id]:CAavg[a.id];a.x=v[0];a.n=v[1];});}
if(C){
  SIMS.slice().reverse().forEach(s=>cS.add(new Option(`${C.name} – ${SIMSTAGE[s.after]||s.after}`,s.after)));
  cS.add(new Option(`12-race average (no simulation)`,''));
  if(!SIMS.length)cS.value='';
  const cHead=()=>{$('tcHead').innerHTML=head(C.r,C.name,'Best teams for this race · '+
    (cS.value?`simulation ${SIMSTAGE[cS.value]||cS.value}`:'12-race average'));};
  cS.onchange=()=>{cSim();cHead();cAssets();runC();};
  cSim();cHead();
  $('tlCNote').innerHTML=`Ranked by expected points (xPts). Pick a manager to start from their team, budget and free transfers; extra transfers cost 10 pts each. ${SIMS.length?`xPts come from our race simulation: the weekend run 10,000 times from each car's qualifying and practice pace, how hard the track is to pass on, reliability and overtaking, scored with the F1 Fantasy rules. Pick a run under <b>Simulation</b>: <b>very early</b> is from past races only, before any practice; each later one adds that session from this weekend`:`xPts are each asset's average over its last 12 races, until the race simulation has run for this round`}${C.pricesFrom<C.r?`. Prices are today's (F1 publishes R${C.r} prices after R${C.pricesFrom})`:''}.`;
  cM.add(new Option('— none —',''));C.managers.forEach(m=>cM.add(new Option(m.m,m.m)));
  if(C.managers.some(m=>m.m==='Tim'))cM.value='Tim';
}
cSetMgr();cAssets();runC();
cM.onchange=()=>{cSetMgr();runC();};
[cF,cC,cW].forEach(e=>e.onchange=runC); cB.oninput=runC;
$('tcMore').onclick=()=>{cst.n=Math.min(50,cst.n+10);runC();};

// ── Hindsight ──────────────────────────────────────────────────────────────
const H=T.hindsight||[];
const st={inc:new Set(),exc:new Set(),n:10};
const rSel=$('tlRound'), mSel=$('tlMgr'), bIn=$('tlBudget'), cSel=$('tlChip');
H.slice().reverse().forEach(h=>rSel.add(new Option(`R${h.r} ${h.name}${h.final?'':' (live)'}`,h.r)));
function round(){return H.find(h=>h.r==rSel.value);}
function fillMgr(){const h=round();const keep=mSel.value;mSel.innerHTML='';
  mSel.add(new Option('— none —',''));(h?h.teams:[]).forEach(t=>mSel.add(new Option(t.m,t.m)));
  if([...mSel.options].some(o=>o.value===keep))mSel.value=keep;}
function mgr(){const h=round();return h&&h.teams.find(t=>t.m===mSel.value);}
function setBudget(){const m=mgr();bIn.value=(m&&m.budget?m.budget:100).toFixed(1);}
function assetsBar(){const h=round();if(!h){$('tlAssets').innerHTML='';return;}
  const sorted=h.assets.slice().sort(byPts);
  $('tlAssets').innerHTML=sorted.map((a,i)=>(i&&a.t!==sorted[i-1].t?'<div class="gap"></div>':'')+
    `<button data-id="${a.id}" class="${st.inc.has(a.id)?'inc':st.exc.has(a.id)?'exc':''}" style="${tc(a)}">${K(a)}<span class="tl-v" style="font-weight:400;opacity:.75">${a.x}</span></button>`).join('');
  $('tlAssets').querySelectorAll('button').forEach(b=>b.onclick=()=>{const id=b.dataset.id;
    if(st.inc.has(id)){st.inc.delete(id);st.exc.add(id);}else if(st.exc.has(id)){st.exc.delete(id);}else st.inc.add(id);
    assetsBar();runH();});}
function runH(){
  const h=round(); if(!h){$('tlTeams').innerHTML='<div class="tl-note">No rounds yet.</div>';return;}
  const o={budget:parseFloat(bIn.value)||100,chip:cSel.value,inc:st.inc,exc:st.exc,n:st.n};
  const top=bestTeams(h.assets,o);
  $('tlTeams').innerHTML=top.length?`<div class="tl-team" style="padding:4px 0"><div></div><div class="tl-rank">TEAM</div><div class="tl-num"><small>POINTS</small></div><div class="tl-num"><small>COST / Δ$</small></div></div>`+top.map((t,i)=>teamRow(t,i,o.chip)).join('')
    :'<div class="tl-note">No team fits that budget and those filters.</div>';
  $('tlMore').style.display=top.length>=st.n&&st.n<50?'':'none';
  const m=mgr();
  if(m&&top.length){const best=top[0].pts;const col=COL[m.m]||'#ccc';
    const byId=Object.fromEntries(h.assets.map(a=>[a.id,a]));
    const mine=m.picks.map(id=>byId[id]).filter(Boolean);
    const cost=mine.reduce((a,b)=>a+b.p,0);
    $('tlCmp').innerHTML=`<span style="color:${col};font-weight:600">${m.m}</span> scored <b>${Math.round(m.pts)}</b>${m.chip?` with ${m.chip}`:''}, team cost ${fmt(cost)}. Best possible within ${fmt(o.budget)}: <b>${Math.round(best)}</b> (${Math.round(best-m.pts)} more).<div class="tl-codes" style="margin-top:6px">${mine.slice().sort(byPts).map(a=>`<span class="tl-code ${a.id===m.mega?'x3':a.id===m.cap?'x2':''}" style="${tc(a)}">${K(a)}${V(a.x)}</span>`).join('')}</div>`;
  } else $('tlCmp').innerHTML='';
}
rSel.onchange=()=>{st.inc.clear();st.exc.clear();st.n=10;fillMgr();setBudget();assetsBar();runH();};
mSel.onchange=()=>{setBudget();runH();};
bIn.oninput=runH; cSel.onchange=runH;
$('tlMore').onclick=()=>{st.n=Math.min(50,st.n+10);runH();};
fillMgr();setBudget();assetsBar();runH();

// ── Budget builder ────────────────────────────────────────────────────────
// ── Elite Data ─────────────────────────────────────────────────────────────
const E=T.elite||[];
function eliteView(){
  const el=$('tlE');
  if(!E.length){el.innerHTML='<div class="tl-note">Elite Data starts with Mexico 2026: what the global top 500 picked, once teams lock. Nothing yet.</div>';return;}
  const sel=$('tlERound'), cur=sel?+sel.value:E[E.length-1].r, e=E.find(x=>x.r===cur)||E[E.length-1];
  const pct=v=>v==null?'—':(v>=10||v===0?Math.round(v):v.toFixed(1))+'%';
  // F1FT's order: pick % highest first, then DRS %, then alphabetical.
  const rows=t=>e.assets.filter(a=>a.t===t).sort((a,b)=>b.pk-a.pk||(b.drs||0)-(a.drs||0)||a.c.localeCompare(b.c)).map(a=>{
    const d=a.lg==null?null:a.pk-a.lg;
    return `<tr><td class="lk"><span class="tl-code" style="${tc(a)}">${K(a)}</span></td><td>${pct(a.pk)}</td><td>${pct(a.drs)}</td><td>${a.lg==null?'—':a.lg+'%'}</td>
      <td class="${d==null?'':d>=15?'tl-up':d<=-15?'tl-dn':''}">${d==null?'—':(d>0?'+':'')+Math.round(d)}</td></tr>`;}).join('');
  const head=n=>`<tr><th>${n}</th><th>Top 500</th><th>DRS</th><th>League</th><th>Diff</th></tr>`;
  // As F1FT shows them: the share of teams that played each chip this round.
  const chips=e.nd?Object.entries(e.chips).sort((a,b)=>b[1]-a[1]).map(([c,n])=>`${c} ${pct(100*n/e.nd)}`).join(' · '):'';
  el.innerHTML=`<div class="tl-note">What the global top 500 locked in for each round, against our league. Pick % comes from all ${e.n} teams as soon as teams lock. DRS % (2x or 3x) and chips need each team read one at a time, about 200 an hour, so they're complete about 3 hours after the lock${e.nd<e.n?` (<b>${e.nd} of ${e.n} read so far</b>)`:''}. "Diff" is top 500 minus league, in points: a big plus is something the best players back that nobody here does.</div>
    <div class="tl-ctl" style="grid-template-columns:1fr"><div><label>Round</label><select id="tlERound">${E.slice().reverse().map(x=>`<option value="${x.r}" ${x.r===e.r?'selected':''}>R${x.r} ${x.name}</option>`).join('')}</select></div></div>
    ${e.nd?`<div class="tl-note" style="margin-top:0"><b>Chips played this round:</b> ${chips||'none'} <span style="color:#666">(share of ${e.nd<e.n?`the ${e.nd} teams read so far`:`all ${e.n} teams`})</span></div>`:''}
    <div class="section-label">Drivers</div><table class="tl-pp">${head('Driver')}${rows('D')}</table>
    <div class="section-label">Constructors</div><table class="tl-pp">${head('Team')}${rows('C')}</table>`;
  $('tlERound').onchange=eliteView;}
eliteView();

const P=T.predictor;
function ppTable(rows,tier,label){
  if(!rows.length)return '';
  // Like F1FT: highest expected price change first. An asset at the $3M floor
  // or $34M cap can't take the full step; those bands go in a 0.0 column.
  rows=rows.slice().sort((a,b)=>b.xd-a.xd||b.p-a.p);
  const base=rows[0].p>=18.5?[-0.3,-0.1,0.1,0.3]:[-0.6,-0.2,0.2,0.6];
  const zero=rows.some(a=>a.steps.some(s=>s===0));
  const hd=zero?[base[0],base[1],0,base[2],base[3]]:base;
  const pr=P.prevRounds;
  return `<div class="section-label">${label}</div><table class="tl-pp"><tr><th>${tier}</th><th>$</th>${pr.map(r=>`<th>R${r}</th>`).join('')}
    ${hd.map(s=>`<th>${s===0?'0.0':sgn(s)}</th>`).join('')}<th>xΔ$</th></tr>`+
  rows.map(a=>{
    // Bands 0-3 have lower/upper point limits needs[i-1] / needs[i].
    const col=hd.map(()=>({p:0,lo:null,hi:null,any:false}));
    a.probs.forEach((p,i)=>{const k=hd.indexOf(a.steps[i]===0?0:base[i]);const c=col[k];
      const lo=i===0?null:a.needs[i-1], hi=i===3?null:a.needs[i];
      if(!c.any){c.lo=lo;}c.hi=hi;c.p+=p;c.any=true;});
    const best=Math.max(...col.map(c=>Math.round(c.p*100)));
    const cells=col.map(c=>{if(!c.any)return '<td></td>';
      const need=c.lo==null&&c.hi==null?'any':c.lo==null?`&lt;${Math.ceil(c.hi)}`:`${Math.ceil(c.lo)}+`;
      const pct=Math.round(c.p*100);
      return `<td class="${pct===best?'lk':''}">${pct}%<small>${need}</small></td>`;}).join('');
    return `<tr><td class="lk"><span class="tl-code" style="${tc(a)}">${K(a)}</span></td><td>${fmt(a.p)}</td>${a.prev.map(([r,x])=>`<td>${x==null?'—':Math.round(x)}</td>`).join('')}${cells}<td class="${a.xd>0.04?'tl-up':a.xd<-0.04?'tl-dn':''}">${a.xd>0?'+':''}${a.xd.toFixed(2)}</td></tr>`;}).join('')+'</table>';}
if(P){
  const A=P.assets, grp=(t,hi)=>A.filter(a=>a.t===t&&(hi?a.p>=18.5:a.p<18.5));
  $('tlP').innerHTML=head(P.r,P.name,`Price changes after this race${P.live?' · weekend under way':''}`)+`<div class="tl-note">Each percentage is the chance of that price move; underneath is the points it takes. Sorted by expected price change (xΔ$), best first; 0.0 is for an asset at the $3M floor or $34M cap. F1 moves a price by its average points per $M over the last 3 races it took part in (this one included): under 0.6, 0.6 to 0.9, 0.9 to 1.2, or 1.2 and up. Prices stay between $3M and $34M.
  <br>${P.src?`The odds come from our race simulation: each asset's points over 10,000 simulated runs of the weekend (last run ${SIMWHEN[P.src]||P.src}).`:`The odds come from each asset's last 8 races until the race simulation has run for this round.`}</div>`+
  ppTable(grp('D',true),'Driver','Drivers $18.5M+')+ppTable(grp('D',false),'Driver','Drivers under $18.5M')+
  ppTable(grp('C',true),'Team','Constructors $18.5M+')+ppTable(grp('C',false),'Team','Constructors under $18.5M');
} else $('tlP').innerHTML='<div class="tl-note">No prices published yet.</div>';
})();
</script>
"""
