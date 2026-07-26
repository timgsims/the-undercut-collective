#!/usr/bin/env python3
"""
The Undercut Collective — F1 Fantasy Dashboard Builder
=======================================================
Reads f1_data.db (populated by fetch_f1_data.py from the F1 Fantasy API)
and generates a combined HTML dashboard, then commits and pushes it to
GitHub Pages.

Run this after every race:  python build_dashboard.py
Or just double-click it in File Explorer.
"""

import sys
import os
import re
import sqlite3
import subprocess
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these if paths change
# ─────────────────────────────────────────────────────────────────────────────

# Folder where this script lives (= your GitHub repo root)
REPO_DIR = Path(__file__).parent

DB_PATH = REPO_DIR / "f1_data.db"

# Only pause for a keypress when someone's actually watching a terminal —
# a scheduled/unattended run must never block waiting for input.
INTERACTIVE = sys.stdin.isatty()

def pause(msg="\nPress Enter to exit..."):
    if INTERACTIVE:
        input(msg)

# The output HTML file (must be index.html for GitHub Pages)
OUTPUT_FILE = REPO_DIR / "index.html"

# Git settings — leave GITHUB_REMOTE blank to skip auto-push
GITHUB_REMOTE = "origin"   # set to "" to disable auto-push
COMMIT_MSG_PREFIX = "Update dashboard"  # race name appended automatically

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SEASON = 2026

MANAGER_COLOURS = {
    "Tim":     "#378ADD",
    "Stu":     "#1D9E75",
    "Jaime":   "#D85A30",
    "Grayson": "#7F77DD",
    "Cain":    "#BA7517",
    "Lori":    "#D4537E",
    "Mark":    "#888780",
    "Dan":     "#5DCAA5",
    "Mike":    "#E24B4A",
}

CHIP_STYLES = {
    "Limitless":   {"bg": "#1a3a5c", "tc": "#60aaff"},
    "Wildcard":    {"bg": "#4a1a12", "tc": "#ff7a5a"},
    "Final Fix":   {"bg": "#3d2200", "tc": "#ffaa44"},
    "Auto Pilot":  {"bg": "#0d3328", "tc": "#3ddbb0"},
    "No Negative": {"bg": "#251a4a", "tc": "#b39dff"},
    "Extra DRS":   {"bg": "#1a3010", "tc": "#88dd44"},
}

CHIP_ORDER = ["Limitless", "Wildcard", "Final Fix", "Auto Pilot", "No Negative", "Extra DRS"]

DASH_PATTERNS = [[], [6,2], [2,2], [8,3], [4,2], [6,2,2,2], [3,3], [8,2,2,2], [1,2]]

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — READ EXCEL
# ─────────────────────────────────────────────────────────────────────────────

def read_database(db_path, season=None):
    """season=None means "the most recent season present in the database" —
    the live dashboard always wants that. Passing an explicit year is how the
    future season-archive feature will pull up a past year's data instead."""
    print(f"Reading database: {db_path}")
    if not Path(db_path).exists():
        print(f"\nERROR: Database not found at:\n  {db_path}")
        print("Run fetch_f1_data.py first to create and populate it.")
        pause()
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    data = {}

    if season is None:
        row = conn.execute("SELECT MAX(season) AS s FROM races").fetchone()
        season = row["s"] if row and row["s"] is not None else None
    data["season"] = season

    races = [{"round": r["round"], "name": r["name"], "col": None}
              for r in conn.execute(
                  "SELECT round, name FROM races WHERE season=? ORDER BY round", (season,)
              )]
    race_name_by_round = {r["round"]: r["name"] for r in races}

    manager_rows = list(conn.execute("SELECT id, name, team_name FROM managers"))

    # NZ country leaderboard — a single current-snapshot stat (not per-round
    # history like global_ranks), sourced from F1's public top-500-NZ feed.
    # Managers ranked outside that top 500 simply have no row here — there's
    # no legitimate way to get their NZ rank without their own session cookie.
    nz_by_manager = {
        row["manager_id"]: {"rank": row["nz_rank"], "points": row["nz_points"]}
        for row in conn.execute(
            "SELECT manager_id, nz_rank, nz_points FROM nz_leaderboard WHERE season=?", (season,)
        )
    }

    managers_raw = []
    for mrow in manager_rows:
        mid = mrow["id"]
        scores           = {}
        global_ranks     = {}   # {race_name: {"gdrank":, "ovrank":}}
        transfers        = {}   # {race_name: count}
        inactive_penalty = {}   # {race_name: points}
        for rr in conn.execute(
            "SELECT round, points, gameday_rank, overall_rank, transfers_made, "
            "inactive_driver_penalty FROM race_results WHERE season=? AND manager_id=? ORDER BY round",
            (season, mid),
        ):
            rname = race_name_by_round.get(rr["round"])
            if not rname:
                continue
            scores[rname] = int(rr["points"])
            if rr["gameday_rank"] is not None or rr["overall_rank"] is not None:
                global_ranks[rname] = {"gdrank": rr["gameday_rank"], "ovrank": rr["overall_rank"]}
            if rr["transfers_made"] is not None:
                transfers[rname] = rr["transfers_made"]
            if rr["inactive_driver_penalty"] is not None:
                inactive_penalty[rname] = rr["inactive_driver_penalty"]

        total = sum(scores.values())
        avg   = round(total / len(scores), 1) if scores else 0

        managers_raw.append({
            "name":   mrow["name"],
            "team":   mrow["team_name"],
            "total":  total,
            "rank":   0,   # assigned below, once every manager's total is known
            "avg":    avg,
            "scores": scores,
            "global_ranks":     global_ranks,
            "transfers":        transfers,
            "inactive_penalty": inactive_penalty,
            "nz_rank":          nz_by_manager.get(mid, {}).get("rank"),
            "color":  MANAGER_COLOURS.get(mrow["name"], "#888888"),
        })

    managers_raw.sort(key=lambda m: -m["total"])
    for i, m in enumerate(managers_raw):
        m["rank"] = i + 1

    data["managers"]   = managers_raw
    data["races"]      = races
    # races_done = has ANY points data, including a race weekend that's only
    # partway through — season totals/standings are meant to track live as a
    # weekend unfolds (Tim's call), so this stays inclusive of partial data.
    data["races_done"] = [r for r in races if any(
        m["scores"].get(r["name"]) is not None for m in managers_raw)]

    # races_finalized = the subset where F1 Fantasy's own mds flag says the
    # whole gameday is scored. Anything that declares a *result* for a race
    # (podiums, race winner, finish-position stats) must only ever look at
    # this list, never races_done — a tied or partial race isn't decided yet.
    final_by_round = {
        row["round"]: bool(row["is_final"])
        for row in conn.execute(
            "SELECT round, MAX(is_final) AS is_final FROM race_results WHERE season=? GROUP BY round",
            (season,),
        )
    }
    data["races_finalized"] = [r for r in data["races_done"] if final_by_round.get(r["round"])]

    # Session-by-session status (Qualifying/Sprint Qualifying/Sprint/Race) for
    # whichever race is currently live — powers the "how far through this
    # weekend are we" detail on the live-race box.
    sessions_by_round = {}
    for row in conn.execute(
        "SELECT round, session_number, session_type, is_done FROM session_status "
        "WHERE season=? ORDER BY round, session_number", (season,),
    ):
        sessions_by_round.setdefault(row["round"], []).append(
            {"type": row["session_type"], "done": bool(row["is_done"])}
        )
    data["sessions_by_round"] = sessions_by_round

    n_finish_cols = len(managers_raw)
    data["n_finish_cols"] = n_finish_cols

    # ── Chip usage — summary flags + which race each chip was used in ────────
    manager_name_by_id = {mrow["id"]: mrow["name"] for mrow in manager_rows}
    chips_used      = {m["name"]: {} for m in managers_raw}
    chip_race_usage = {m["name"]: {} for m in managers_raw}
    for crow in conn.execute(
        "SELECT manager_id, chip_name, round_taken FROM chips_used WHERE season=?", (season,)
    ):
        mname = manager_name_by_id.get(crow["manager_id"])
        if not mname:
            continue
        chips_used[mname][crow["chip_name"]] = True
        rname = race_name_by_round.get(crow["round_taken"])
        if rname:
            chip_race_usage[mname][rname] = crow["chip_name"]
    data["chips_used"]      = chips_used
    data["chip_race_usage"] = chip_race_usage

    # ── Lineups — team_picks + players, DRS/captain flag from race_results ───
    player_names = {p["id"]: (p["name"], p["type"]) for p in
                     conn.execute("SELECT id, name, type FROM players")}
    captain_by_round_manager = {
        (rr["round"], rr["manager_id"]): rr["captain_player_id"]
        for rr in conn.execute(
            "SELECT round, manager_id, captain_player_id FROM race_results WHERE season=?", (season,)
        )
    }
    # {round: {player_id: gameday_points}} — the driver/constructor's own base
    # score that race, same for every manager who picked them (DRS doubling
    # is applied per-manager below, not baked into this shared source value).
    points_by_round_player = {}
    bud_by_round_player = {}
    value_by_round_player = {}
    raw_value_by_round_player = {}   # unshifted — fallback for the latest round, see below
    latest_overall_points = {}   # player_id -> overall_points as of the highest round seen
    latest_value          = {}   # player_id -> absolute price as of the highest round seen
    latest_overall_round  = {}   # player_id -> that round, to track "latest"
    for rr in conn.execute(
        "SELECT round, player_id, gameday_points, value_change, overall_points, value "
        "FROM player_results WHERE season=?", (season,)
    ):
        points_by_round_player.setdefault(rr["round"], {})[rr["player_id"]] = rr["gameday_points"]
        # Same one-race lag confirmed for the team-level budget fix: round N's
        # feed reflects the price move CAUSED BY round N-1, not round N itself
        # (verified: round 11's sum of value_change == Tim's known round-10
        # team budget change, exactly). File it under round N-1 so it lines
        # up with the race that actually caused it — the absolute value has
        # the same start-of-gameday-N timing, so it gets the same shift.
        bud_by_round_player.setdefault(rr["round"] - 1, {})[rr["player_id"]] = rr["value_change"]
        value_by_round_player.setdefault(rr["round"] - 1, {})[rr["player_id"]] = rr["value"]
        raw_value_by_round_player.setdefault(rr["round"], {})[rr["player_id"]] = rr["value"]

        if rr["round"] > latest_overall_round.get(rr["player_id"], -1):
            latest_overall_round[rr["player_id"]] = rr["round"]
            latest_overall_points[rr["player_id"]] = rr["overall_points"]
            latest_value[rr["player_id"]] = rr["value"]

    # Season-to-date points per driver/constructor NAME, as of the most recent
    # round we have — used to sort Team Picks' transfer/trade lists by current
    # standing rather than alphabetically.
    data["player_season_points"] = {
        player_names.get(pid, (None, None))[0]: pts
        for pid, pts in latest_overall_points.items()
        if player_names.get(pid, (None, None))[0]
    }
    # Same, but current absolute price rather than points — used to sort the
    # Lineup Viewer by current value.
    data["player_season_value"] = {
        player_names.get(pid, (None, None))[0]: val
        for pid, val in latest_value.items()
        if player_names.get(pid, (None, None))[0]
    }

    # {round: {player_id: {session_type: points}}} — used to break a race's
    # total down into Qualifying/Sprint/Race for the live-progress box.
    session_points_by_round_player = {}
    for rr in conn.execute(
        "SELECT round, player_id, session_type, points FROM player_session_points WHERE season=?", (season,)
    ):
        session_points_by_round_player.setdefault(rr["round"], {}).setdefault(
            rr["player_id"], {})[rr["session_type"]] = rr["points"]

    lineups = {m["name"]: {} for m in managers_raw}
    session_points = {m["name"]: {} for m in managers_raw}   # {race_name: {session_type: total}}
    for mrow in manager_rows:
        mid, mname = mrow["id"], mrow["name"]
        for round_no, rname in race_name_by_round.items():
            picks_rows = list(conn.execute(
                "SELECT player_id FROM team_picks WHERE season=? AND round=? AND manager_id=? "
                "ORDER BY position", (season, round_no, mid)
            ))
            if not picks_rows:
                continue
            captain_id = captain_by_round_manager.get((round_no, mid))
            round_points = points_by_round_player.get(round_no, {})
            round_value = value_by_round_player.get(round_no, {})
            round_value_raw = raw_value_by_round_player.get(round_no, {})
            round_session_points = session_points_by_round_player.get(round_no, {})
            picks = []
            race_session_totals = {}
            for prow in picks_rows:
                pid = prow["player_id"]
                pname, ptype = player_names.get(pid, (None, None))
                is_drs = (pid == captain_id)
                base_pts = round_points.get(pid)
                pts = base_pts * 2 if (is_drs and base_pts is not None) else base_pts
                # Fall back to this round's own (lagged) figure when there's no
                # next round yet to peek at — same reasoning as the team-level
                # budget fallback: mid-way through the season's most recent
                # race, the lagged value simply doesn't exist yet.
                pick_value = round_value.get(pid)
                if pick_value is None:
                    pick_value = round_value_raw.get(pid)
                picks.append({
                    "name":           pname or f"Unknown #{pid}",
                    "drs":            is_drs,
                    "drs_marker":     "2X" if is_drs else "",
                    "pts":            pts,
                    "is_constructor": ptype == "constructor",
                    "value":          pick_value,
                })
                for stype, spts in round_session_points.get(pid, {}).items():
                    if spts is None or not stype:
                        continue
                    val = spts * 2 if is_drs else spts
                    race_session_totals[stype] = race_session_totals.get(stype, 0) + val
            lineups[mname][rname] = picks
            session_points[mname][rname] = race_session_totals
    data["lineups"] = lineups
    data["session_points"] = session_points

    # driver_results/constructor_results — each player's own (non-doubled)
    # result that race, keyed by name, for the "actual points regardless of
    # who picked them" popularity view.
    driver_results, constructor_results = {}, {}
    for round_no, rname in race_name_by_round.items():
        round_bud = bud_by_round_player.get(round_no, {})
        for pid, pts in points_by_round_player.get(round_no, {}).items():
            pname, ptype = player_names.get(pid, (None, None))
            if not pname:
                continue
            target = constructor_results if ptype == "constructor" else driver_results
            target.setdefault(pname, {})[rname] = {"pts": pts, "bud": round_bud.get(pid)}
    data["driver_results"]      = driver_results
    data["constructor_results"] = constructor_results

    # ── Budget — team value per race, from race_results ───────────────────────
    budgets = {}
    for mrow in manager_rows:
        mid, mname = mrow["id"], mrow["name"]
        vals = [100.0]  # starting budget
        for race in data["races_done"]:
            row = conn.execute(
                "SELECT team_value FROM race_results WHERE season=? AND round=? AND manager_id=?",
                (season, race["round"], mid),
            ).fetchone()
            if row and row["team_value"] is not None:
                vals.append(round(row["team_value"], 2))
        budgets[mname] = vals
    data["budgets"]           = budgets
    data["budget_race_names"] = [r["name"] for r in data["races_done"]]

    # Standings-position history is left empty — compute() already falls back
    # to deriving position-per-race from cumulative scores when no explicit
    # table is present, which is exactly what we want here.
    data["pos_race_names"]      = []
    data["standings_positions"] = {}

    conn.close()
    return data
# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — COMPUTE DERIVED DATA
# ─────────────────────────────────────────────────────────────────────────────

def rank_with_ties(scored_names):
    """scored_names: [(name, pts), ...]. Returns rank groups using standard
    competition ranking — equal scores share a rank, and the next distinct
    score skips ahead by the tie's size (two tied for 1st means the next
    score is 3rd, not 2nd), the same convention an actual race podium uses.
    Return shape: [(rank, [tied names], pts), ...], highest score first."""
    ordered = sorted(scored_names, key=lambda x: -x[1])
    groups, i, rank = [], 0, 1
    while i < len(ordered):
        pts = ordered[i][1]
        tied = [n for n, p in ordered[i:] if p == pts]
        groups.append((rank, tied, pts))
        rank += len(tied)
        i += len(tied)
    return groups


def compute(data):
    managers        = data["managers"]
    races_done      = data["races_done"]
    races_finalized = data["races_finalized"]
    chips_used      = data["chips_used"]
    budgets         = data["budgets"]
    budget_races    = data["budget_race_names"]
    lineups         = data["lineups"]

    # "Complete" means actually finalized, not just live/in-progress — a race
    # that's only partway through (points included live per Tim's call on the
    # season totals) shouldn't count as done until F1 says the whole gameday
    # is scored.
    n_done = len(races_finalized)
    n_total = len(data["races"])
    finalized_names = {r["name"] for r in races_finalized}

    # Sort managers by rank for consistent ordering
    managers_sorted = sorted(managers, key=lambda m: m["rank"])

    # ── Points progression (cumulative per race) ─────────────────────────────
    # data[0] = 0 (pre-season), then cumulative after each completed race —
    # deliberately includes any live/in-progress race, so the season totals
    # and this chart track along in real time as a weekend unfolds.
    for m in managers_sorted:
        cum = [0]
        running = 0
        for race in races_done:
            pts = m["scores"].get(race["name"])
            if pts is not None:
                running += pts
            cum.append(running)
        m["cumulative"] = cum

    # ── Per-race rankings — built for every race with data (live included),
    # so "current standings for this race" can still be shown while it's in
    # progress, but tagged with is_final so nothing renders it as a decided
    # result (podium/winner) before F1 says the whole gameday is scored.
    sessions_by_round = data.get("sessions_by_round", {})
    for race in races_done:
        rname = race["name"]
        scores_this_race = [(m["name"], m["scores"].get(rname, 0)) for m in managers_sorted]
        race["rank_groups"] = rank_with_ties(scores_this_race)
        race["ranking"]     = sorted(scores_this_race, key=lambda x: -x[1])
        race["is_final"]    = rname in finalized_names
        race["winner"]      = " & ".join(race["rank_groups"][0][1]) if race["rank_groups"] else ""
        race["is_sprint"]   = any(
            "sprint" in (s.get("type") or "").lower()
            for s in sessions_by_round.get(race["round"], [])
        )

    # ── Podiums — only from finalized races. Ties share a position (e.g. two
    # managers both shown as "1st"), matching what the F1 Fantasy leaderboard
    # itself shows for a tied race, rather than us arbitrarily picking one.
    data["podiums"] = []
    for race in races_finalized:
        groups = race["rank_groups"]
        by_rank = {g[0]: g[1] for g in groups}
        data["podiums"].append({
            "race":      race["name"],
            "is_sprint": race.get("is_sprint", False),
            "first":     by_rank.get(1, []),
            "second":    by_rank.get(2, []),
            "third":     by_rank.get(3, []),
        })

    # ── Finish-position distribution — from the same finalized-race rank
    # groups as podiums, so a tied result credits every tied manager at that
    # shared rank instead of one of them getting silently bumped down (and
    # potentially dropping out of the top-3 tally entirely).
    n_finish_cols = data["n_finish_cols"]
    finish_dist = {m["name"]: [0] * n_finish_cols for m in managers_sorted}
    for race in races_finalized:
        for rank, names, _pts in race["rank_groups"]:
            if rank <= n_finish_cols:
                for name in names:
                    finish_dist[name][rank - 1] += 1
    data["finish_dist"] = finish_dist

    # ── Position history — derived from cumulative scores when not tracked ───
    pos_race_names      = data.get("pos_race_names", [])
    standings_positions = data.get("standings_positions", {})

    for m in managers_sorted:
        name = m["name"]
        raw_positions = standings_positions.get(name, [])
        positions = []
        for i, race in enumerate(races_done):
            rname = race["name"]
            # A standings position is itself a declared result for that
            # checkpoint (it can still shift before a live race actually
            # finishes) — same rule as podiums: nothing shown until final.
            if not race.get("is_final", False):
                positions.append(None)
                continue
            pos = None
            if rname in pos_race_names:
                idx = pos_race_names.index(rname)
                if idx < len(raw_positions) and raw_positions[idx] is not None:
                    pos = raw_positions[idx]
            if pos is None:
                # No explicitly-tracked position for this race (the normal
                # case now — there's no separate positions table at all) —
                # derive it from cumulative scores through this race instead
                # of leaving it blank.
                cum_scores = {mm["name"]: sum(mm["scores"].get(rd["name"], 0)
                              for rd in races_done[:i+1]) for mm in managers_sorted}
                sorted_cum = sorted(cum_scores.items(), key=lambda x: -x[1])
                pos_map = {n: j+1 for j, (n, _) in enumerate(sorted_cum)}
                pos = pos_map.get(name)
            positions.append(pos)
        m["positions"] = positions

    # ── Chip usage per manager ───────────────────────────────────────────────
    chip_race_usage = data.get("chip_race_usage", {})
    for m in managers_sorted:
        name = m["name"]
        used = chips_used.get(name, {})
        m["chips"] = {chip: used.get(chip, False) for chip in CHIP_ORDER}
        used_list = [c for c in CHIP_ORDER if m["chips"].get(c)]
        m["chip_label"] = used_list[0] if len(used_list) == 1 else (
                          f"{len(used_list)} chips" if used_list else None)
        m["chip_bg"] = CHIP_STYLES[used_list[0]]["bg"] if len(used_list) == 1 else None
        m["chip_tc"] = CHIP_STYLES[used_list[0]]["tc"] if len(used_list) == 1 else None
        # Per-race chip: {race_name: chip_name} — only races where chip was actually used
        m["chip_by_race"] = chip_race_usage.get(name, {})

    # ── Budget data ──────────────────────────────────────────────────────────
    for m in managers_sorted:
        name = m["name"]
        bvals = budgets.get(name, [100.0])
        m["budgets"] = bvals
        # bvals[0] = Pre-Season start, remaining = post-race values
        m["budget_current"] = bvals[-1] if bvals else 100.0
        m["budget_start"]   = bvals[0]  if bvals else 100.0
        m["budget_vs_start"] = round(m["budget_current"] - m["budget_start"], 2) if bvals else 0
        m["budget_last_change"] = round(bvals[-1] - bvals[-2], 2) if len(bvals) >= 2 else 0

    # ── Lineups ─────────────────────────────────────────────────────────────
    for m in managers_sorted:
        m["lineups"] = lineups.get(m["name"], {})

    # ── Stats highlights ────────────────────────────────────────────────────
    all_race_scores = [(m["name"], rname, pts)
                       for m in managers_sorted
                       for rname, pts in m["scores"].items()]

    if all_race_scores:
        best  = max(all_race_scores, key=lambda x: x[2])
        worst = min(all_race_scores, key=lambda x: x[2])
    else:
        best = worst = ("—", "—", 0)

    best_avg  = max(managers_sorted, key=lambda m: m["avg"])
    worst_avg = min(managers_sorted, key=lambda m: m["avg"])

    # Most consistent = lowest std dev of per-race finish positions (not overall
    # standings). Only counts finalized races (a live/partial race doesn't have
    # a decided finish position yet), and reuses the same tie-aware ranking
    # built above rather than re-deriving it independently.
    import statistics
    for m in managers_sorted:
        race_finishes = []
        for race in races_finalized:
            if race["name"] not in m["scores"]:
                continue
            for rank, names, _pts in race["rank_groups"]:
                if m["name"] in names:
                    race_finishes.append(rank)
                    break
        m["race_finishes"] = race_finishes

    def finish_std(m):
        valid = m.get("race_finishes", [])
        if len(valid) < 2:
            return 99
        return statistics.stdev(valid)

    most_consistent = min(managers_sorted, key=finish_std)
    pos_desc = ", ".join(f"P{p}" for p in most_consistent["race_finishes"])

    # Biggest swing = largest overall standings position change between consecutive races
    biggest_swing_val = 0
    biggest_swing_m = managers_sorted[0]
    for m in managers_sorted:
        pos = [p for p in m["positions"] if p is not None]
        for i in range(1, len(pos)):
            swing = abs(pos[i] - pos[i-1])
            if swing > biggest_swing_val:
                biggest_swing_val = swing
                biggest_swing_m = m

    # Transfer activity — straight from the API's own per-race transfer count
    # (usersubs), not derived from diffing rosters race-to-race.
    for m in managers_sorted:
        m["total_transfers"] = sum(m["transfers"].values())
    most_transfers_m = max(managers_sorted, key=lambda m: m["total_transfers"])

    # Bad luck — points lost to a pick that didn't participate that race.
    for m in managers_sorted:
        m["total_inactive_penalty"] = sum(m["inactive_penalty"].values())
    worst_luck_m = max(managers_sorted, key=lambda m: m["total_inactive_penalty"])

    data["highlights"] = {
        "best":            best,
        "worst":           worst,
        "best_avg":        best_avg,
        "worst_avg":       worst_avg,
        "most_consistent": (most_consistent, pos_desc),
        "biggest_swing":   (biggest_swing_m, biggest_swing_val),
        "most_transfers":  most_transfers_m,
        "worst_luck":      worst_luck_m,
    }

    data["managers_sorted"] = managers_sorted
    data["n_done"]  = n_done
    data["n_total"] = n_total
    data["races_done"] = races_done

    return data


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — GENERATE HTML PANELS
# ─────────────────────────────────────────────────────────────────────────────

def js(v):
    """Convert Python value to safe JS literal."""
    if v is None:     return "null"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, str):  return "'" + v.replace("'", "\\'") + "'"
    if isinstance(v, list): return "[" + ",".join(js(x) for x in v) + "]"
    if isinstance(v, dict): return "{" + ",".join(f'"{k}":{js(vv)}' for k, vv in v.items()) + "}"
    return str(v)

def chip_pill(label, bg, tc, small=False, ml="4px"):
    size = "9px" if small else "10px"
    pad  = "1px 5px" if small else "1px 7px"
    return (f'<span class="chip-pill" style="background:{bg};color:{tc};'
            f'font-size:{size};padding:{pad};margin-left:{ml};white-space:nowrap">{label}</span>')


def sprint_pill(small=False):
    size = "8px" if small else "9px"
    pad  = "1px 5px" if small else "1px 6px"
    # vertical-align:middle wasn't enough — Chart.js/browser baseline math for
    # a small badge next to much larger text still read as sitting a couple
    # px low, so nudge it up directly rather than fight the metric-dependent
    # "middle" calculation.
    return (f'<span style="background:#f0f0f0;color:#222;font-weight:600;'
            f'border-radius:4px;font-size:{size};padding:{pad};margin-left:5px;'
            f'letter-spacing:.03em;display:inline-block;position:relative;top:-2px">SPRINT</span>')


def race_label(race, small=False):
    """Race name, with a SPRINT pill appended when that weekend had a sprint
    session — reused everywhere a race name shows up as an HTML label."""
    return race["name"] + (sprint_pill(small) if race.get("is_sprint") else "")


def podium_table_html(pods, M):
    """Row-per-finisher podium table (Race | Pos | Manager | Points), shared
    by the Podiums page's Race Results and the Leaderboard's Latest Podiums
    so the two can't drift apart. Built as CSS-grid rows rather than a real
    <table> with rowspan — a rowspan'd cell only draws its border under the
    last row of its span, leaving every "continuation" row's gridline
    broken/partial under the Race/Pos columns, and table-cell borders also
    render less reliably at 0.5px on mobile than a plain div border does.
    Race/Pos text simply isn't repeated on continuation rows, giving the
    same "merged" look without relying on real cell merging for it."""
    def get_color(name):
        return MANAGER_COLOURS.get(name, "#888")

    def get_chip_pill(name, race_name):
        m = next((mm for mm in M if mm["name"] == name), None)
        if m:
            chip = m["chip_by_race"].get(race_name)
            if chip and chip in CHIP_STYLES:
                return chip_pill(chip, CHIP_STYLES[chip]["bg"], CHIP_STYLES[chip]["tc"], small=True)
        return ""

    medal_colors = {"1st": "#FFD700", "2nd": "#C0C0C0", "3rd": "#CD7F32"}
    pod_grid = "90px 40px minmax(100px,1fr) 64px"
    pod_min_w = 90 + 40 + 100 + 64 + 10 * 3

    all_rows = []   # [(race_cell, pos_cell, name_cell, pts_txt, filled)]
    for pod in pods:
        filled = bool(pod["first"] or pod["second"] or pod["third"])
        entries = []
        for label, names in (("1st", pod["first"]), ("2nd", pod["second"]), ("3rd", pod["third"])):
            entries.extend((label, name) for name in names) if names else entries.append((label, None))

        race_label_html = pod["race"] + (sprint_pill(small=True) if pod.get("is_sprint") else "")
        race_shown = False
        i = 0
        while i < len(entries):
            label = entries[i][0]
            j = i
            while j < len(entries) and entries[j][0] == label:
                j += 1
            for k in range(i, j):
                _, name = entries[k]
                race_cell = race_label_html if not race_shown else ""
                race_shown = True
                pos_cell = (f'<span style="color:{medal_colors[label]};font-weight:600">{label}</span>'
                            if k == i else "")
                if name is None:
                    all_rows.append((race_cell, pos_cell, '<span style="color:#555">TBC</span>', "", filled))
                else:
                    pts = next((m["scores"].get(pod["race"], "") for m in M if m["name"] == name), "")
                    pts_txt = f'{pts} pts' if pts != "" else "—"
                    pill = get_chip_pill(name, pod["race"])
                    name_cell = f'<span style="color:{get_color(name)};font-weight:500">{name}{pill}</span>'
                    all_rows.append((race_cell, pos_cell, name_cell, pts_txt, filled))
            i = j

    pod_rows = ""
    for idx, (race_cell, pos_cell, name_cell, pts_txt, filled) in enumerate(all_rows):
        border = "" if idx == len(all_rows) - 1 else "border-bottom:0.5px solid #2a2a2a;"
        opacity = "opacity:0.5;" if not filled else ""
        pod_rows += (
            f'<div style="display:grid;grid-template-columns:{pod_grid};gap:10px;align-items:center;'
            f'padding:8px 0;{border}{opacity}min-width:{pod_min_w}px">'
            f'<div style="color:#888;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{race_cell}</div>'
            f'<div style="font-size:13px">{pos_cell}</div>'
            f'<div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{name_cell}</div>'
            f'<div style="text-align:right;color:#888;font-size:12px;white-space:nowrap">{pts_txt}</div>'
            f'</div>'
        )

    pod_header = (
        f'<div style="display:grid;grid-template-columns:{pod_grid};gap:10px;padding:4px 0 8px;min-width:{pod_min_w}px">'
        f'<div style="font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.04em">Race</div>'
        f'<div style="font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.04em">Pos</div>'
        f'<div style="font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.04em">Manager</div>'
        f'<div style="font-size:9px;color:#555;text-align:right;text-transform:uppercase;letter-spacing:.04em">Points</div>'
        f'</div>'
    )

    return f"""<div class="card" style="padding:4px 16px;overflow-x:auto;-webkit-overflow-scrolling:touch">
{pod_header}
{pod_rows}
</div>"""


def _global_standing_section(data):
    """Current global-rank standing table — how the league stacks up against
    every F1 Fantasy player worldwide. Lives at the bottom of the Leaderboard
    page rather than its own tab."""
    M = data["managers_sorted"]

    # Approximate registered-player counts, as displayed in F1 Fantasy's own
    # UI (the API exposes neither figure directly — confirmed via HAR review
    # — so these are "+"-qualified approximations Tim read off the leaderboard
    # page itself, not exact counts). Percentiles below are approximate too.
    TOTAL_GLOBAL_PLAYERS = 2_100_000
    TOTAL_NZ_PLAYERS     = 27_500

    def percentile(rank, total):
        if not total or not rank:
            return None
        return round(rank / total * 100, 2)

    rows = []
    for m in M:
        gr = m.get("global_ranks", {})
        cur_ov = next((v["ovrank"] for v in reversed(list(gr.values())) if v.get("ovrank") is not None), None)
        gd_entries = [(rname, v["gdrank"]) for rname, v in gr.items() if v.get("gdrank") is not None]
        best_gd = min(gd_entries, key=lambda x: x[1]) if gd_entries else None
        rows.append({"m": m, "cur_ov": cur_ov, "best_gd": best_gd})

    rows.sort(key=lambda r: (r["cur_ov"] if r["cur_ov"] is not None else 10**9))

    def fmt_rank(n):
        return f"{n:,}" if n is not None else "—"

    def fmt_total(n):
        if n >= 1_000_000:
            return f"{n/1_000_000:g}m+"
        if n >= 1_000:
            return f"{n/1_000:g}k+"
        return f"{n}+"

    def rank_pct_html(rank, total):
        """Rank right-aligned with its approximate percentile as small grey
        text alongside it — same two-sub-column pattern as the Best Single
        Race cell, so the gap (not the text block) sits on the column center."""
        if rank is None:
            return '<div style="text-align:center">—</div>'
        pct = percentile(rank, total)
        pct_html = f'<div style="text-align:left;color:#555;font-size:11px;align-self:center">(top {pct}%)</div>' if pct is not None else ""
        return (
            f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">'
            f'<div style="text-align:right">{fmt_rank(rank)}</div>'
            f'{pct_html}'
            f'</div>'
        )

    # Dedicated grid template — deliberately not reusing Budget Tracker's
    # .brow/.bcol classes, which are hardcoded to narrow (52-60px) columns
    # sized for short currency figures, not longer rank numbers.
    grid_cols = "26px minmax(70px,1fr) minmax(120px,1fr) minmax(110px,1fr) minmax(140px,1fr)"

    rows_html = ""
    for r in rows:
        m = r["m"]
        # NZ rank comes from F1's public top-500-NZ snapshot feed, not a
        # per-round field — a manager ranked below 500th nationally has no
        # row at all, hence "—" here rather than a fabricated number.
        nz_html = rank_pct_html(m.get("nz_rank"), TOTAL_NZ_PLAYERS)
        cur_ov_html = rank_pct_html(r["cur_ov"], TOTAL_GLOBAL_PLAYERS)
        # Split into two sub-columns (rank right-aligned, race name left-
        # aligned) so the gap between them — not the text block as a whole —
        # sits on the column's true center line.
        if r["best_gd"]:
            best_gd_html = (
                f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">'
                f'<div style="text-align:right">{fmt_rank(r["best_gd"][1])}</div>'
                f'<div style="text-align:left;color:#555;font-size:11px;align-self:center">({r["best_gd"][0]})</div>'
                f'</div>'
            )
        else:
            best_gd_html = '<div style="text-align:center">—</div>'
        rows_html += f"""<div style="display:grid;grid-template-columns:{grid_cols};align-items:center;gap:10px;padding:9px 0;border-bottom:0.5px solid #2a2a2a">
  <div style="display:flex;align-items:center;justify-content:center"><div class="dot" style="background:{m['color']}"></div></div>
  <div style="font-size:13px;font-weight:500">{m['name']}</div>
  <div style="font-size:13px;font-weight:500">{cur_ov_html}</div>
  <div style="font-size:13px;font-weight:500">{nz_html}</div>
  <div style="font-size:13px">{best_gd_html}</div>
</div>"""

    return f"""<div class="section-label">Global standing</div>
<div class="hint" style="margin-bottom:.5rem">How the league stacks up against every F1 Fantasy player worldwide. NZ rank only appears for players inside F1 Fantasy's public New Zealand top 500 — a "—" means ranked lower than that, not unranked.</div>
<div class="card" style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <div style="min-width:520px">
  <div style="display:grid;grid-template-columns:{grid_cols};gap:10px;padding:4px 0 8px;align-items:end">
    <div></div><div style="font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.04em">Player</div><div style="font-size:9px;color:#555;text-align:center">Overall rank<br><span style="color:#444">out of {fmt_total(TOTAL_GLOBAL_PLAYERS)}</span></div><div style="font-size:9px;color:#555;text-align:center">NZ rank<br><span style="color:#444">out of {fmt_total(TOTAL_NZ_PLAYERS)}</span></div><div style="font-size:9px;color:#555;text-align:center">Best single race</div>
  </div>
  {rows_html}
  </div>
</div>
<div class="hint">Percentiles are approximate — F1 Fantasy shows "{fmt_total(TOTAL_GLOBAL_PLAYERS)}" global and "{fmt_total(TOTAL_NZ_PLAYERS)}" NZ players rather than an exact count.</div>"""


def _weekend_summary_data(data, races):
    """Build the {race_name: {...}} structure behind the weekend points
    summary table — used by both the Leaderboard's live box and the Race
    Breakdown archive dropdown, so the two can never disagree."""
    M = data["managers_sorted"]
    sp = data.get("session_points", {})
    sessions_by_round = data.get("sessions_by_round", {})
    result = {}
    for race in races:
        rname = race["name"]
        sessions = sessions_by_round.get(race["round"], [])
        session_types = [s["type"] for s in sessions if s.get("type")]
        # F1's own drivers feed doesn't expose Sprint Race points as a
        # separate line item on a sprint weekend (confirmed against the live
        # API: only Sprint Qualifying/Qualifying/Race appear, and they sum
        # exactly to the known total) — so "Race" there is genuinely Sprint
        # Race + Main Race combined. Label it honestly rather than implying
        # a breakdown that doesn't exist upstream.
        is_sprint_weekend = "Sprint Qualifying" in session_types
        session_labels = [
            "Sprint + Race" if (is_sprint_weekend and st == "Race") else st
            for st in session_types
        ]
        rows = []
        for m in M:
            if rname not in m["scores"]:
                continue
            race_sp = sp.get(m["name"], {}).get(rname, {})
            rows.append({
                "name":     m["name"],
                "color":    m["color"],
                "sessions": [race_sp.get(st) for st in session_types],
                "total":    m["scores"].get(rname),
            })
        rows.sort(key=lambda r: -(r["total"] if r["total"] is not None else -1))
        result[rname] = {
            "isFinal":       race.get("is_final", False),
            "sessionTypes":  session_types,
            "sessionLabels": session_labels,
            "sessions":      [{"type": s.get("type"), "done": s.get("done", False)} for s in sessions],
            "rows":          rows,
        }
    return result


# ── 01 Leaderboard ───────────────────────────────────────────────────────────
def panel_leaderboard(data):
    M   = data["managers_sorted"]
    RD  = data["races_done"]
    NT  = data["n_total"]
    ND  = data["n_done"]

    leader = M[0]["total"] if M else 0
    last   = M[-1]["total"] if M else 0
    gap_span = leader - last

    # Latest-race box — season totals already include live partial points
    # (per Tim's call), but there's nowhere else showing THIS race's own
    # session-by-session breakdown. Always shows the most recent race, live
    # or not — once it's finalized the header just switches to a "completed"
    # summary and stays until the next race weekend gets any data at all.
    # Same source (_weekend_summary_data) as the Race Breakdown archive
    # dropdown, so the two can never disagree.
    latest_race = max(RD, key=lambda r: r["round"]) if RD else None
    live_box = ""
    if latest_race:
        rname = latest_race["name"]
        d = _weekend_summary_data(data, [latest_race])[rname]
        header_text = (f"{race_label(latest_race)} completed weekend points summary" if d["isFinal"]
                       else f"{race_label(latest_race)} weekend in progress")

        session_html = ""
        if d["sessions"]:
            pills = "".join(
                f'<span style="font-size:10px;padding:2px 8px;border-radius:10px;'
                f'background:{"#1a3010" if s["done"] else "#2a2a2a"};'
                f'color:{"#88dd44" if s["done"] else "#888"}">'
                f'{"✓" if s["done"] else "…"} {s["type"]}</span>'
                for s in d["sessions"]
            )
            session_html = f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">{pills}</div>'

        grid_cols = "minmax(70px,1fr) " + " ".join(["64px"] * len(d["sessionTypes"])) + " 64px"

        table_header = (
            f'<div style="display:grid;grid-template-columns:{grid_cols};gap:8px;padding:4px 0 6px;border-bottom:0.5px solid #2a2a2a">'
            f'<div style="font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.04em">Manager</div>'
            + "".join(f'<div style="font-size:9px;color:#555;text-align:right;white-space:nowrap">{st}</div>' for st in d["sessionLabels"])
            + '<div style="font-size:9px;color:#555;text-align:right">Total</div>'
            + '</div>'
        )

        table_rows = ""
        for row in d["rows"]:
            cells = "".join(
                f'<div style="text-align:right;font-size:13px">{v if v is not None else "—"}</div>'
                for v in row["sessions"]
            )
            table_rows += (
                f'<div style="display:grid;grid-template-columns:{grid_cols};gap:8px;align-items:center;padding:7px 0;border-bottom:0.5px solid #2a2a2a">'
                f'<div style="font-size:13px;font-weight:500;color:{row["color"]}">{row["name"]}</div>'
                f'{cells}'
                f'<div style="text-align:right;font-size:13px;font-weight:600">{row["total"] if row["total"] is not None else "—"}</div>'
                f'</div>'
            )

        box_border = "1px solid #ffaa44" if not d["isFinal"] else "0.5px solid #2a2a2a"
        title_color = "#ffaa44" if not d["isFinal"] else "#eee"

        live_box = f"""<div class="card" style="border:{box_border};margin-bottom:1rem;padding-top:16px">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
    <span style="font-size:18px">🏁</span>
    <span style="font-weight:600;color:{title_color}">{header_text}</span>
  </div>
  {session_html}
  {table_header}
  {table_rows}
</div>"""

    # Standings rows
    medal_styles = [
        "background:#FFD700;color:#7a5800",
        "background:#C0C0C0;color:#4a4a4a",
        "background:#CD7F32;color:#5a2d00",
    ]
    rows_html = ""
    for i, m in enumerate(M):
        pct   = round(m["total"] / leader * 100, 1) if leader else 0
        medal = medal_styles[i] if i < 3 else "background:#2a2a2a;color:#888"
        gap   = "Leader" if i == 0 else str(M[i]["total"] - M[0]["total"])
        rows_html += f"""<div class="row">
  <div class="pos-badge" style="{medal}">{i+1}</div>
  <div><div class="manager-name">{m['name']}</div><div class="team-name">{m['team']}</div>
  <div class="bar-bg"><div class="bar-fill" style="width:{pct}%;background:{m['color']}"></div></div></div>
  <div class="gap-col">{gap}</div>
  <div class="pts-col">{m['total']}</div>
</div>"""

    # Progress chart
    labels  = ["Pre-season"] + [r["name"] for r in RD]
    datasets = []
    for i, m in enumerate(M):
        dash = DASH_PATTERNS[i % len(DASH_PATTERNS)]
        datasets.append({
            "label": m["name"], "data": m["cumulative"],
            "borderColor": m["color"], "borderDash": dash,
            "borderWidth": 2, "pointBackgroundColor": m["color"],
            "pointRadius": 4, "pointHoverRadius": 6,
            "fill": False, "tension": 0.2
        })

    legend_html = "".join(
        f'<span><span style="width:10px;height:10px;border-radius:2px;background:{m["color"]};display:inline-block"></span>{m["name"]}</span>'
        for m in M)

    html = f"""<div class="subtitle">F1 Fantasy League — Season Leaderboard · {ND} of {NT} races complete</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{ND}</div><div class="mc-lbl">Races complete</div></div>
  <div class="mc"><div class="mc-val">{NT - ND}</div><div class="mc-lbl">Races remaining</div></div>
  <div class="mc"><div class="mc-val">{leader}</div><div class="mc-lbl">Pts — leader</div></div>
  <div class="mc"><div class="mc-val">{gap_span}</div><div class="mc-lbl">Pts gap P1 to P{len(M)}</div></div>
</div>
{live_box}
<div class="section-label">Standings</div>
<div class="card">{rows_html}</div>
<div class="hint">Bar shows points as % of leader's total ({leader} pts)</div>
<div class="section-label">Points progression</div>
<div style="position:relative;height:280px"><canvas id="progressChart"></canvas></div>
<div class="legend" id="legend-leaderboard"></div>
<script>
(function(){{
const datasets={js(datasets)};
new Chart(document.getElementById('progressChart'),{{
  type:'line',
  data:{{labels:{js(labels)},datasets:datasets}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} pts`}}}}}},
    scales:{{x:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
             y:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}}}}}}
}});
const leg=document.getElementById('legend-leaderboard');
leg.innerHTML={js(legend_html)};
}})();
</script>"""
    return html + _global_standing_section(data)


# ── 02 Race Breakdown ─────────────────────────────────────────────────────────
def panel_race_breakdown(data):
    M   = data["managers_sorted"]
    RD  = data["races_done"]
    ND  = data["n_done"]
    NT  = data["n_total"]

    all_scores = [pts for m in M for pts in m["scores"].values()]
    high = max(all_scores) if all_scores else 0
    low  = min(all_scores) if all_scores else 0
    avg  = round(sum(all_scores) / len(all_scores)) if all_scores else 0

    high_m = next((f"{m['name']}, R{i+1}" for i, race in enumerate(RD)
                   for m in M if m["scores"].get(race["name"]) == high), "")

    medal_styles = ["background:#FFD700;color:#7a5800",
                    "background:#C0C0C0;color:#4a4a4a",
                    "background:#CD7F32;color:#5a2d00"]
    slot_styles  = ["background:#2a2200;border:0.5px solid #FFD700",
                    "background:#222;border:0.5px solid #C0C0C0",
                    "background:#221a12;border:0.5px solid #CD7F32"]
    ordinals     = ["1st","2nd","3rd","4th","5th","6th","7th","8th","9th"]

    by_name = {m["name"]: m for m in M}

    race_cards_html = ""
    for race in reversed(RD):
        rname = race["name"]
        rnd   = race["round"]
        is_final = race.get("is_final", False)
        # Flatten the shared tie-aware rank groups back into (rank, manager, pts)
        # rows — same source panel_leaderboard's podiums use, so a tie can never
        # show differently between the two pages.
        scores_here = []
        for rank, names, pts in race.get("rank_groups", []):
            for nm in names:
                scores_here.append((rank, by_name[nm], pts))
        max_pts = scores_here[0][2] if scores_here else 1

        # A definitive medal podium only makes sense once the race is finalized
        # — while it's still live, we show the same ranked list further down
        # without declaring gold/silver/bronze.
        podium_html = ""
        if is_final:
            for rank, mm, pts in scores_here:
                if rank > 3:
                    break
                style_idx = rank - 1
                podium_html += (f'<div class="slot" style="{slot_styles[style_idx]}">'
                                f'<div class="medal" style="{medal_styles[style_idx]}">{rank}</div>'
                                f'<div><div class="slot-name" style="color:{mm["color"]}">{mm["name"]}</div>'
                                f'<div class="slot-pts">{pts} pts</div></div></div>')

        score_rows = ""
        for rank, mm, pts in scores_here:
            pct = round(max(0, pts) / max_pts * 100) if max_pts > 0 else 0
            if is_final and rank <= 3:
                bg, tc = ["#FFD700","#C0C0C0","#CD7F32"][rank-1], ["#7a5800","#4a4a4a","#5a2d00"][rank-1]
            else:
                bg, tc = "#2a2a2a", "#888"
            race_chip = mm["chip_by_race"].get(rname)
            if race_chip and race_chip in CHIP_STYLES:
                pill = chip_pill(race_chip, CHIP_STYLES[race_chip]["bg"], CHIP_STYLES[race_chip]["tc"], small=True)
            else:
                pill = ""
            score_rows += (f'<div class="score-row">'
                           f'<div class="pos-dot" style="background:{bg};color:{tc}">{rank}</div>'
                           f'<div><div class="score-name">{mm["name"]}{pill}</div>'
                           f'<div class="bar-bg"><div class="bar-fill" style="width:{pct}%;background:{mm["color"]}"></div></div></div>'
                           f'<div class="score-pts">{pts}</div></div>')

        all_pts   = [pts for _, _, pts in scores_here]
        rng       = max(all_pts) - min(all_pts) if all_pts else 0
        r_avg     = round(sum(all_pts) / len(all_pts)) if all_pts else 0
        negatives = sum(1 for s in all_pts if s < 0)
        neg_note  = f"{negatives} went negative" if negatives else "Everyone positive"
        live_tag  = '' if is_final else '<span class="tag" style="background:#3a2a00;color:#ffaa44">Live — not final</span>'

        race_cards_html += f"""<div class="race-card">
  <div class="race-header"><span class="race-title">{race_label(race)}</span><span class="race-round">Round {rnd}</span></div>
  <div class="podium-strip">{podium_html}</div>
  {score_rows}
  <div><span class="tag">Range: {rng} pts</span><span class="tag">Avg: {r_avg} pts</span><span class="tag">{neg_note}</span>{live_tag}</div>
</div>"""

    # Grouped bar chart data
    bar_datasets = []
    for m in M:
        race_pts = [m["scores"].get(r["name"], 0) for r in RD]
        bar_datasets.append({"label": m["name"], "data": race_pts,
                              "backgroundColor": m["color"], "borderWidth": 0})
    race_labels = [r["name"] for r in RD]
    legend_html = "".join(
        f'<span><span style="width:10px;height:10px;border-radius:2px;background:{m["color"]};display:inline-block"></span>{m["name"]}</span>'
        for m in M)

    # ── Weekend points summary archive — same Qualifying/Sprint/Race/Total
    # table as the Leaderboard's live box, but selectable for any race that
    # has data, live or finalized.
    weekend_by_race = _weekend_summary_data(data, RD)
    latest_race = max(RD, key=lambda r: r["round"]) if RD else None
    weekend_race_opts = "".join(
        f'<option value="{r["name"]}"{" selected" if r is latest_race else ""}>{r["name"]}</option>'
        for r in RD
    )

    return f"""<div class="subtitle">Race Breakdown · {ND} of {NT} races complete</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{ND}</div><div class="mc-lbl">Races complete</div></div>
  <div class="mc"><div class="mc-val">{high}</div><div class="mc-lbl">Highest single race ({high_m})</div></div>
  <div class="mc"><div class="mc-val">{low}</div><div class="mc-lbl">Lowest single race</div></div>
  <div class="mc"><div class="mc-val">{avg}</div><div class="mc-lbl">Avg points per race</div></div>
</div>
<div class="section-label">Weekend points summary</div>
<div style="display:flex;gap:12px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
  <label style="font-size:13px">Race</label>
  <select id="weekendRaceSel">{weekend_race_opts}</select>
</div>
<div id="weekend-summary-display"></div>
<script>
(function(){{
const weekendByRace={js(weekend_by_race)};
const weekendSel=document.getElementById('weekendRaceSel');
function renderWeekendSummary(rname){{
  const d=weekendByRace[rname];
  const el=document.getElementById('weekend-summary-display');
  if(!d){{el.innerHTML='';return;}}
  const headerText=d.isFinal?(rname+' completed weekend points summary'):(rname+' weekend in progress');
  const boxBorder=d.isFinal?'0.5px solid #2a2a2a':'1px solid #ffaa44';
  const titleColor=d.isFinal?'#eee':'#ffaa44';
  const pills=d.sessions.map(s=>`<span style="font-size:10px;padding:2px 8px;border-radius:10px;background:${{s.done?'#1a3010':'#2a2a2a'}};color:${{s.done?'#88dd44':'#888'}}">${{s.done?'✓':'…'}} ${{s.type}}</span>`).join('');
  const gridCols='minmax(70px,1fr) '+d.sessionTypes.map(()=>'64px').join(' ')+' 64px';
  const headHtml=`<div style="display:grid;grid-template-columns:${{gridCols}};gap:8px;padding:4px 0 6px;border-bottom:0.5px solid #2a2a2a">
    <div style="font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.04em">Manager</div>
    ${{d.sessionLabels.map(st=>`<div style="font-size:9px;color:#555;text-align:right;white-space:nowrap">${{st}}</div>`).join('')}}
    <div style="font-size:9px;color:#555;text-align:right">Total</div>
  </div>`;
  const rowsHtml=d.rows.map(r=>`<div style="display:grid;grid-template-columns:${{gridCols}};gap:8px;align-items:center;padding:7px 0;border-bottom:0.5px solid #2a2a2a">
    <div style="font-size:13px;font-weight:500;color:${{r.color}}">${{r.name}}</div>
    ${{r.sessions.map(v=>`<div style="text-align:right;font-size:13px">${{v===null?'—':v}}</div>`).join('')}}
    <div style="text-align:right;font-size:13px;font-weight:600">${{r.total===null?'—':r.total}}</div>
  </div>`).join('');
  el.innerHTML=`<div class="card" style="border:${{boxBorder}};padding-top:16px">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
      <span style="font-size:18px">🏁</span>
      <span style="font-weight:600;color:${{titleColor}}">${{headerText}}</span>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">${{pills}}</div>
    ${{headHtml}}${{rowsHtml}}
  </div>`;
}}
weekendSel.addEventListener('change',function(){{renderWeekendSummary(this.value);}});
renderWeekendSummary(weekendSel.value);
}})();
</script>
<div class="section-label">Points per race — all managers</div>
<div style="position:relative;height:300px">
  <div id="barChartYAxis" style="position:absolute;top:0;left:0;width:48px;height:100%;z-index:2;background:#0f0f0f;pointer-events:none"></div>
  <div id="barChartScroll" style="overflow-x:auto;-webkit-overflow-scrolling:touch;height:100%;padding-left:48px;box-sizing:border-box">
    <div style="position:relative;height:100%;min-width:{max(len(race_labels)*90, 400)}px">
      <canvas id="barChart"></canvas>
    </div>
  </div>
</div>
<div class="legend" id="legend-race"></div>
<script>
(function(){{
const nRaces={len(race_labels)};
const chart=new Chart(document.getElementById('barChart'),{{
  type:'bar',
  data:{{labels:{js(race_labels)},datasets:{js(bar_datasets)}}},
  options:{{responsive:true,maintainAspectRatio:false,
    layout:{{padding:{{top:10,left:0}}}},
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} pts`}}}}}},
    scales:{{
      x:{{ticks:{{color:'#888',maxRotation:30}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
      y:{{ticks:{{color:'#888',display:false}},grid:{{color:'rgba(255,255,255,0.06)'}},border:{{display:false}}}}
    }}}}
}});
function freezeYAxis(){{
  const yAxis=document.getElementById('barChartYAxis');
  const cvs=document.getElementById('barChart');
  const scale=chart.scales.y;
  if(!scale)return;
  const dpr=window.devicePixelRatio||1;
  const cssH=cvs.clientHeight||300;
  const fc=document.createElement('canvas');
  fc.width=48*dpr; fc.height=cssH*dpr;
  fc.style.width='48px'; fc.style.height=cssH+'px';
  const ctx2=fc.getContext('2d');
  ctx2.scale(dpr,dpr);
  ctx2.fillStyle='#0f0f0f';
  ctx2.fillRect(0,0,48,cssH);
  const ticks=scale.ticks;
  const top=scale.top; const bottom=scale.bottom;
  const range=bottom-top;
  const vMin=scale.min; const vMax=scale.max;
  ticks.forEach(t=>{{
    const v=t.value;
    const yPx=top+((vMax-v)/(vMax-vMin))*range;
    ctx2.fillStyle='#888';
    ctx2.font='11px -apple-system,BlinkMacSystemFont,sans-serif';
    ctx2.textAlign='right';
    ctx2.fillText(t.label,44,yPx+4);
  }});
  yAxis.innerHTML='';
  yAxis.appendChild(fc);
}}
chart.options.animation={{onComplete:freezeYAxis}};
chart.update();
document.getElementById('legend-race').innerHTML={js(legend_html)};
}})();
</script>
<div class="section-label">Round by round</div>
{race_cards_html}"""


# ── 03 Head-to-Head ───────────────────────────────────────────────────────────
def panel_h2h(data):
    M  = data["managers_sorted"]
    RD = data["races_done"]

    manager_data_js = {}
    for m in M:
        manager_data_js[m["name"]] = {
            "team":      m["team"],
            "color":     m["color"],
            "pos":       m["rank"],
            "total":     m["total"],
            "races":     [m["scores"].get(r["name"], 0) for r in RD],
            "podiums":   [1 if any(
                             pod["race"] == r["name"] and
                             m["name"] in pod[pos_key]
                             for pod in data["podiums"]
                             for pos_key in ["first","second","third"]
                           ) else 0
                          for r in RD],
            "wins":      [1 if any(
                             pod["race"] == r["name"] and m["name"] in pod["first"]
                             for pod in data["podiums"]
                           ) else 0
                          for r in RD],
            "chips":     {c: m["chips"].get(c, False) for c in CHIP_ORDER},
            "chipLabel":   m["chip_label"],
            "chipBg":      m["chip_bg"],
            "chipTc":      m["chip_tc"],
            "chipByRace":  m["chip_by_race"],   # {race_name: chip_name}
        }

    names_js = js(list(manager_data_js.keys()))
    data_js  = js(manager_data_js)
    chips_js = js([{"key": c, "label": c,
                    "bg": CHIP_STYLES[c]["bg"], "tc": CHIP_STYLES[c]["tc"],
                    "desc": {"Limitless":"Unlimited transfers","Wildcard":"Unlimited changes",
                             "Final Fix":"Post-quali changes","Auto Pilot":"Auto DRS boost",
                             "No Negative":"No negative pts","Extra DRS":"3x pts booster"}[c]}
                   for c in CHIP_ORDER])
    race_names_js = js([r["name"] for r in RD])

    return f"""<div class="subtitle">Head-to-Head Comparison</div>
<div style="display:flex;gap:12px;align-items:center;margin-bottom:1.5rem;flex-wrap:wrap">
  <div style="display:flex;align-items:center;gap:8px"><label>Manager A</label><select id="selA" onchange="ucH2HSync('A')"></select></div>
  <div style="font-size:13px;color:#888;font-weight:500">vs</div>
  <div style="display:flex;align-items:center;gap:8px"><label>Manager B</label><select id="selB" onchange="ucH2HSync('B')"></select></div>
</div>
<div id="vs-header" class="vs-header"></div>
<div class="section-label" style="margin-top:0">Season stats</div>
<div class="stat-grid" id="stat-grid"></div>
<div class="section-label">Chip usage</div>
<div class="card" id="chip-table"></div>
<div class="section-label">Race by race</div>
<div class="card" id="race-rows"></div>
<div class="section-label">Points progression</div>
<div style="position:relative;height:220px"><canvas id="h2hChart"></canvas></div>
<script>
(function(){{
const chips={chips_js};
const chipStyles={js({c: {"bg": CHIP_STYLES[c]["bg"], "tc": CHIP_STYLES[c]["tc"]} for c in CHIP_ORDER})};
const mgrs={data_js};
const raceNames={race_names_js};
const names={names_js};
let chart=null;
const selA=document.getElementById('selA'),selB=document.getElementById('selB');
function populateSelect(sel,exclude,cur){{
  const prev=cur||sel.value;sel.innerHTML='';
  names.filter(n=>n!==exclude).forEach(n=>{{const o=document.createElement('option');o.value=n;o.textContent=n;if(n===prev)o.selected=true;sel.appendChild(o);}});
  if(!sel.value)sel.value=sel.options[0].value;
}}
function syncSelects(changed){{if(changed==='A')populateSelect(selB,selA.value,selB.value);else populateSelect(selA,selB.value,selA.value);render();}}
window.ucH2HSync=syncSelects;
populateSelect(selA,null,names[0]);
if(names.length>1)populateSelect(selB,names[0],names[1]);
else populateSelect(selB,null,names[0]);
function tickSvg(used,color){{
  if(used)return`<div class="tick" style="background:${{color}}22"><svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M2 6l3 3 5-5" stroke="${{color}}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></div>`;
  return`<div class="tick" style="background:#1e1e1e"><svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M4 4l4 4M8 4l-4 4" stroke="#555" stroke-width="1.5" stroke-linecap="round"/></svg></div>`;
}}
function raceChipPill(mgr, raceName){{
  const label=mgr.chipByRace&&mgr.chipByRace[raceName];
  if(!label)return'';
  const st=chipStyles[label]||{{bg:'#222',tc:'#aaa'}};
  return`<span style="display:inline-block;font-size:9px;padding:1px 5px;border-radius:20px;font-weight:500;margin-left:4px;background:${{st.bg}};color:${{st.tc}}">${{label}}</span>`;
}}
function render(){{
  const nA=selA.value,nB=selB.value,A=mgrs[nA],B=mgrs[nB];
  const aRW=A.wins.reduce((s,v)=>s+v,0),bRW=B.wins.reduce((s,v)=>s+v,0);
  const aP=A.podiums.reduce((s,v)=>s+v,0),bP=B.podiums.reduce((s,v)=>s+v,0);
  const aL=A.total>=B.total;
  document.getElementById('vs-header').innerHTML=`
    <div class="vs-card${{aL?' winner':''}}" style="${{aL?`border-color:${{A.color}}`:''}}">
      <div class="vs-name" style="color:${{A.color}}">${{nA}}</div><div class="vs-team">${{A.team}}</div>
      <div class="vs-pts" style="color:${{A.color}}">${{A.total}}</div><div class="vs-pos">P${{A.pos}} overall</div></div>
    <div class="vs-divider">vs</div>
    <div class="vs-card${{!aL?' winner':''}}" style="${{!aL?`border-color:${{B.color}}`:''}}">
      <div class="vs-name" style="color:${{B.color}}">${{nB}}</div><div class="vs-team">${{B.team}}</div>
      <div class="vs-pts" style="color:${{B.color}}">${{B.total}}</div><div class="vs-pos">P${{B.pos}} overall</div></div>`;
  const rows=[[A.total,'Total pts',B.total],[Math.round(A.total/Math.max(1,raceNames.length)),'Avg per race',Math.round(B.total/Math.max(1,raceNames.length))],[Math.max(...A.races),'Best race',Math.max(...B.races)],[Math.min(...A.races),'Worst race',Math.min(...B.races)],[aRW,'Race wins',bRW],[aP,'Podiums',bP]];
  document.getElementById('stat-grid').innerHTML=rows.map(([av,lbl,bv])=>{{
    const aH=av>bv,bH=bv>av;
    return`<div class="stat-val${{aH?' hi':''}}" style="${{aH?`border-color:${{A.color}}`:''}}">  ${{av}}</div><div class="stat-label">${{lbl}}</div><div class="stat-val${{bH?' hi':''}}" style="${{bH?`border-color:${{B.color}}`:''}}">${{bv}}</div>`;
  }}).join('');
  const aU=chips.filter(c=>A.chips[c.key]).length,bU=chips.filter(c=>B.chips[c.key]).length;
  document.getElementById('chip-table').innerHTML=
    `<div class="chip-row" style="padding:10px 0"><div style="font-size:11px;font-weight:500;color:${{A.color}};text-align:center">${{nA}}<br><span style="font-size:10px;color:#888;font-weight:400">${{aU}} used</span></div><div style="font-size:11px;color:#888;text-align:center">Chip</div><div style="font-size:11px;font-weight:500;color:${{B.color}};text-align:center">${{nB}}<br><span style="font-size:10px;color:#888;font-weight:400">${{bU}} used</span></div></div>`+
    chips.map(c=>`<div class="chip-row"><div style="display:flex;justify-content:center">${{tickSvg(A.chips[c.key],A.color)}}</div><div style="text-align:center"><span class="chip-pill" style="background:${{c.bg}};color:${{c.tc}}">${{c.label}}</span><div style="font-size:10px;color:#555;margin-top:3px">${{c.desc}}</div></div><div style="display:flex;justify-content:center">${{tickSvg(B.chips[c.key],B.color)}}</div></div>`).join('');
  const maxP=Math.max(...[...A.races,...B.races].map(Math.abs),1);
  function chipPill(m){{
    if(!m.chipLabel)return'';
    return`<span style="display:inline-block;font-size:9px;padding:1px 5px;border-radius:20px;font-weight:500;margin-left:4px;background:${{m.chipBg}};color:${{m.chipTc}}">${{m.chipLabel}}</span>`;
  }}
  document.getElementById('race-rows').innerHTML=raceNames.map((race,i)=>{{
    const ap=A.races[i],bp=B.races[i],aW=ap>bp;
    const aPct=Math.round(Math.abs(ap)/maxP*100),bPct=Math.round(Math.abs(bp)/maxP*100);
    return`<div class="race-row">
      <div style="text-align:right"><div style="font-size:14px;font-weight:500;color:${{aW?A.color:'#888'}}">${{ap}}</div><div style="font-size:10px;margin-top:2px;text-align:right">${{raceChipPill(A,race)}}</div><div style="display:flex;justify-content:flex-end;margin-top:4px"><div style="height:6px;border-radius:3px;width:${{aPct}}%;background:${{A.color}};opacity:${{aW?1:0.4}}"></div></div></div>
      <div class="race-label">${{race}}</div>
      <div><div style="font-size:14px;font-weight:500;color:${{!aW?B.color:'#888'}}">${{bp}}</div><div style="font-size:10px;margin-top:2px">${{raceChipPill(B,race)}}</div><div style="display:flex;margin-top:4px"><div style="height:6px;border-radius:3px;width:${{bPct}}%;background:${{B.color}};opacity:${{!aW?1:0.4}}"></div></div></div></div>`;
  }}).join('');
  const cumA=A.races.reduce((acc,v)=>[...acc,acc[acc.length-1]+v],[0]);
  const cumB=B.races.reduce((acc,v)=>[...acc,acc[acc.length-1]+v],[0]);
  if(chart)chart.destroy();
  chart=new Chart(document.getElementById('h2hChart'),{{type:'line',data:{{labels:['Pre-season',...raceNames],datasets:[
    {{label:nA,data:cumA,borderColor:A.color,backgroundColor:A.color+'22',borderWidth:2.5,pointBackgroundColor:A.color,pointRadius:5,fill:true,tension:0.2}},
    {{label:nB,data:cumB,borderColor:B.color,backgroundColor:B.color+'22',borderWidth:2.5,pointBackgroundColor:B.color,pointRadius:5,fill:true,tension:0.2}}
  ]}},options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} pts`}}}}}},
    scales:{{x:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},y:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}}}}}}
  }});
}}
render();
}})();
</script>"""


# ── 04 Budget Tracker ─────────────────────────────────────────────────────────
def panel_budget(data):
    M      = data["managers_sorted"]
    b_races= data["budget_race_names"]
    RD     = data["races_done"]

    # Sort by current budget desc
    sorted_m = sorted(M, key=lambda m: -m["budget_current"])
    max_b    = max(m["budget_current"] for m in sorted_m) if sorted_m else 100
    min_b    = min(m["budget_current"] for m in sorted_m) if sorted_m else 100
    avg_b    = round(sum(m["budget_current"] for m in M) / len(M), 1) if M else 100

    rows_html = ""
    for m in sorted_m:
        cur  = m["budget_current"]
        vs   = round(cur - m["budgets"][0], 2) if m["budgets"] else 0
        lc   = m.get("budget_last_change", 0)
        pct  = round(cur / max_b * 100, 1)
        vs_c = "pos" if vs > 0 else ("neg" if vs < 0 else "neu")
        lc_c = "pos" if lc > 0 else ("neg" if lc < 0 else "neu")
        vs_s = "+" if vs > 0 else ""
        lc_s = "+" if lc > 0 else ""
        rows_html += f"""<div class="brow">
  <div style="display:flex;align-items:center;justify-content:center"><div class="dot" style="background:{m['color']}"></div></div>
  <div><div class="bname">{m['name']}</div><div class="bar-bg"><div class="bar-fill" style="width:{pct}%;background:{m['color']}"></div></div></div>
  <div class="bcol" style="font-weight:500">{cur:.1f}m</div>
  <div class="bcol {vs_c}">{vs_s}{vs:.1f}m</div>
  <div class="bcol {lc_c}">{lc_s}{lc:.1f}m</div>
</div>"""

    # Budget timeline chart
    timeline_labels = b_races[:max(len(m["budgets"]) for m in M)] if M else []
    timeline_datasets = []
    for i, m in enumerate(M):
        dash = DASH_PATTERNS[i % len(DASH_PATTERNS)]
        timeline_datasets.append({
            "label": m["name"], "data": m["budgets"],
            "borderColor": m["color"], "borderDash": dash,
            "borderWidth": 2, "pointBackgroundColor": m["color"],
            "pointRadius": 4, "pointHoverRadius": 6,
            "fill": False, "tension": 0.2
        })

    # Budget change per race — HTML table (no canvas, no lifecycle issues)
    max_races = max((len(m["budgets"]) for m in M), default=1)
    # Only show races that have actually completed (budget data exists for all managers)
    # change index i means: budgets[i] - budgets[i-1], so race label is budget_race_names[i]
    n_change_cols = max_races - 1  # number of completed race changes
    change_race_names = b_races[1:max_races] if len(b_races) >= max_races else b_races[1:]

    # Build per-manager change lists (only completed races)
    manager_changes = []
    for m in M:
        changes = [round(m["budgets"][i] - m["budgets"][i-1], 2)
                   for i in range(1, len(m["budgets"]))]
        manager_changes.append((m, changes))

    # Build table header
    race_th = "".join(
        f'<th style="font-size:10px;color:#555;font-weight:500;text-align:right;padding:4px 8px;white-space:nowrap">{rn}</th>'
        for rn in change_race_names)

    # Build table rows
    table_rows = ""
    for m, changes in manager_changes:
        cells = ""
        for c in changes:
            if c > 0:
                color = "#1D9E75"; sign = "+"
            elif c < 0:
                color = "#E24B4A"; sign = ""
            else:
                color = "#555"; sign = ""
            cells += f'<td style="font-size:12px;font-weight:500;color:{color};text-align:right;padding:5px 8px;white-space:nowrap">{sign}{c:.1f}m</td>'
        table_rows += f"""<tr style="border-bottom:0.5px solid #2a2a2a">
  <td style="padding:5px 8px;white-space:nowrap;position:sticky;left:0;background:#1a1a1a;z-index:1">
    <div style="display:flex;align-items:center;gap:7px">
      <div style="width:8px;height:8px;border-radius:50%;background:{m['color']};flex-shrink:0"></div>
      <span style="font-size:13px;font-weight:500">{m['name']}</span>
    </div>
  </td>
  {cells}
</tr>"""

    budget_change_html = f"""<div class="card" style="padding:4px 0;overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table style="border-collapse:collapse;min-width:max(100%,{max(len(change_race_names)*90+120, 300)}px)">
    <thead>
      <tr style="border-bottom:0.5px solid #2a2a2a">
        <th style="font-size:10px;color:#555;font-weight:500;text-align:left;padding:4px 8px;position:sticky;left:0;background:#1a1a1a;z-index:1;min-width:90px">Manager</th>
        {race_th}
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>"""

    legend_html = "".join(
        f'<span><span style="width:10px;height:10px;border-radius:2px;background:{m["color"]};display:inline-block"></span>{m["name"]}</span>'
        for m in M)

    import math
    # The chart's axis bounds must come from the full season history for every
    # manager (their line may have dipped lower, or peaked higher, than where
    # they sit today) — using only *current* budgets (like the stat cards
    # above do, correctly) would clip off those earlier highs/lows.
    all_budget_values = [v for m in M for v in m["budgets"]]
    hist_max = max(all_budget_values) if all_budget_values else max_b
    hist_min = min(all_budget_values) if all_budget_values else min_b
    y_max = math.ceil((hist_max + 2) / 2) * 2   # next even number at least 2m above highest-ever
    y_min = math.floor((hist_min - 2) / 2) * 2  # next even number at least 2m below lowest-ever
    y_min = max(80, y_min)   # never go below 80m
    y_max = min(120, y_max)  # never go above 120m

    return f"""<div class="subtitle">Budget Tracker · Team values across the season</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{max_b:.1f}m</div><div class="mc-lbl">Highest budget ({sorted_m[0]['name']})</div></div>
  <div class="mc"><div class="mc-val">{min_b:.1f}m</div><div class="mc-lbl">Lowest budget ({sorted_m[-1]['name']})</div></div>
  <div class="mc"><div class="mc-val">{round(max_b - min_b, 1)}m</div><div class="mc-lbl">Largest gap</div></div>
  <div class="mc"><div class="mc-val">{avg_b}m</div><div class="mc-lbl">League average</div></div>
</div>
<div class="section-label">Current budgets</div>
<div class="card">
  <div style="display:grid;grid-template-columns:26px 1fr 52px 60px 60px;gap:10px;padding:6px 0 2px">
    <div></div><div></div>
    <div class="col-hdr">Current</div><div class="col-hdr">vs start</div><div class="col-hdr">Last race</div>
  </div>
  {rows_html}
</div>
<div class="section-label">Budget timeline</div>
<div style="position:relative;height:280px"><canvas id="budgetChart"></canvas></div>
<div class="legend" id="legend-budget"></div>
<div class="section-label">Budget change per race</div>
{budget_change_html}
<script>
(function(){{
new Chart(document.getElementById('budgetChart'),{{
  type:'line',
  data:{{labels:{js(timeline_labels)},datasets:{js(timeline_datasets)}}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toFixed(1)}}m`}}}}}},
    scales:{{x:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
             y:{{min:{y_min},max:{y_max},ticks:{{color:'#888',callback:v=>v.toFixed(0)+'m'}},grid:{{color:'rgba(255,255,255,0.06)'}}}}}}}}
}});
document.getElementById('legend-budget').innerHTML={js(legend_html)};
}})();
</script>"""


# ── 05 Podiums ────────────────────────────────────────────────────────────────
def panel_podiums(data):
    M   = data["managers_sorted"]
    POD = data["podiums"]
    ND  = data["n_done"]
    NT  = data["n_total"]
    FD  = data["finish_dist"]

    # pod["first"]/["second"]/["third"] are lists now (a tied race credits
    # every tied manager, not just one) — membership check, not equality.
    managers_with_pod = sum(1 for m in M if sum(FD.get(m["name"], [0,0,0])[:3]) > 0)
    diff_winners = len({name for p in POD for name in p["first"]})

    cards_html = podium_table_html(POD, M)

    # Podium share — same finish_dist the Stats tab uses, not an independent
    # re-count from POD, so the two pages can't drift apart. Shown as one
    # medal icon per podium finish rather than a stacked bar. CSS-colored
    # circles, not emoji — emoji medal colors are drawn by the OS/browser's
    # emoji font and can't be tuned (gold ended up reading too close to
    # bronze), whereas these use the same explicit hex values as the medal
    # badges elsewhere in the app.
    def medal_dot(color):
        return (f'<span style="display:inline-block;width:13px;height:13px;border-radius:50%;'
                f'background:{color};box-shadow:inset 0 0 0 1px rgba(0,0,0,.25)"></span>')

    GOLD, SILVER, BRONZE = "#FFDE21", "#C0C0C0", "#CD7F32"

    medal_rows_html = ""
    for m in M:
        fd = FD.get(m["name"], [0, 0, 0])
        p1, p2, p3 = fd[0], fd[1], fd[2]
        total = p1 + p2 + p3
        icons = medal_dot(GOLD) * p1 + medal_dot(SILVER) * p2 + medal_dot(BRONZE) * p3
        icons_html = icons if icons else '<span style="color:#555;font-size:12px">No podiums yet</span>'
        medal_rows_html += f"""<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:0.5px solid #2a2a2a">
  <div class="dot" style="background:{m['color']}"></div>
  <div style="min-width:70px;font-size:13px;font-weight:500;flex-shrink:0">{m['name']}</div>
  <div style="flex:1;display:flex;flex-wrap:wrap;gap:5px;align-items:center">{icons_html}</div>
  <div style="font-size:12px;color:#888;min-width:20px;text-align:right;flex-shrink:0">{total}</div>
</div>"""

    return f"""<div class="subtitle">Podiums · Race results &amp; podium share</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{ND}</div><div class="mc-lbl">Races complete</div></div>
  <div class="mc"><div class="mc-val">{managers_with_pod}</div><div class="mc-lbl">Managers with a podium</div></div>
  <div class="mc"><div class="mc-val">{diff_winners}</div><div class="mc-lbl">Different race winners</div></div>
  <div class="mc"><div class="mc-val">{len(M) - managers_with_pod}</div><div class="mc-lbl">Managers without a podium</div></div>
</div>
<div class="section-label">Podium share</div>
<div class="hint" style="margin-bottom:8px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
  <span>{medal_dot(GOLD)} 1st</span><span>&middot;</span><span>{medal_dot(SILVER)} 2nd</span><span>&middot;</span><span>{medal_dot(BRONZE)} 3rd</span><span>— one dot per podium finish this season.</span>
</div>
<div class="card" style="padding:4px 16px">{medal_rows_html}</div>
<div class="section-label">Race results</div>
{cards_html}"""


# ── 06 Stats ─────────────────────────────────────────────────────────────────
def panel_stats(data):
    M   = data["managers_sorted"]
    ND  = data["n_done"]
    HL  = data["highlights"]

    total_race_scores = len(data["races_done"])
    chips_used_count  = sum(1 for m in M for c in CHIP_ORDER if m["chips"].get(c))

    # Finish distribution table
    N_FIN = data.get("n_finish_cols", len(M))

    pos_headers = "".join(
        f'<div style="font-size:9px;color:#555;text-align:center">P{i}</div>'
        for i in range(1, N_FIN + 1))
    finish_rows_html = ""
    for m in M:
        fd = data["finish_dist"].get(m["name"], [0]*N_FIN)
        cells = ""
        for i, v in enumerate(fd[:N_FIN]):
            if v == 0:
                cls = "f0"
            elif i == 0: cls = "f1"
            elif i == 1: cls = "f2"
            elif i == 2: cls = "f3"
            else:        cls = "fn"
            cells += f'<div style="display:flex;justify-content:center"><div class="fin {cls}">{v if v else ""}</div></div>'
        finish_rows_html += f"""<div class="srow">
  <div style="display:flex;align-items:center;justify-content:center"><div class="dot" style="background:{m['color']}"></div></div>
  <div class="sname">{m['name']}</div>
  {cells}<div class="total-pts">{m['total']}</div>
</div>"""

    # Chip usage rows
    chip_rows_html = ""
    for chip in CHIP_ORDER:
        cs    = CHIP_STYLES[chip]
        users = [m["name"] for m in M if m["chips"].get(chip)]
        users_html = "".join(
            f'<span style="font-size:12px;font-weight:500;color:{MANAGER_COLOURS.get(u,"#888")}">{u}</span>'
            for u in users) or '<span style="font-size:12px;color:#555">Not yet used</span>'
        chip_rows_html += (
            f'<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:0.5px solid #2a2a2a">'
            f'<span class="chip-pill" style="background:{cs["bg"]};color:{cs["tc"]};font-size:11px;padding:2px 10px">{chip}</span>'
            f'<div style="flex:1;display:flex;gap:6px;flex-wrap:wrap">{users_html}</div>'
            f'<span style="font-size:11px;color:#888">{len(users)}/{len(M)} used</span></div>')

    # Highlights
    best_m, best_r, best_pts      = HL["best"]
    worst_m, worst_r, worst_pts   = HL["worst"]
    best_avg_m                    = HL["best_avg"]
    worst_avg_m                   = HL["worst_avg"]
    cons_m, cons_pos              = HL["most_consistent"]
    swing_m, swing_val            = HL["biggest_swing"]
    transfers_m                   = HL["most_transfers"]
    luck_m                        = HL["worst_luck"]

    def hl_row(name, desc):
        color = MANAGER_COLOURS.get(name, "#888")
        return (f'<div class="hl-card"><div class="hl-title">{desc[0]}</div>'
                f'<div class="hl-row"><div class="hl-dot" style="background:{color}"></div>'
                f'<div class="hl-name">{name}</div>'
                f'<div class="hl-val">{desc[1]}</div></div></div>')

    highlights_html = (
        hl_row(best_m,     ["Highest single race score", f"{best_pts} pts — {best_r}"]) +
        hl_row(worst_m,    ["Lowest single race score",  f"{worst_pts} pts — {worst_r}"]) +
        hl_row(best_avg_m["name"],  ["Best average per race",   f"{best_avg_m['avg']:.1f} pts/race"]) +
        hl_row(worst_avg_m["name"], ["Worst average per race",  f"{worst_avg_m['avg']:.1f} pts/race"]) +
        hl_row(cons_m["name"],  ["Most consistent",  cons_pos]) +
        hl_row(swing_m["name"], ["Biggest position swing", f"{swing_val} places"]) +
        hl_row(transfers_m["name"], ["Most active trader", f"{transfers_m['total_transfers']} transfers"]) +
        (hl_row(luck_m["name"], ["Worst luck", f"{luck_m['total_inactive_penalty']} pts lost to inactive picks"])
         if luck_m["total_inactive_penalty"] > 0 else
         hl_row("—", ["Worst luck", "No inactive-driver penalties yet"]))
    )

    # Name column's minimum trimmed from the original ~100px, but kept as
    # minmax(...,1fr) rather than a flat width — on mobile there's no spare
    # space to grow into so it renders at the tight minimum, but on a wide
    # PC viewport it still stretches to fill the row, which is what pushes
    # P1-P8/Pts flush to the right edge of the card (matching the original
    # look there) instead of the whole table clustering flush-left with a
    # dead gap on the right.
    fin_grid = f"26px minmax(58px,1fr) repeat({N_FIN},26px) 36px"
    fin_min_w = 26 + 58 + 26 * N_FIN + 36 + 5 * (N_FIN + 3)

    return f"""<div class="subtitle">Stats · Season overview &amp; highlights</div>
<style>.srow{{display:grid;grid-template-columns:{fin_grid};align-items:center;gap:5px;padding:7px 0;border-bottom:0.5px solid #2a2a2a;min-width:{fin_min_w}px}}</style>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{total_race_scores}</div><div class="mc-lbl">Races scored so far</div></div>
  <div class="mc"><div class="mc-val">{chips_used_count}</div><div class="mc-lbl">Chips used league-wide</div></div>
  <div class="mc"><div class="mc-val">{cons_m['name']}</div><div class="mc-lbl">Most consistent ({cons_pos})</div></div>
  <div class="mc"><div class="mc-val">{swing_m['name']}</div><div class="mc-lbl">Biggest swing ({swing_val} places)</div></div>
</div>
<div class="section-label">Finish distribution</div>
<div class="card" style="padding:4px 8px 4px 2px;overflow-x:auto;-webkit-overflow-scrolling:touch">
  <div style="display:grid;grid-template-columns:{fin_grid};gap:5px;padding:6px 0 2px;min-width:{fin_min_w}px">
    <div></div><div></div>{pos_headers}<div style="font-size:9px;color:#555;text-align:right">Pts</div>
  </div>
  {finish_rows_html}
</div>
<div class="section-label">Chip usage</div>
<div class="card" style="padding-left:8px">{chip_rows_html}</div>
<div class="section-label">Season highlights</div>
<div class="hl-grid">{highlights_html}</div>"""


# ── 07 Position Changes ───────────────────────────────────────────────────────
def panel_positions(data):
    M   = data["managers_sorted"]
    RD  = data["races_done"]
    ND  = data["n_done"]

    if ND == 0:
        return "<div class='subtitle'>Position Changes · No races complete yet</div>"

    # Most gained / lost in a SINGLE race (not overall)
    best_single  = ("—", 0, "—")   # (manager, gain, race_name)
    worst_single = ("—", 0, "—")   # (manager, loss, race_name)
    for m in M:
        for ri in range(1, len(m["positions"])):
            prev = m["positions"][ri - 1]
            curr = m["positions"][ri]
            if prev is None or curr is None:
                continue
            change = prev - curr   # positive = moved up
            race_name = RD[ri]["name"] if ri < len(RD) else "—"
            if change > best_single[1]:
                best_single  = (m["name"], change, race_name)
            if change < worst_single[1]:
                worst_single = (m["name"], change, race_name)

    # Longest streak at current position
    streak_best = ("—", 0)   # (manager, streak_length)
    for m in M:
        cur_pos = next((p for p in reversed(m["positions"]) if p is not None), None)
        if cur_pos is None:
            continue
        streak = 0
        for p in reversed([p for p in m["positions"] if p is not None]):
            if p == cur_pos:
                streak += 1
            else:
                break
        if streak > streak_best[1]:
            streak_best = (m["name"], streak)

    last_race_name = RD[-1]["name"] if RD else "—"

    # ── Position timeline chart ───────────────────────────────────────────────
    race_labels = [r["name"] for r in RD]
    datasets = []
    for i, m in enumerate(M):
        if not m["positions"]:
            continue
        # Convert None → null for JS; Chart.js will gap them with spanGaps:false
        # Use actual values only — Chart.js handles nulls fine with spanGaps:true
        pts = [p if p is not None else "null" for p in m["positions"]]
        pts_js = "[" + ",".join(str(p) for p in pts) + "]"
        dash = DASH_PATTERNS[i % len(DASH_PATTERNS)]
        datasets.append({
            "__name":       m["name"],
            "__pts":        pts_js,   # pre-rendered, not via js()
            "__color":      m["color"],
            "__dash":       js(dash),
        })

    legend_html = "".join(
        f'<span><span style="width:10px;height:10px;border-radius:2px;background:{m["color"]};display:inline-block;flex-shrink:0"></span>{m["name"]}</span>'
        for m in M)

    # Build datasets JS manually so pts_js goes in unquoted
    ds_js = "[" + ",".join(
        f'{{label:{js(d["__name"])},data:{d["__pts"]},borderColor:{js(d["__color"])},'
        f'borderDash:{d["__dash"]},borderWidth:2.5,pointBackgroundColor:{js(d["__color"])},'
        f'pointRadius:6,pointHoverRadius:8,fill:false,tension:0,spanGaps:true}}'
        for d in datasets
    ) + "]"

    return f"""<div class="subtitle">Position Changes · How the standings have shifted</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">+{best_single[1]}</div><div class="mc-lbl">Biggest single-race climb — {best_single[0]} ({best_single[2]})</div></div>
  <div class="mc"><div class="mc-val">{worst_single[1]}</div><div class="mc-lbl">Biggest single-race drop — {worst_single[0]} ({worst_single[2]})</div></div>
  <div class="mc"><div class="mc-val">{streak_best[1]} race{'s' if streak_best[1] != 1 else ''}</div><div class="mc-lbl">Longest position streak — {streak_best[0]}</div></div>
  <div class="mc"><div class="mc-val">{last_race_name}</div><div class="mc-lbl">Most recent race</div></div>
</div>
<div class="hint" style="margin-bottom:1.5rem">Numbers show overall standings after each race.</div>
<div class="section-label">Position timeline</div>
<div style="position:relative;height:360px">
  <div id="posChartYAxis" style="position:absolute;top:0;left:0;width:36px;height:100%;z-index:2;background:#0f0f0f;pointer-events:none"></div>
  <div id="posChartScroll" style="overflow-x:auto;-webkit-overflow-scrolling:touch;height:100%;padding-left:36px;box-sizing:border-box">
    <div style="position:relative;height:100%;min-width:{max(len(race_labels) * 46, 380)}px">
      <canvas id="posChart"></canvas>
    </div>
  </div>
</div>
<div class="legend" id="legend-positions"></div>
<script>
(function(){{
const posChart=new Chart(document.getElementById('posChart'),{{
  type:'line',
  data:{{labels:{js(race_labels)},datasets:{ds_js}}},
  options:{{responsive:true,maintainAspectRatio:false,
    layout:{{padding:{{top:10,left:0}}}},
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: P${{ctx.parsed.y}}`}}}}}},
    scales:{{
      x:{{ticks:{{color:'#888',font:{{size:12}}}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
      y:{{reverse:true,min:0.5,max:{len(M)}+0.5,
        afterBuildTicks:function(ax){{ax.ticks=[...[{','.join(str(i) for i in range(1,len(M)+1))}].map(v=>{{return{{value:v}};}})]}},
        ticks:{{color:'#888',display:false}},
        grid:{{color:'rgba(255,255,255,0.06)'}},border:{{display:false}}}}
    }}}}
}});
function freezePosYAxis(){{
  const yAxis=document.getElementById('posChartYAxis');
  const cvs=document.getElementById('posChart');
  const scale=posChart.scales.y;
  if(!scale)return;
  const dpr=window.devicePixelRatio||1;
  const cssH=cvs.clientHeight||360;
  const fc=document.createElement('canvas');
  fc.width=36*dpr; fc.height=cssH*dpr;
  fc.style.width='36px'; fc.style.height=cssH+'px';
  const ctx2=fc.getContext('2d');
  ctx2.scale(dpr,dpr);
  ctx2.fillStyle='#0f0f0f';
  ctx2.fillRect(0,0,36,cssH);
  const ticks=scale.ticks;
  const top=scale.top; const bottom=scale.bottom;
  const range=bottom-top;
  const vMin=scale.min; const vMax=scale.max;
  ticks.forEach(t=>{{
    const v=t.value;
    if(v<1||v>{len(M)})return;
    const yPx=top+((vMax-v)/(vMax-vMin))*range;
    ctx2.fillStyle='#888';
    ctx2.font='11px -apple-system,BlinkMacSystemFont,sans-serif';
    ctx2.textAlign='right';
    ctx2.fillText('P'+Math.round(v),32,yPx+4);
  }});
  yAxis.innerHTML='';
  yAxis.appendChild(fc);
}}
posChart.options.animation={{onComplete:freezePosYAxis}};
posChart.update();
document.getElementById('legend-positions').innerHTML={js(legend_html)};
}})();
</script>"""


# ── 08 Team Picks ─────────────────────────────────────────────────────────────
def panel_picks(data):
    M           = data["managers_sorted"]
    RD          = data["races_done"]
    drv_results = data.get("driver_results", {})       # {driver_name: {race_name: pts}}
    con_results = data.get("constructor_results", {})  # {con_name:    {race_name: pts}}
    season_pts  = data.get("player_season_points", {})
    season_val  = data.get("player_season_value", {})

    def by_season_points(names):
        # Highest current season total first, not alphabetical.
        return sorted(names, key=lambda n: -(season_pts.get(n) or 0))

    # Ordered list of races that have lineup data
    all_lineup_races = sorted(set(
        rname for m in M for rname in m["lineups"].keys()
    ), key=lambda r: next((rd["round"] for rd in data["races"] if rd["name"] == r), 99))

    if not all_lineup_races:
        return """<div class="subtitle">Team Picks · No lineup data available yet</div>"""

    n_managers = len(M)

    # ── Season-level stats (across all races) ─────────────────────────────────
    driver_counts_all = {}
    con_counts_all    = {}
    drs_counts_all    = {}

    for m in M:
        for rname, picks in m["lineups"].items():
            for pick in picks:
                nm = pick["name"]
                if pick.get("is_constructor"):
                    con_counts_all[nm] = con_counts_all.get(nm, 0) + 1
                else:
                    driver_counts_all[nm] = driver_counts_all.get(nm, 0) + 1
                    if pick["drs"]:
                        drs_counts_all[nm] = drs_counts_all.get(nm, 0) + 1

    top_driver     = max(driver_counts_all, key=driver_counts_all.get) if driver_counts_all else "—"
    top_con        = max(con_counts_all,    key=con_counts_all.get)    if con_counts_all    else "—"
    top_drs        = max(drs_counts_all,    key=drs_counts_all.get)    if drs_counts_all    else "—"
    top_driver_cnt = driver_counts_all.get(top_driver, 0)
    top_con_cnt    = con_counts_all.get(top_con, 0)
    top_drs_cnt    = drs_counts_all.get(top_drs, 0)

    # ── Per-race JS data ──────────────────────────────────────────────────────
    # teams_by_race: per race, list of manager picks (with pts per pick, DRS flag)
    teams_by_race = {}
    for rname in all_lineup_races:
        teams_by_race[rname] = []
        for m in M:
            picks = m["lineups"].get(rname, [])
            if not picks:
                continue
            race_pts = sum((p["pts"] or 0) for p in picks)
            chip_by_race = m.get("chip_by_race", {})
            chip_name = chip_by_race.get(rname)
            chip_style = CHIP_STYLES.get(chip_name, {})
            teams_by_race[rname].append({
                "name":      m["name"],
                "teamName":  m["team"],
                "color":     m["color"],
                "racePts":   race_pts,
                "chip":      {"label": chip_name, "bg": chip_style.get("bg",""), "tc": chip_style.get("tc","")} if chip_name else None,
                "picks":     [{"name": p["name"], "drs": p["drs"], "drsMarker": p.get("drs_marker",""),
                               "pts": p["pts"], "isCon": p["is_constructor"],
                               "seasonPts": season_pts.get(p["name"]) or 0,
                               "value": p.get("value"),
                               "seasonValue": season_val.get(p["name"]) or 0} for p in picks],
            })
        # Sort by race points descending so top scorer shows first
        teams_by_race[rname].sort(key=lambda t: -t["racePts"])

    # popularity_by_race: {race_name: {"drivers": [(name, count, pts), ...], "cons": [...]}}
    # Counts how many managers picked each driver/con that race, alongside their actual pts
    popularity_by_race = {}
    for rname in all_lineup_races:
        d_cnt, c_cnt = {}, {}
        for m in M:
            for pick in m["lineups"].get(rname, []):
                nm = pick["name"]
                if pick.get("is_constructor"):
                    c_cnt[nm] = c_cnt.get(nm, 0) + 1
                else:
                    d_cnt[nm] = d_cnt.get(nm, 0) + 1
        # Merge in actual points from results tables
        d_rows = sorted(d_cnt.items(), key=lambda x: -x[1])
        c_rows = sorted(c_cnt.items(), key=lambda x: -x[1])
        popularity_by_race[rname] = {
            "drivers": [{"name": n, "count": c,
                         "pts": (drv_results.get(n, {}).get(rname) or {}).get("pts")} for n, c in d_rows],
            "cons":    [{"name": n, "count": c,
                         "pts": (con_results.get(n, {}).get(rname) or {}).get("pts")} for n, c in c_rows],
            "maxD":    d_rows[0][1] if d_rows else 1,
            "maxC":    c_rows[0][1] if c_rows else 1,
        }

    # ── Trades per race ───────────────────────────────────────────────────────
    # Limitless races and the race immediately after are excluded from diffs —
    # the game auto-resets to the pre-Limitless lineup so neither transition
    # reflects a genuine transfer decision.
    trades_by_race = {}
    for i, rname in enumerate(all_lineup_races):
        if i == 0:
            continue
        prev_rname = all_lineup_races[i - 1]
        race_trades = []
        for m in M:
            cbr           = m.get("chip_by_race", {})
            is_wildcard   = cbr.get(rname) == "Wildcard"
            is_limitless  = cbr.get(rname) == "Limitless"
            was_limitless = cbr.get(prev_rname) == "Limitless"

            # Skip entirely for Limitless and post-Limitless — not real trades
            if is_limitless or was_limitless:
                continue

            prev_picks = m["lineups"].get(prev_rname, [])
            curr_picks = m["lineups"].get(rname, [])
            if not prev_picks or not curr_picks:
                continue

            prev_d = {p["name"] for p in prev_picks if not p["is_constructor"]}
            prev_c = {p["name"] for p in prev_picks if p["is_constructor"]}
            curr_d = {p["name"] for p in curr_picks if not p["is_constructor"]}
            curr_c = {p["name"] for p in curr_picks if p["is_constructor"]}

            sold_d   = by_season_points(prev_d - curr_d)
            bought_d = by_season_points(curr_d - prev_d)
            sold_c   = by_season_points(prev_c - curr_c)
            bought_c = by_season_points(curr_c - prev_c)

            # One combined out/in group per manager, not paired index-to-index
            # — a 2-for-2 swap is one trade of a pair for a pair, not two
            # separate 1-to-1 swaps, so it gets a single arrow, not two.
            outs = [{"name": n, "isCon": False} for n in sold_d]   + [{"name": n, "isCon": True} for n in sold_c]
            ins  = [{"name": n, "isCon": False} for n in bought_d] + [{"name": n, "isCon": True} for n in bought_c]

            total_trades = len(sold_d) + len(sold_c)

            if outs or ins or is_wildcard:
                race_trades.append({
                    "name":       m["name"],
                    "color":      m["color"],
                    "team":       m["team"],
                    "wildcard":   is_wildcard,
                    "outs":       outs,
                    "ins":        ins,
                    "tradeCount": total_trades,
                })

        race_trades.sort(key=lambda x: (-x["tradeCount"], x["name"]))
        trades_by_race[rname] = race_trades

    # ── DRS performance per manager ────────────────────────────────────────────
    # Collect ALL drs picks per race (Extra DRS chip gives both a 2X and 3X pick)
    drs_stats = []
    for m in M:
        races_with_drs = []
        for rname in all_lineup_races:
            picks = m["lineups"].get(rname, [])
            drs_picks = [p for p in picks if p["drs"]]
            if not drs_picks:
                continue
            for drs_pick in drs_picks:
                others = [p for p in picks if not p["drs"] and p["pts"] is not None]
                other_avg = round(sum(p["pts"] for p in others) / len(others), 1) if others else None
                races_with_drs.append({
                    "race":     rname,
                    "pick":     drs_pick["name"],
                    "pts":      drs_pick["pts"],
                    "marker":   drs_pick.get("drs_marker", "2X"),
                    "otherAvg": other_avg,
                })
        if not races_with_drs:
            continue
        scored = [r for r in races_with_drs if r["pts"] is not None]
        hit_rate   = round(sum(1 for r in scored if r["pts"] > 0) / len(scored) * 100) if scored else 0
        avg_pts    = round(sum(r["pts"] for r in scored) / len(scored), 1) if scored else 0
        drs_stats.append({
            "name":    m["name"],
            "color":   m["color"],
            "races":   races_with_drs,
            "hitRate": hit_rate,
            "avgPts":  avg_pts,
            "count":   len(races_with_drs),
        })
    drs_stats.sort(key=lambda x: -x["avgPts"])

    # ── Trade regrets ─────────────────────────────────────────────────────────
    # For each sold→bought pair, compute a combined Borda rank score across two
    # dimensions: points scored and budget change that race.
    # For each dimension, rank all assets of that type (driver or constructor) from
    # best to worst that race. Convert to a 0–1 percentile (1 = best in field).
    # Regret score = avg of sold_percentile − avg of bought_percentile across both dims.
    # A positive score means the sold asset was better-ranked than the replacement
    # on the combined pts+budget axis. Limitless races excluded.

    def build_rank_lookup(results_dict, race_name):
        """Return {asset_name: {"pts_pct": 0-1, "bud_pct": 0-1}} for a given race."""
        entries = []
        for name, races in results_dict.items():
            r = races.get(race_name, {}) or {}
            pts = r.get("pts")
            bud = r.get("bud")
            if pts is not None or bud is not None:
                entries.append((name, pts, bud))
        if not entries:
            return {}
        n = len(entries)
        # Rank by pts (higher = better)
        pts_sorted = sorted(entries, key=lambda x: (x[1] is not None, x[1] or 0))
        pts_rank   = {e[0]: i / (n - 1) if n > 1 else 0.5 for i, e in enumerate(pts_sorted)}
        # Rank by budget change (higher = better)
        bud_sorted = sorted(entries, key=lambda x: (x[2] is not None, x[2] or 0))
        bud_rank   = {e[0]: i / (n - 1) if n > 1 else 0.5 for i, e in enumerate(bud_sorted)}
        return {e[0]: {"pts_pct": pts_rank[e[0]], "bud_pct": bud_rank[e[0]]} for e in entries}

    regrets = []
    wins    = []
    all_trade_impacts = []
    for i, rname in enumerate(all_lineup_races[1:], 1):
        prev_rname = all_lineup_races[i - 1]

        # Build rank lookups for this race (drivers and constructors separately)
        drv_ranks = build_rank_lookup(drv_results, rname)
        con_ranks = build_rank_lookup(con_results, rname)

        for m in M:
            cbr = m.get("chip_by_race", {})
            if cbr.get(rname) == "Limitless" or cbr.get(prev_rname) == "Limitless":
                continue
            prev_picks = m["lineups"].get(prev_rname, [])
            curr_picks = m["lineups"].get(rname, [])
            if not prev_picks or not curr_picks:
                continue

            prev_d = {p["name"] for p in prev_picks if not p["is_constructor"]}
            prev_c = {p["name"] for p in prev_picks if p["is_constructor"]}
            curr_d = {p["name"] for p in curr_picks if not p["is_constructor"]}
            curr_c = {p["name"] for p in curr_picks if p["is_constructor"]}

            sold_d   = by_season_points(prev_d - curr_d)
            bought_d = by_season_points(curr_d - prev_d)
            sold_c   = by_season_points(prev_c - curr_c)
            bought_c = by_season_points(curr_c - prev_c)

            # Whole trade evaluated as one group vs. one group — a 2-for-2
            # swap is judged on its combined outcome, not as two arbitrary
            # index-paired 1-to-1 "sold X for Y" comparisons that never
            # actually happened that way (matches the single-arrow display).
            sold_all   = [(n, False) for n in sold_d] + [(n, True) for n in sold_c]
            bought_all = [(n, False) for n in bought_d] + [(n, True) for n in bought_c]
            if not sold_all or not bought_all:
                continue

            results_by_type = {False: drv_results, True: con_results}
            ranks_by_type   = {False: drv_ranks,   True: con_ranks}

            def gather(group):
                pts_list, bud_list, pct_list = [], [], []
                for gname, gis_con in group:
                    gr = results_by_type[gis_con].get(gname, {}).get(rname) or {}
                    gpts = gr.get("pts")
                    if gpts is None:
                        return None
                    grank = ranks_by_type[gis_con].get(gname)
                    if not grank:
                        return None
                    pts_list.append(gpts)
                    bud_list.append(gr.get("bud") or 0)
                    pct_list.append((grank["pts_pct"] + grank["bud_pct"]) / 2)
                return pts_list, bud_list, pct_list

            sold_data   = gather(sold_all)
            bought_data = gather(bought_all)
            if sold_data is None or bought_data is None:
                continue
            sold_pts_l, sold_bud_l, sold_pct_l = sold_data
            bought_pts_l, bought_bud_l, bought_pct_l = bought_data

            sold_score   = sum(sold_pct_l) / len(sold_pct_l)
            bought_score = sum(bought_pct_l) / len(bought_pct_l)
            diff_score   = sold_score - bought_score   # positive = regret, negative = good trade

            sold_total_pts   = sum(sold_pts_l)
            bought_total_pts = sum(bought_pts_l)
            sold_total_bud   = round(sum(sold_bud_l), 2)
            bought_total_bud = round(sum(bought_bud_l), 2)

            entry = {
                "manager":   m["name"],
                "color":     m["color"],
                "sold":      [{"name": n, "isCon": c} for n, c in sold_all],
                "bought":    [{"name": n, "isCon": c} for n, c in bought_all],
                "soldPts":   sold_total_pts,
                "boughtPts": bought_total_pts,
                "soldBud":   sold_total_bud,
                "boughtBud": bought_total_bud,
                "nextRace":  rname,
                "soldAfter": prev_rname,
            }

            # Swing is always "bought minus sold" — the actual net effect on
            # the manager's score/value from having made the trade, so a
            # regret correctly shows as negative (you'd have scored more had
            # you not made the swap), not a positive "how much you lost by".
            pts_margin = round(bought_total_pts - sold_total_pts, 1)
            bud_margin = round(bought_total_bud - sold_total_bud, 2)

            # Every trade's swing, regardless of regret/win classification —
            # feeds Transfer Impact per Race/Overview, which cares about the
            # raw swing for every trade a manager made, not just the top 3
            # standout regrets/wins the Trade Regrets tables highlight.
            all_trade_impacts.append({**entry, "ptsMargin": pts_margin, "budMargin": bud_margin})

            if diff_score > 0 and sold_total_pts > bought_total_pts:
                # Regret: sold group outperformed bought group
                regrets.append({**entry,
                    "ptsMargin": pts_margin,
                    "budMargin": bud_margin,
                    "score":     round(diff_score * 100, 1),
                })
            elif diff_score < 0 and bought_total_pts > sold_total_pts:
                # Win: bought group outperformed sold group
                wins.append({**entry,
                    "ptsMargin": pts_margin,
                    "budMargin": bud_margin,
                    "score":     round(-diff_score * 100, 1),
                })

    regrets.sort(key=lambda x: -x["score"])
    top_regrets = regrets[:3]
    wins.sort(key=lambda x: -x["score"])
    top_wins = wins[:3]

    # ── Transfer impact (per race + season overview) ─────────────────────────
    impact_by_manager = {}
    for t in all_trade_impacts:
        impact_by_manager.setdefault(t["manager"], []).append(t)

    impact_overview = []
    for m in M:
        trades = impact_by_manager.get(m["name"], [])
        impact_overview.append({
            "name":     m["name"],
            "color":    m["color"],
            "goodPts":  sum(1 for t in trades if t["ptsMargin"] > 0),
            "badPts":   sum(1 for t in trades if t["ptsMargin"] < 0),
            "netPts":   round(sum(t["ptsMargin"] for t in trades), 1),
            "goodBud":  sum(1 for t in trades if t["budMargin"] > 0),
            "badBud":   sum(1 for t in trades if t["budMargin"] < 0),
            "netBud":   round(sum(t["budMargin"] for t in trades), 2),
        })

    # ── Loyalty ───────────────────────────────────────────────────────────────
    if len(all_lineup_races) >= 2:
        loyalty = {}
        for m in M:
            held = None
            for rname in all_lineup_races:
                names = {p["name"] for p in m["lineups"].get(rname, [])}
                held = names if held is None else held & names
            for asset in (held or set()):
                loyalty.setdefault(asset, []).append(m["name"])
        loyalty_highlights = sorted(
            [(asset, mgrs) for asset, mgrs in loyalty.items() if len(mgrs) >= 2],
            key=lambda x: -len(x[1]))[:4]
    else:
        loyalty_highlights = []

    # ── Trade count summary ───────────────────────────────────────────────────
    total_trades_per_manager = {}
    for rname, race_trades in trades_by_race.items():
        for entry in race_trades:
            n = entry["name"]
            total_trades_per_manager[n] = total_trades_per_manager.get(n, 0) + entry["tradeCount"]
    most_active     = max(total_trades_per_manager, key=total_trades_per_manager.get) if total_trades_per_manager else "—"
    most_active_cnt = total_trades_per_manager.get(most_active, 0)
    least_active    = min(total_trades_per_manager, key=total_trades_per_manager.get) if total_trades_per_manager else "—"
    least_active_cnt= total_trades_per_manager.get(least_active, 0)

    # ── HTML helpers ──────────────────────────────────────────────────────────
    _rd_names  = [r["name"] for r in data["races_done"]]
    _last_done = _rd_names[-1] if _rd_names else (all_lineup_races[-1] if all_lineup_races else "")
    trade_race_opts = "".join(
        f'<option value="{rn}"{" selected" if rn == _last_done else ""}>{rn}</option>'
        for rn in _rd_names[1:])
    race_options = "".join(
        f'<option value="{rn}"{" selected" if rn == _last_done else ""}>{rn}</option>'
        for rn in _rd_names)
    impact_manager_opts = "".join(f'<option value="{m["name"]}">{m["name"]}</option>' for m in M)

    def fmt_pts(v):
        return f'+{v}' if v >= 0 else str(v)
    def fmt_bud(v):
        if v is None: return '—'
        return f'+{v:.1f}m' if v >= 0 else f'{v:.1f}m'

    loyalty_html = ""
    if loyalty_highlights:
        items = "".join(
            f'<div class="loyalty-item"><div class="loyalty-asset">{asset}</div>'
            f'<div class="loyalty-holders">{", ".join(mgrs)}</div></div>'
            for asset, mgrs in loyalty_highlights)
        loyalty_html = f"""
<div class="section-label">Loyal holds — picked every race</div>
<div class="card" style="padding:12px 16px">{items}</div>"""

    def trade_swing_table(rows):
        body = ""
        for r in rows:
            sold_names   = ", ".join(item["name"] for item in r["sold"])
            bought_names = ", ".join(item["name"] for item in r["bought"])
            body += (
                f'<tr>'
                f'<td><div style="display:flex;align-items:center;gap:6px;white-space:nowrap">'
                f'<div class="team-dot" style="background:{r["color"]}"></div>{r["manager"]}</div></td>'
                f'<td style="color:#888;white-space:nowrap">{r["nextRace"]}</td>'
                f'<td style="color:#f44336">{sold_names}</td>'
                f'<td style="color:#4caf50">{bought_names}</td>'
                f'<td style="white-space:nowrap">{fmt_pts(r["ptsMargin"])} pts</td>'
                f'<td style="white-space:nowrap">{fmt_bud(r["budMargin"])}</td>'
                f'<td style="color:#888">{r["score"]}</td>'
                f'</tr>'
            )
        return f"""<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
<table class="regret-table">
  <thead><tr>
    <th>Manager</th><th>Race</th><th>Sold</th><th>Bought</th><th>Pts swing</th><th>Value swing</th><th>Score</th>
  </tr></thead>
  <tbody>{body}</tbody>
</table>
</div>"""

    regret_html = ""
    if top_regrets:
        regret_html = f"""
<div class="section-label">Trade regrets — sold too soon</div>
<div class="hint" style="margin-bottom:12px">Trades where the sold group outranked its replacement on both points and value change the following race. Score is a combined percentile ranking (0–100). Limitless races excluded.</div>
{trade_swing_table(top_regrets)}"""

    wins_html = ""
    if top_wins:
        wins_html = f"""
<div class="section-label">Best trades — great calls</div>
<div class="hint" style="margin-bottom:12px">Trades where the bought group outranked what was sold on both points and value change the following race. Score is a combined percentile ranking (0–100). Limitless races excluded.</div>
{trade_swing_table(top_wins)}"""

    return f"""<div class="subtitle">Team Picks · Lineups, trades &amp; transfer analysis</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{top_driver.split('. ')[-1] if '. ' in top_driver else top_driver}</div><div class="mc-lbl">Most picked driver ({top_driver_cnt}×)</div></div>
  <div class="mc"><div class="mc-val">{top_con}</div><div class="mc-lbl">Most picked constructor ({top_con_cnt}×)</div></div>
  <div class="mc"><div class="mc-val">{most_active.split(' ')[0] if most_active != '—' else '—'}</div><div class="mc-lbl">Most active trader ({most_active_cnt} moves)</div></div>
  <div class="mc"><div class="mc-val">{least_active.split(' ')[0] if least_active != '—' else '—'}</div><div class="mc-lbl">Most loyal ({least_active_cnt} moves)</div></div>
</div>

<div class="section-label">Lineup viewer</div>
<div style="display:flex;gap:16px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
  <div style="display:flex;gap:12px;align-items:center">
    <label style="font-size:13px">Race</label>
    <select id="raceSel" onchange="ucPicksRace(this.value)">{race_options}</select>
  </div>
  <div style="display:flex;gap:12px;align-items:center">
    <label style="font-size:13px">Manager</label>
    <select id="teamSel" onchange="ucPicksTeam(raceSel.value, this.value)"></select>
  </div>
</div>
<div id="picks-team-display"></div>

<div class="section-label">Transfers</div>
<div class="hint" style="margin-bottom:12px">Which managers made moves between races. Wildcard and Limitless weekends are flagged.</div>
<div style="display:flex;gap:12px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
  <label style="font-size:13px">Into race</label>
  <select id="tradeRaceSel">{trade_race_opts}</select>
</div>
<div id="picks-trades-display"></div>

<div class="section-label">Transfer impact overview</div>
<div class="hint" style="margin-bottom:12px">How many good and bad transfers has each manager made, and what's been the overall impact throughout {SEASON}?</div>
<div class="toggle-group" id="impactOverviewToggle" style="margin-bottom:1rem">
  <button type="button" class="toggle-btn active" data-metric="pts">Points impact</button>
  <button type="button" class="toggle-btn" data-metric="bud">Budget impact</button>
</div>
<div class="card" style="padding:12px 16px">
  <div id="impactOverviewChartWrap" style="position:relative">
    <div id="impactOverviewLabels" style="position:absolute;top:0;left:0;width:118px;height:100%;pointer-events:none"></div>
    <canvas id="impactOverviewChart"></canvas>
  </div>
</div>

<div class="section-label">Transfer impact per race</div>
<div class="hint" style="margin-bottom:12px">What impact did the transfers you made each race have on your budget and total score? Each bar is one trade, showing the net swing of bought vs. sold.</div>
<div style="display:flex;gap:16px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
  <div style="display:flex;gap:12px;align-items:center">
    <label style="font-size:13px">Manager</label>
    <select id="impactManagerSel">{impact_manager_opts}</select>
  </div>
  <div class="toggle-group" id="impactPerRaceToggle">
    <button type="button" class="toggle-btn active" data-metric="pts">Points impact</button>
    <button type="button" class="toggle-btn" data-metric="bud">Budget impact</button>
  </div>
</div>
<div class="card" style="padding:12px 16px">
  <div id="impactPerRaceChartWrap" style="position:relative"><canvas id="impactPerRaceChart"></canvas></div>
  <div id="impactPerRaceEmpty" style="display:none;padding:1rem;color:#555;font-size:13px;text-align:center">No transfers recorded yet for this manager.</div>
</div>

<div class="section-label">Driver &amp; constructor popularity</div>
<div class="hint" style="margin-bottom:12px">How many managers picked each driver/constructor for the selected race, with their actual points. Updates with the race selector above.</div>
<div class="pop-grid">
  <div>
    <div style="font-size:11px;color:#888;font-weight:500;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Drivers</div>
    <div class="card" style="padding:12px 16px"><div id="picks-driver-pop"></div></div>
  </div>
  <div>
    <div style="font-size:11px;color:#888;font-weight:500;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Constructors</div>
    <div class="card" style="padding:12px 16px"><div id="picks-con-pop"></div></div>
  </div>
</div>

{regret_html}
{wins_html}
{loyalty_html}

<div class="section-label">DRS performance</div>
<div class="hint" style="margin-bottom:12px">Each manager's DRS pick results race by race. Extra DRS chip races show both picks separately. Hit rate = % of DRS picks that scored positive.</div>
<div id="picks-drs-display"></div>
<script>
(function(){{
const teamsByRace={js(teams_by_race)};
const popByRace={js(popularity_by_race)};
const tradesByRace={js(trades_by_race)};
const drsStats={js(drs_stats)};
const impactByManager={js(impact_by_manager)};
const impactOverview={js(impact_overview)};
const nManagers={n_managers};
const p1Manager={js(M[0]["name"] if M else "")};
const raceSel=document.getElementById('raceSel');
const teamSel=document.getElementById('teamSel');
const tradeRaceSel=document.getElementById('tradeRaceSel');

/* ── Lineup viewer ── */
function showRace(rname){{
  const teams=teamsByRace[rname]||[];
  const prev=teamSel.value||p1Manager;
  teamSel.innerHTML='';
  teams.forEach(t=>{{
    const o=document.createElement('option');
    o.value=t.name;
    o.textContent=t.name+' \u2014 '+t.teamName;
    if(t.name===prev)o.selected=true;
    teamSel.appendChild(o);
  }});
  if(teams.length>0) showTeam(rname, teamSel.value);
  else document.getElementById('picks-team-display').innerHTML=
    '<div style="padding:1rem;color:#555;font-size:13px">No lineup data for this race yet.</div>';
  renderPop(rname);
}}

function showTeam(rname, manName){{
  const teams=teamsByRace[rname]||[];
  const t=teams.find(x=>x.name===manName);
  if(!t){{document.getElementById('picks-team-display').innerHTML='';return;}}
  const chipHtml=t.chip
    ?`<span class="chip-pill" style="background:${{t.chip.bg}};color:${{t.chip.tc}};margin-left:auto">${{t.chip.label}}</span>`:'';
  // Sorted by current-season price value (highest first), then arranged so
  // the 2-column grid reads top-to-bottom within a column before moving to
  // the next column, rather than the grid's default left-to-right/wrap order.
  function bySeasonValue(list){{
    return [...list].sort((a,b)=>(b.seasonValue||0)-(a.seasonValue||0));
  }}
  function toColumnMajor(sorted, cols){{
    const rows=Math.ceil(sorted.length/cols)||1;
    const grid=new Array(rows*cols).fill(null);
    let idx=0;
    for(let c=0;c<cols;c++){{
      for(let r=0;r<rows;r++){{
        if(idx<sorted.length) grid[r*cols+c]=sorted[idx++];
      }}
    }}
    return grid.filter(x=>x!==null);
  }}
  const drivers=toColumnMajor(bySeasonValue(t.picks.filter(p=>!p.isCon)), 2);
  const cons=toColumnMajor(bySeasonValue(t.picks.filter(p=>p.isCon)), 2);
  function pickCard(p){{
    const ptsTxt=p.pts!=null?(p.pts>=0?`+${{p.pts}}`:`${{p.pts}}`):'–';
    const ptsColor=p.pts==null?'#555':p.pts>0?'#4caf50':p.pts<0?'#f44336':'#888';
    const valueTxt=p.value!=null?`${{p.value.toFixed(1)}}m`:'—';
    const drsStyle=p.drs?`border-color:${{t.color}};background:${{t.color}}18`:'';
    const drsLabel=p.drsMarker?`<span style="font-size:10px;font-weight:600;color:${{t.color}};margin-left:4px">${{p.drsMarker}}</span>`:'';
    return `<div class="pick" style="${{drsStyle}}">
      <div class="pick-label">${{p.drs?'<span style="color:'+t.color+'">⚡ DRS</span>':p.isCon?'Constructor':'Driver'}}</div>
      <div class="pick-name">${{p.name}}${{drsLabel}}</div>
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:3px">
        <span class="pick-pts" style="color:${{ptsColor}};margin-top:0">${{ptsTxt}} pts</span>
        <span style="font-size:11px;color:#888">${{valueTxt}}</span>
      </div>
    </div>`;
  }}
  const totalPts=t.racePts;
  const totalTxt=totalPts>=0?`+${{totalPts}}`:`${{totalPts}}`;
  const totalColor=totalPts>0?'#4caf50':totalPts<0?'#f44336':'#888';
  const totalValue=t.picks.reduce((sum,p)=>sum+(p.value||0),0);
  const totalValueTxt=totalValue>0?`${{totalValue.toFixed(1)}}m`:'—';
  document.getElementById('picks-team-display').innerHTML=`
  <div class="team-card" style="margin-bottom:1rem">
    <div class="team-header">
      <div class="team-dot" style="background:${{t.color}}"></div>
      <div><div class="team-name">${{t.name}}</div><div class="team-sub">${{t.teamName}}</div></div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:13px;font-weight:500">${{totalValueTxt}}</div>
        <div style="font-size:9px;color:#666;text-transform:uppercase;letter-spacing:.04em">Team value</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-left:12px">
        ${{chipHtml}}
        <div style="font-size:18px;font-weight:600;color:${{totalColor}}">${{totalTxt}} pts</div>
      </div>
    </div>
    <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;margin:10px 0 6px">Drivers</div>
    <div class="picks-grid">${{drivers.map(pickCard).join('')}}</div>
    <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;margin:10px 0 6px">Constructors</div>
    <div class="picks-grid">${{cons.map(pickCard).join('')}}</div>
  </div>`;
}}

/* ── Trades ── */
function renderTrades(rname){{
  const entries=tradesByRace[rname]||[];
  const el=document.getElementById('picks-trades-display');
  if(!entries.length){{
    el.innerHTML='<div style="color:#555;font-size:13px;padding:8px 0">No transfers for this race.</div>';
    return;
  }}
  const cards=entries.map(e=>{{
    const wcBadge=e.wildcard
      ?`<span class="chip-pill" style="background:#4a1a12;color:#ff7a5a;margin-left:6px">Wildcard</span>`:'';
    const chip=(p,cls,mark)=>`<span class="${{cls}}">${{mark}} ${{p.name}}<span class="trade-type">${{p.isCon?'CON':'DRV'}}</span></span>`;
    const hasTrade=e.outs.length||e.ins.length;
    const rows=hasTrade
      ?`<div class="trade-row">
          <div class="trade-row-group">${{e.outs.map(p=>chip(p,'trade-out','\u2716')).join('')}}</div>
          <div class="trade-row-group">${{e.ins.map(p=>chip(p,'trade-in','\u2714')).join('')}}</div>
        </div>`
      :'';
    const noTrades=!hasTrade
      ?`<div style="font-size:12px;color:#555;margin-top:6px">No changes detected</div>`:'';
    return `<div class="trade-card">
      <div class="team-header" style="margin-bottom:${{hasTrade?'10px':'4px'}}">
        <div class="team-dot" style="background:${{e.color}}"></div>
        <div style="flex:1"><div style="display:flex;align-items:center;flex-wrap:wrap;gap:2px"><span class="team-name">${{e.name}}</span>${{wcBadge}}</div><div class="team-sub">${{e.tradeCount}} transfer${{e.tradeCount!==1?'s':''}}</div></div>
      </div>
      ${{rows}}${{noTrades}}
    </div>`;
  }}).join('');
  el.innerHTML=`<div class="trade-grid">${{cards}}</div>`;
}}
tradeRaceSel.addEventListener('change', function(){{ renderTrades(this.value); }});

/* ── Transfer impact ── */
let impactOverviewChart=null;
function roundUpToStep(v, step){{ return Math.max(step, Math.ceil((v*1.05)/step)*step); }}

function renderImpactOverview(metric){{
  const wrap=document.getElementById('impactOverviewChartWrap');
  const labelsEl=document.getElementById('impactOverviewLabels');
  const rows=impactOverview.map(m=>({{
    name: m.name, color: m.color,
    net:  metric==='pts'?m.netPts:m.netBud,
    good: metric==='pts'?m.goodPts:m.goodBud,
    bad:  metric==='pts'?m.badPts:m.badBud,
  }}));
  wrap.style.height=Math.max(140, rows.length*44)+'px';
  const values=rows.map(r=>r.net);
  const colors=values.map(v=>v>0?'#4caf50':v<0?'#f44336':'#555');
  const step=metric==='pts'?50:2;
  const bound=roundUpToStep(Math.max(1, ...values.map(v=>Math.abs(v))), step);
  const unit=metric==='pts'?' pts':'m';
  if(impactOverviewChart)impactOverviewChart.destroy();
  impactOverviewChart=new Chart(document.getElementById('impactOverviewChart'),{{
    type:'bar',
    data:{{labels:rows.map(()=>''),datasets:[{{data:values,backgroundColor:colors,borderRadius:3,barThickness:18}}]}},
    options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{
        title:ctx=>rows[ctx[0].dataIndex].name,
        label:ctx=>` ${{ctx.parsed.x>=0?'+':''}}${{ctx.parsed.x}}${{unit}}`
      }}}}}},
      scales:{{
        x:{{min:-bound,max:bound,ticks:{{stepSize:step,color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
        y:{{afterFit:s=>{{s.width=118;}},ticks:{{display:false}},grid:{{display:false}},border:{{display:false}}}}
      }},
      animation:{{onComplete:()=>{{
        const ys=impactOverviewChart.scales.y;
        labelsEl.innerHTML=rows.map((r,i)=>{{
          const yPx=ys.getPixelForTick(i);
          return `<div style="position:absolute;left:0;top:${{yPx}}px;transform:translateY(-50%);font-size:12px;color:#ccc;white-space:nowrap;max-width:110px;overflow:hidden;text-overflow:ellipsis">${{r.name}} <span style="color:#4caf50">${{r.good}}✔</span><span style="color:#f44336;margin-left:3px">${{r.bad}}✖</span></div>`;
        }}).join('');
      }}}}
    }}
  }});
}}

let impactPerRaceChart=null;
function renderImpactPerRace(managerName, metric){{
  const trades=impactByManager[managerName]||[];
  const chartWrap=document.getElementById('impactPerRaceChartWrap');
  const emptyEl=document.getElementById('impactPerRaceEmpty');
  const canvas=document.getElementById('impactPerRaceChart');
  if(!trades.length){{
    canvas.style.display='none';
    emptyEl.style.display='block';
    if(impactPerRaceChart){{impactPerRaceChart.destroy();impactPerRaceChart=null;}}
    return;
  }}
  canvas.style.display='block';
  emptyEl.style.display='none';
  chartWrap.style.height=Math.max(140, trades.length*44)+'px';
  const labels=trades.map(t=>t.nextRace);
  const values=trades.map(t=>metric==='pts'?t.ptsMargin:t.budMargin);
  const colors=values.map(v=>v>0?'#4caf50':v<0?'#f44336':'#555');
  const step=metric==='pts'?50:0.5;
  const bound=roundUpToStep(Math.max(1, ...values.map(v=>Math.abs(v))), step);
  const unit=metric==='pts'?' pts':'m';
  if(impactPerRaceChart)impactPerRaceChart.destroy();
  impactPerRaceChart=new Chart(canvas,{{
    type:'bar',
    data:{{labels:labels,datasets:[{{data:values,backgroundColor:colors,borderRadius:3,barThickness:18}}]}},
    options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{
        label:ctx=>` ${{ctx.parsed.x>=0?'+':''}}${{ctx.parsed.x}}${{unit}}`,
        afterLabel:ctx=>{{
          const t=trades[ctx.dataIndex];
          return [`Sold: ${{t.sold.map(p=>p.name).join(', ')}}`,`Bought: ${{t.bought.map(p=>p.name).join(', ')}}`];
        }}
      }}}}}},
      scales:{{
        x:{{min:-bound,max:bound,ticks:{{stepSize:step,color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
        y:{{ticks:{{color:'#ccc',font:{{size:12}}}},grid:{{display:false}}}}
      }}}}
  }});
}}

const impactManagerSel=document.getElementById('impactManagerSel');
let impactPerRaceMetric='pts';
let impactOverviewMetric='pts';
document.getElementById('impactPerRaceToggle').addEventListener('click',function(e){{
  const btn=e.target.closest('.toggle-btn');
  if(!btn)return;
  this.querySelectorAll('.toggle-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  impactPerRaceMetric=btn.dataset.metric;
  renderImpactPerRace(impactManagerSel.value, impactPerRaceMetric);
}});
document.getElementById('impactOverviewToggle').addEventListener('click',function(e){{
  const btn=e.target.closest('.toggle-btn');
  if(!btn)return;
  this.querySelectorAll('.toggle-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  impactOverviewMetric=btn.dataset.metric;
  renderImpactOverview(impactOverviewMetric);
}});
impactManagerSel.addEventListener('change',function(){{renderImpactPerRace(this.value, impactPerRaceMetric);}});

/* ── DRS table ── */
(function(){{
  const el=document.getElementById('picks-drs-display');
  if(!drsStats.length){{el.innerHTML='';return;}}
  const rows=drsStats.map(d=>{{
    const raceRows=d.races.map(r=>{{
      const ptsTxt=r.pts!=null?(r.pts>0?`<span style="color:#4caf50">+${{r.pts}}</span>`:`<span style="color:#f44336">${{r.pts}}</span>`):'–';
      const markerColor=r.marker==='3X'?'#88dd44':'#888';
      return `<div class="drs-race-row">
        <span class="drs-race-name">${{r.race}}</span>
        <span class="drs-pick-name">${{r.pick}} <span style="font-size:10px;color:${{markerColor}};font-weight:600">${{r.marker}}</span></span>
        <span class="drs-pts">${{ptsTxt}}</span>
      </div>`;
    }}).join('');
    const hitColor=d.hitRate>=75?'#4caf50':d.hitRate>=50?'#ffaa44':'#f44336';
    const avgTxt=d.avgPts>=0?`+${{d.avgPts}}`:`${{d.avgPts}}`;
    const avgColor=d.avgPts>0?'#4caf50':d.avgPts<0?'#f44336':'#888';
    return `<div class="drs-manager-card">
      <div class="team-header" style="margin-bottom:8px">
        <div class="team-dot" style="background:${{d.color}}"></div>
        <div class="team-name">${{d.name}}</div>
        <div style="margin-left:auto;display:flex;gap:16px;align-items:center">
          <div style="text-align:right"><div style="font-size:16px;font-weight:600;color:${{hitColor}}">${{d.hitRate}}%</div><div style="font-size:10px;color:#666">hit rate</div></div>
          <div style="text-align:right"><div style="font-size:16px;font-weight:600;color:${{avgColor}}">${{avgTxt}}</div><div style="font-size:10px;color:#666">avg pts</div></div>
        </div>
      </div>
      <div class="drs-races">${{raceRows}}</div>
    </div>`;
  }}).join('');
  el.innerHTML=rows;
}})();

/* ── Popularity bars ── */
function renderPop(rname){{
  const pop=popByRace[rname];
  if(!pop){{
    document.getElementById('picks-driver-pop').innerHTML='';
    document.getElementById('picks-con-pop').innerHTML='';
    return;
  }}
  function barRow(item, maxCnt, isDriver){{
    const pct=Math.round(item.count/maxCnt*100);
    const ptsStr=item.pts!=null?(item.pts>=0?`<span style="color:#4caf50">+${{item.pts}}</span>`:`<span style="color:#f44336">${{item.pts}}</span>`):'';
    const barColor=isDriver?'#378ADD':'#D85A30';
    return `<div class="pop-row">
      <div style="flex:1;min-width:0">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px">
          <div class="pop-name">${{item.name}}</div>
          <div style="font-size:11px;color:#666;white-space:nowrap">${{ptsStr}}</div>
        </div>
        <div class="pop-bar-bg"><div class="pop-bar-fill" style="width:${{pct}}%;background:${{barColor}}"></div></div>
      </div>
      <div class="pop-count" style="white-space:nowrap">${{item.count}}/${{nManagers}}</div>
    </div>`;
  }}
  document.getElementById('picks-driver-pop').innerHTML=pop.drivers.map(d=>barRow(d,pop.maxD,true)).join('');
  document.getElementById('picks-con-pop').innerHTML=pop.cons.map(c=>barRow(c,pop.maxC,false)).join('');
}}

window.ucPicksRace=showRace;
window.ucPicksTeam=showTeam;
showRace(raceSel.value);
renderTrades(tradeRaceSel.value);
renderImpactOverview(impactOverviewMetric);
renderImpactPerRace(impactManagerSel.value, impactPerRaceMetric);
}})();
</script>"""




# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — ASSEMBLE COMBINED HTML
# ─────────────────────────────────────────────────────────────────────────────

SHARED_CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e8e8e6; min-height: 100vh; }
  /* Dark-themed scrollbars — desktop browsers otherwise show a light system
     scrollbar on the horizontally-scrolling tables/charts, which looks out
     of place against the dark theme. Mobile browsers mostly use overlay
     scrollbars already, so this is a desktop-only visual fix. */
  * { scrollbar-width: thin; scrollbar-color: #3a3a3a #1a1a1a; }
  ::-webkit-scrollbar { height: 9px; width: 9px; }
  ::-webkit-scrollbar-track { background: #1a1a1a; }
  ::-webkit-scrollbar-thumb { background: #3a3a3a; border-radius: 5px; }
  ::-webkit-scrollbar-thumb:hover { background: #4a4a4a; }
  .site-header { position: sticky; top: 0; z-index: 200; background: #0f0f0f; border-bottom: 1px solid #1e1e1e; }
  .header-inner { max-width: 900px; margin: 0 auto; padding: 0 1.5rem; }
  .header-top { display: flex; align-items: center; gap: 10px; padding: 10px 0 8px; border-bottom: 1px solid #1a1a1a; }
  .header-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
  .header-badge { margin-left: auto; font-size: 10px; color: #666; background: #1a1a1a; border: 0.5px solid #2a2a2a; padding: 2px 8px; border-radius: 20px; }
  .tab-nav { display: flex; gap: 2px; overflow-x: auto; padding: 6px 0; scrollbar-width: none; }
  .tab-nav::-webkit-scrollbar { display: none; }
  .tab-btn { flex-shrink: 0; background: transparent; border: none; color: #555; font-size: 12px; font-weight: 500; font-family: inherit; padding: 5px 12px; border-radius: 6px; cursor: pointer; transition: color 0.12s, background 0.12s; white-space: nowrap; }
  .tab-btn:hover { color: #aaa; background: #1a1a1a; }
  .tab-btn.active { color: #e8e8e6; background: #1e1e1e; border: 0.5px solid #2a2a2a; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .panel-body { max-width: 860px; margin: 0 auto; padding: 1.5rem; }

  /* shared dashboard styles */
  h1 { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #888; margin-bottom: 1.5rem; }
  .section-label { font-size: 11px; font-weight: 500; color: #888; letter-spacing: .05em; text-transform: uppercase; margin-bottom: 8px; margin-top: 1.5rem; }
  .metric-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 8px; margin-bottom: 1.5rem; }
  .mc { background: #1a1a1a; border-radius: 8px; padding: 12px 14px; border: 0.5px solid #2a2a2a; }
  .mc-val { font-size: 20px; font-weight: 500; }
  .mc-lbl { font-size: 11px; color: #888; margin-top: 2px; }
  .card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 4px 16px; margin-bottom: 1rem; }
  .hint { font-size: 11px; color: #555; margin-top: 6px; }
  .legend { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .legend span { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #888; }
  .chip-pill { display: inline-block; font-size: 10px; padding: 1px 7px; border-radius: 20px; font-weight: 500; margin-left: 4px; }
  .bar-bg { height: 4px; background: #2a2a2a; border-radius: 2px; overflow: hidden; margin-top: 5px; }
  .bar-fill { height: 100%; border-radius: 2px; }
  /* scrollable wrappers for wide tables */
  .scroll-x { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .toggle-group { display: inline-flex; gap: 4px; background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 8px; padding: 3px; }
  .toggle-btn { background: transparent; border: none; color: #888; font-size: 12px; font-weight: 500; font-family: inherit; padding: 5px 12px; border-radius: 6px; cursor: pointer; }
  .toggle-btn.active { background: #2a2a2a; color: #e8e8e6; }
  /* mobile */
  @media (max-width: 600px) {
    .panel-body { padding: 1rem; }
    .metric-grid { grid-template-columns: repeat(2,1fr); }
    .hl-grid { grid-template-columns: 1fr !important; }
    .mc-val { font-size: 16px; }
  }
  /* leaderboard */
  .row { display: grid; grid-template-columns: 26px 1fr 52px 52px; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 0.5px solid #2a2a2a; }
  .row:last-child { border-bottom: none; }
  .pos-badge { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 500; flex-shrink: 0; }
  .manager-name { font-size: 14px; font-weight: 500; line-height: 1.3; }
  .team-name { font-size: 11px; color: #888; }
  .gap-col { font-size: 11px; color: #888; text-align: right; white-space: nowrap; }
  .pts-col { font-size: 15px; font-weight: 500; text-align: right; white-space: nowrap; }
  /* race breakdown */
  .race-card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 10px; }
  .race-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
  .race-title { font-size: 15px; font-weight: 500; }
  .race-round { font-size: 11px; color: #888; }
  .podium-strip { display: grid; grid-template-columns: repeat(3,1fr); gap: 6px; margin-bottom: 12px; }
  .slot { border-radius: 8px; padding: 6px 8px; display: flex; align-items: center; gap: 8px; min-width: 0; }
  .medal { width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 500; flex-shrink: 0; }
  .slot-name { font-size: 13px; font-weight: 500; }
  .slot-pts { font-size: 11px; color: #888; }
  .score-row { display: grid; grid-template-columns: 26px 1fr 44px; align-items: center; gap: 10px; padding: 5px 0; border-bottom: 0.5px solid #2a2a2a; }
  .score-row:last-child { border-bottom: none; }
  .pos-dot { width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 500; flex-shrink: 0; }
  .score-name { font-size: 13px; font-weight: 500; }
  .score-pts { font-size: 13px; font-weight: 500; text-align: right; }
  .tag { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 20px; background: #1e1e1e; color: #888; margin-right: 6px; margin-top: 8px; border: 0.5px solid #2a2a2a; }
  /* h2h */
  .vs-header { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px; margin-bottom: 1.5rem; }
  .vs-card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 1rem 1.25rem; text-align: center; }
  .vs-card.winner { border-width: 2px; }
  .vs-name { font-size: 18px; font-weight: 500; }
  .vs-team { font-size: 11px; color: #888; margin-top: 2px; }
  .vs-pts { font-size: 28px; font-weight: 500; margin-top: 8px; }
  .vs-pos { font-size: 12px; color: #888; margin-top: 2px; }
  .vs-divider { font-size: 14px; font-weight: 500; color: #888; text-align: center; }
  .stat-grid { display: grid; grid-template-columns: 1fr auto 1fr; gap: 6px; margin-bottom: 1.5rem; }
  .stat-val { background: #1a1a1a; border-radius: 8px; padding: 8px 12px; font-size: 14px; font-weight: 500; text-align: center; border: 0.5px solid #2a2a2a; }
  .stat-val.hi { border-width: 1.5px; }
  .stat-label { background: #111; border-radius: 8px; padding: 8px 12px; font-size: 11px; color: #888; text-align: center; display: flex; align-items: center; justify-content: center; }
  .race-row { display: grid; grid-template-columns: 1fr 80px 1fr; align-items: center; gap: 8px; padding: 10px 0; border-bottom: 0.5px solid #2a2a2a; }
  .race-row:last-child { border-bottom: none; }
  .race-label { font-size: 12px; color: #888; text-align: center; }
  .chip-row { display: grid; grid-template-columns: 1fr 1fr 1fr; align-items: center; gap: 6px; padding: 8px 0; border-bottom: 0.5px solid #2a2a2a; }
  .chip-row:last-child { border-bottom: none; }
  .tick { width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; margin: 0 auto; }
  select { font-size: 13px; padding: 6px 10px; border-radius: 8px; border: 0.5px solid #333; background: #1a1a1a; color: #e8e8e6; cursor: pointer; }
  label { font-size: 13px; color: #888; }
  /* budget */
  .brow { display: grid; grid-template-columns: 26px 1fr 52px 60px 60px; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 0.5px solid #2a2a2a; }
  .brow:last-child { border-bottom: none; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .bname { font-size: 13px; font-weight: 500; }
  .bcol { font-size: 13px; text-align: right; white-space: nowrap; }
  .col-hdr { font-size: 10px; color: #555; text-align: right; white-space: nowrap; }
  .pos { color: #1D9E75; } .neg { color: #E24B4A; } .neu { color: #888; }
  /* stats — .srow grid-template-columns injected dynamically by panel_stats */
  .srow:last-child { border-bottom: none; }
  .sname { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .fin { width: 24px; height: 24px; border-radius: 4px; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 500; }
  .f0 { background: #1e1e1e; color: #444; } .f1 { background: #FFD700; color: #7a5800; } .f2 { background: #C0C0C0; color: #4a4a4a; } .f3 { background: #CD7F32; color: #5a2d00; } .fn { background: #1e1e1e; color: #888; }
  .total-pts { font-size: 12px; font-weight: 500; text-align: right; }
  .hl-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 10px; margin-bottom: 1rem; }
  .hl-card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 8px; padding: 10px 14px; }
  .hl-title { font-size: 12px; color: #888; margin-bottom: 6px; }
  .hl-row { display: flex; align-items: center; gap: 10px; }
  .hl-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .hl-name { font-size: 14px; font-weight: 500; }
  .hl-val { font-size: 13px; color: #888; }
  /* positions */
  .change-row { display: grid; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 0.5px solid #2a2a2a; }
  .change-row:last-child { border-bottom: none; }
  .mname { font-size: 13px; font-weight: 500; }
  .pos-badge { width: 26px; height: 26px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 500; background: #1e1e1e; color: #888; }
  .net-badge { font-size: 12px; font-weight: 500; text-align: right; }
  /* team picks */
  .team-card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 10px; }
  .team-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .team-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .team-name { font-size: 14px; font-weight: 500; }
  .team-sub { font-size: 11px; color: #888; }
  /* Fixed 2 columns (not auto-fill) so the JS can reliably arrange picks
     top-to-bottom-then-left-to-right by season points — a responsive
     column count would make that ordering meaningless. */
  .picks-grid { display: grid; grid-template-columns: repeat(2,1fr); gap: 6px; }
  .pick { border-radius: 8px; padding: 7px 10px; background: #111; border: 0.5px solid #2a2a2a; }
  .pick-label { font-size: 9px; color: #555; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 2px; }
  .pick-name { font-size: 12px; font-weight: 500; }
  .pick-pts { font-size: 12px; font-weight: 500; margin-top: 3px; }
  .drs-badge { display: inline-block; font-size: 9px; padding: 1px 5px; border-radius: 10px; background: #FFD700; color: #7a5800; font-weight: 500; margin-left: 4px; }
  /* trades */
  .trade-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 10px; }
  .trade-card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 12px 14px; }
  .trade-row { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; padding: 4px 0; font-size: 12px; }
  .trade-row-group { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
  .trade-type { font-size: 9px; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: .04em; margin-left: 5px; }
  .trade-out { color: #f44336; }
  .trade-in { color: #4caf50; }
  .trade-arrow { color: #444; flex-shrink: 0; }
  /* DRS performance */
  .drs-manager-card { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
  .drs-races { border-top: 0.5px solid #2a2a2a; margin-top: 8px; padding-top: 6px; }
  .drs-race-row { display: grid; grid-template-columns: 90px 1fr 50px; align-items: center; gap: 8px; padding: 4px 0; border-bottom: 0.5px solid #1e1e1e; font-size: 12px; }
  .drs-race-row:last-child { border-bottom: none; }
  .drs-race-name { color: #888; font-size: 11px; }
  .drs-pick-name { font-weight: 500; }
  .drs-pts { text-align: right; font-weight: 500; }
  /* regrets */
  .regret-table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 620px; margin-bottom: 1rem; }
  .regret-table th { text-align: left; font-size: 9px; color: #555; text-transform: uppercase; letter-spacing: .04em; padding: 6px 10px; border-bottom: 0.5px solid #2a2a2a; white-space: nowrap; }
  .regret-table td { padding: 8px 10px; border-bottom: 0.5px solid #2a2a2a; vertical-align: top; }
  .regret-table tr:last-child td { border-bottom: none; }
  /* loyalty */
  .loyalty-item { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 7px 0; border-bottom: 0.5px solid #2a2a2a; font-size: 13px; }
  .loyalty-item:last-child { border-bottom: none; }
  .loyalty-asset { font-weight: 500; }
  .loyalty-holders { font-size: 11px; color: #888; text-align: right; }
  /* popularity */
  .pop-row { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 0.5px solid #2a2a2a; }
  .pop-row:last-child { border-bottom: none; }
  .pop-bar-bg { height: 4px; background: #2a2a2a; border-radius: 2px; overflow: hidden; margin-top: 3px; }
  .pop-bar-fill { height: 100%; border-radius: 2px; }
  .pop-name { font-size: 13px; font-weight: 500; }
  .pop-count { font-size: 12px; color: #888; text-align: right; white-space: nowrap; }
  .pop-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 1.5rem; }
"""

PANELS = [
    ("🏁 Leaderboard",    "leaderboard", panel_leaderboard),
    ("🗓 Race Breakdown",  "race",        panel_race_breakdown),
    ("⚔️ Head-to-Head",   "h2h",         panel_h2h),
    ("💰 Budget Tracker",  "budget",      panel_budget),
    ("🏆 Podiums",         "podiums",     panel_podiums),
    ("📊 Stats",           "stats",       panel_stats),
    ("📈 Positions",       "positions",   panel_positions),
    ("🧩 Team Picks",      "picks",       panel_picks),
]

def build_html(data):
    nd   = data["n_done"]
    nt   = data["n_total"]
    last = data["races_done"][-1]["name"] if data["races_done"] else "Pre-season"

    # NZ timestamp for "last updated" — tries zoneinfo (Python 3.9+), falls back to UTC+12/13
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        now_nz = datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception:
        from datetime import datetime, timezone, timedelta
        now_nz = datetime.now(timezone(timedelta(hours=12)))
    # Use cross-platform formatting (no %-d / %-I which are Linux-only)
    day  = str(now_nz.day)                          # no leading zero
    mon  = now_nz.strftime("%b")
    year = now_nz.strftime("%Y")
    hr   = now_nz.strftime("%I").lstrip("0") or "12"  # 12-hr no leading zero
    mn   = now_nz.strftime("%M")
    ampm = now_nz.strftime("%p").lower()
    updated_str = f"{day} {mon} {year}, {hr}:{mn} {ampm} NZT"

    tab_buttons = ""
    panel_divs  = ""
    for i, (label, slug, fn) in enumerate(PANELS):
        active = " active" if i == 0 else ""
        tab_buttons += f'      <button class="tab-btn{active}" id="tab-{slug}" onclick="showTab(\'{slug}\')">{label}</button>\n'
        try:
            content = fn(data)
        except Exception as e:
            content = f'<div style="padding:2rem;color:#E24B4A">Error generating panel: {e}</div>'
        panel_divs += f'  <div class="tab-panel{active}" id="panel-{slug}">\n    <div class="panel-body">\n{content}\n    </div>\n  </div>\n\n'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>The Undercut Collective — F1 Fantasy {SEASON}</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icons/favicon-64.png" sizes="64x64" type="image/png">
<link rel="icon" href="icons/favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="icons/icon-180.png">
<meta name="theme-color" content="#0f0f0f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Undercut F1">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
{SHARED_CSS}
</style>
</head>
<body>
<div class="site-header">
  <div class="header-inner">
    <div class="header-top">
      <span style="font-size:16px">🏎</span>
      <span class="header-title">The Undercut Collective</span>
      <span class="header-badge">{SEASON} Season · {nd}/{nt} races · Updated {updated_str}</span>
    </div>
    <nav class="tab-nav">
{tab_buttons}    </nav>
  </div>
</div>
<div id="tab-content">
{panel_divs}</div>
<script>
function showTab(slug, fromRestore) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + slug).classList.add('active');
  document.getElementById('tab-' + slug).classList.add('active');
  document.getElementById('tab-' + slug).scrollIntoView({{behavior:'smooth',block:'nearest',inline:'center'}});
  window.scrollTo({{top:0, behavior: fromRestore ? 'instant' : 'smooth'}});
  try {{ sessionStorage.setItem('uc-tab', slug); }} catch(e) {{}}
}}
try {{
  const t = sessionStorage.getItem('uc-tab');
  if (t && document.getElementById('panel-' + t)) showTab(t, true);
}} catch(e) {{}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — GIT COMMIT + PUSH
# ─────────────────────────────────────────────────────────────────────────────

def git_push(last_race, repo_dir):
    if not GITHUB_REMOTE:
        print("Auto-push disabled (GITHUB_REMOTE is empty). Done.")
        return

    msg = f"{COMMIT_MSG_PREFIX} — {last_race}"
    try:
        def run(cmd):
            result = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            return result.stdout.strip()

        run(["git", "add", "index.html"])
        run(["git", "commit", "-m", msg])
        run(["git", "push", GITHUB_REMOTE])
        print(f"✅ Pushed to GitHub: '{msg}'")
    except FileNotFoundError:
        print("WARNING: git not found. Install Git from https://git-scm.com and try again.")
    except RuntimeError as e:
        err = str(e)
        if "nothing to commit" in err:
            print("No changes to commit (data unchanged since last push).")
        else:
            print(f"Git error: {err}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Force UTF-8 output on Windows to avoid emoji encoding errors
    import sys, io
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    print("=" * 55)
    print("  The Undercut Collective -- Dashboard Builder")
    print("=" * 55)

    raw  = read_database(DB_PATH)
    data = compute(raw)

    nd   = data["n_done"]
    nt   = data["n_total"]
    last = data["races_done"][-1]["name"] if data["races_done"] else "Pre-season"
    print(f"  Season: {SEASON} | Races complete: {nd}/{nt} | Last: {last}")
    print(f"  Managers: {', '.join(m['name'] for m in data['managers_sorted'])}")
    print()

    html = build_html(data)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    size = OUTPUT_FILE.stat().st_size // 1024
    print(f"✅ Dashboard written: {OUTPUT_FILE} ({size} KB)")
    print()

    git_push(last, REPO_DIR)

    print()
    print("All done! Your friends can view the site at your GitHub Pages URL.")
    pause("Press Enter to close...")


if __name__ == "__main__":
    main()
