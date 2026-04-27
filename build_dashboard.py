#!/usr/bin/env python3
"""
The Undercut Collective — F1 Fantasy Dashboard Builder
=======================================================
Reads your Excel workbook and generates a combined HTML dashboard,
then commits and pushes it to GitHub Pages.

Run this after every race:  python build_dashboard.py
Or just double-click it in File Explorer.

Requirements (install once):
    pip install openpyxl
"""

import sys
import os
import re
import subprocess
from pathlib import Path

# ── Try importing openpyxl, give clear instructions if missing ────────────────
try:
    import openpyxl
except ImportError:
    print("ERROR: openpyxl is not installed.")
    print("Fix:   Open a terminal and run:  pip install openpyxl")
    input("\nPress Enter to exit...")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these if paths change
# ─────────────────────────────────────────────────────────────────────────────

WORKBOOK_PATH = r"C:\Users\tim\OneDrive\Documents\Excel\the-undercut-collective\The Undercut Collective F1 Fantasy League.xlsx"

# Folder where this script lives (= your GitHub repo root)
REPO_DIR = Path(__file__).parent

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
    "Limitless":   {"bg": "#d9e6f7", "tc": "#0C447C"},
    "Wildcard":    {"bg": "#efdcd9", "tc": "#712B13"},
    "Final Fix":   {"bg": "#f7e9d9", "tc": "#633806"},
    "Auto Pilot":  {"bg": "#d9f3ed", "tc": "#085041"},
    "No Negative": {"bg": "#e7e0fa", "tc": "#3C3489"},
    "Extra DRS":   {"bg": "#e5f1d9", "tc": "#27500A"},
}

CHIP_ORDER = ["Limitless", "Wildcard", "Final Fix", "Auto Pilot", "No Negative", "Extra DRS"]

DASH_PATTERNS = [[], [6,2], [2,2], [8,3], [4,2], [6,2,2,2], [3,3], [8,2,2,2], [1,2]]

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — READ EXCEL
# ─────────────────────────────────────────────────────────────────────────────

def read_workbook(path):
    print(f"Reading workbook: {path}")
    if not Path(path).exists():
        print(f"\nERROR: Workbook not found at:\n  {path}")
        print("Check the WORKBOOK_PATH setting at the top of this script.")
        input("\nPress Enter to exit...")
        sys.exit(1)

    wb = openpyxl.load_workbook(path, data_only=True)
    data = {}

    # ── Races sheet (sheet index 0) ──────────────────────────────────────────
    ws = wb.worksheets[0]   # Races

    # Row 2 = headers. Even columns H onwards = race scores (col 8, 10, 12...)
    # Row 1 = round numbers in even cols.  Row 2 = race names in even cols.
    # Cols: A=Team, B=Name, C=Total, D=Rank, E=Interval, F=Gap, G=Avg, H onwards = scores

    # Build race list from row 2 (col 8 = H onwards, every 2)
    races = []       # list of {"round": int, "name": str, "col": int}
    for col in range(8, ws.max_column + 1, 2):
        name = ws.cell(row=2, column=col).value
        rnd  = ws.cell(row=1, column=col).value
        if name and rnd:
            races.append({"round": int(rnd), "name": str(name).strip(), "col": col})

    # Managers (rows 3–11)
    managers_raw = []
    for row in range(3, 12):
        name_cell = ws.cell(row=row, column=2).value
        if not name_cell:
            break
        name  = str(name_cell).strip()
        team  = str(ws.cell(row=row, column=1).value or "").strip()
        total = ws.cell(row=row, column=3).value or 0
        rank  = ws.cell(row=row, column=4).value or 0
        avg   = ws.cell(row=row, column=7).value or 0

        scores = {}
        for race in races:
            v = ws.cell(row=row, column=race["col"]).value
            if v is not None and v != "":
                try:
                    scores[race["name"]] = int(float(str(v)))
                except (ValueError, TypeError):
                    pass   # position string in odd col — skip

        managers_raw.append({
            "name":   name,
            "team":   team,
            "total":  int(float(str(total))) if total else 0,
            "rank":   int(rank) if rank else 0,
            "avg":    float(str(avg)) if avg else 0,
            "scores": scores,   # {race_name: pts}
            "color":  MANAGER_COLOURS.get(name, "#888888"),
        })

    # Sort by rank
    managers_raw.sort(key=lambda m: m["rank"])
    data["managers"]    = managers_raw
    data["races"]       = races
    data["races_done"]  = [r for r in races if any(
        m["scores"].get(r["name"]) is not None for m in managers_raw)]

    # ── Podiums sheet (index 1) ──────────────────────────────────────────────
    # Only include races that exist in the Races sheet (filters out cancelled races)
    valid_race_names = {r["name"] for r in races}
    ws_pod = wb.worksheets[1]
    podiums = []
    for row in range(2, ws_pod.max_row + 1):
        race_name = ws_pod.cell(row=row, column=1).value
        if not race_name:
            continue
        race_name = str(race_name).strip()
        if race_name not in valid_race_names:
            continue   # skip cancelled / removed races
        p1 = ws_pod.cell(row=row, column=2).value
        p2 = ws_pod.cell(row=row, column=3).value
        p3 = ws_pod.cell(row=row, column=4).value
        podiums.append({
            "race":   race_name,
            "first":  str(p1).strip() if p1 else "",
            "second": str(p2).strip() if p2 else "",
            "third":  str(p3).strip() if p3 else "",
        })
    data["podiums"] = podiums

    # ── Stats sheet (index 2) — finish distribution + chip usage ────────────
    ws_stats = wb.worksheets[2]
    finish_dist = {}     # {name: [p1,p2,...,p9]}
    chips_used  = {}     # {name: {chip_name: bool}}

    # Rows 2–10: finish distribution
    for row in range(2, 11):
        name = ws_stats.cell(row=row, column=2).value
        if not name:
            break
        name = str(name).strip()
        finishes = []
        for col in range(3, 12):   # cols C–K = P1–P9
            v = ws_stats.cell(row=row, column=col).value
            finishes.append(int(v) if v else 0)
        finish_dist[name] = finishes

    # Chip usage: header row 12, data rows 13+
    # Col B=Name, C=Limitless, D=Wildcard, E=Final Fix, F=Auto Pilot, G=No Negative, H=Extra DRS
    chip_header_row = None
    for row in range(11, 25):
        if ws_stats.cell(row=row, column=1).value == "CHIPS USED":
            chip_header_row = row
            break

    if chip_header_row:
        chip_cols = {}   # {col: chip_name}
        for col in range(3, 9):
            hdr = ws_stats.cell(row=chip_header_row, column=col).value
            if hdr:
                chip_cols[col] = str(hdr).strip()

        for row in range(chip_header_row + 1, chip_header_row + 12):
            name = ws_stats.cell(row=row, column=2).value
            if not name:
                continue
            name = str(name).strip()
            used = {}
            for col, chip in chip_cols.items():
                v = ws_stats.cell(row=row, column=col).value
                used[chip] = bool(v and str(v).strip().upper() == "X")
            chips_used[name] = used

    data["finish_dist"] = finish_dist
    data["chips_used"]  = chips_used

    # ── Driver Changes sheet (index 3) — lineups ────────────────────────────
    ws_dc = wb.worksheets[3]

    # Row 2: headers — col C = first race name, H = second race name, etc (every 6 cols? no)
    # Actual layout: A=Team, B=Name, C=driver/constructor name, D=2X (DRS marker), E=Budget+-, F=Value, G=#
    # Then H = China picks, I = China #, J = Japan picks, ...
    # Each race is a separate column group.

    # Build lineup_races from row 2 headers
    lineup_race_cols = {}   # {race_name: col_index}
    for col in range(3, ws_dc.max_column + 1):
        hdr = ws_dc.cell(row=2, column=col).value
        if hdr and str(hdr).strip() not in ("2X", "Budget +-", "Value", "#", ""):
            # Only grab columns where header is a race name (not a sub-column)
            # Race cols appear to be C(3), H(8), J(10), L(12) etc in this sheet
            pass

    # Simpler: just read col C (first race = Australia) for all teams
    # Structure: 7 rows per team (5 drivers + 2 constructors)
    lineups = {}  # {manager_name: {race_name: {drivers:[...], constructors:[...], drs:""}}}

    # Find all race columns: row 2, cols 3 onwards, skip sub-columns
    # Sub-columns after each race: col+1="#", col+2 might be "Budget+-" etc (only after first race)
    # From XML: C=Australia, H=China, J=Japan, L=Bahrain... (col 3, 8, 10, 12, ...)
    # Let's detect by reading row 2 and skipping known sub-col names
    skip_hdrs = {"2X", "Budget +-", "Value", "#", "", None}
    race_cols_dc = []
    for col in range(3, ws_dc.max_column + 1):
        hdr = ws_dc.cell(row=2, column=col).value
        if hdr and str(hdr).strip() not in skip_hdrs:
            race_cols_dc.append((str(hdr).strip(), col))

    # Read picks per manager (7 rows per manager)
    row = 3
    while row <= ws_dc.max_row:
        team = ws_dc.cell(row=row, column=1).value
        name = ws_dc.cell(row=row, column=2).value
        if not team or not name:
            row += 1
            continue
        name = str(name).strip()
        if name not in lineups:
            lineups[name] = {}

        # Read 7 rows for this manager (5 drivers + 2 constructors)
        for race_name, col in race_cols_dc:
            picks = []
            for r in range(row, row + 7):
                pick_name = ws_dc.cell(row=r, column=col).value
                drs_marker = ws_dc.cell(row=r, column=col + 1).value if col + 1 <= ws_dc.max_column else None
                if pick_name and str(pick_name).strip():
                    picks.append({
                        "name": str(pick_name).strip(),
                        "drs":  bool(drs_marker and str(drs_marker).strip() == "2X"),
                    })
            if picks:
                lineups[name][race_name] = picks

        row += 7   # advance to next manager

    data["lineups"] = lineups

    # ── Reference Tables sheet (index 9) — budgets ──────────────────────────
    ws_ref = wb.worksheets[9]

    # Budget section starts at row 41 based on XML analysis
    # Row 41 = header: Name, Pre-Season, Australia, China, ...
    # Rows 42–50 = one per manager
    budget_header_row = None
    for row in range(38, 55):
        cell = ws_ref.cell(row=row, column=1).value
        if cell and str(cell).strip() == "Name":
            # Check if next col is "Pre-Season"
            next_cell = ws_ref.cell(row=row, column=2).value
            if next_cell and "Pre" in str(next_cell):
                budget_header_row = row
                break

    budgets = {}   # {name: [pre_season, r1, r2, ...]}
    budget_race_names = []

    if budget_header_row:
        # Read race headers from row
        for col in range(2, ws_ref.max_column + 1):
            hdr = ws_ref.cell(row=budget_header_row, column=col).value
            if hdr:
                budget_race_names.append(str(hdr).strip())
            else:
                break

        for row in range(budget_header_row + 1, budget_header_row + 12):
            name = ws_ref.cell(row=row, column=1).value
            if not name:
                continue
            name = str(name).strip()
            vals = []
            for col in range(2, 2 + len(budget_race_names)):
                v = ws_ref.cell(row=row, column=col).value
                if v is not None and v != "":
                    try:
                        vals.append(round(float(str(v)), 2))
                    except (ValueError, TypeError):
                        break
                else:
                    break
            if vals:
                budgets[name] = vals

    data["budgets"]            = budgets
    data["budget_race_names"]  = budget_race_names

    # ── Reference Tables sheet — overall standings positions per race ─────────
    # Rows 14+: Name | Australia | China | Japan | ...  (overall leaderboard pos)
    ws_ref2 = wb.worksheets[9]
    pos_header_row = None
    for r in range(12, 30):
        cell_val = ws_ref2.cell(row=r, column=1).value
        if cell_val and str(cell_val).strip() == "Name":
            next_val = ws_ref2.cell(row=r, column=2).value
            if next_val and str(next_val).strip() not in ("Pre-Season", "Budget", ""):
                pos_header_row = r
                break

    pos_race_names = []
    standings_positions = {}   # {name: [pos_r1, pos_r2, ...]}
    if pos_header_row:
        for col in range(2, ws_ref2.max_column + 1):
            hdr = ws_ref2.cell(row=pos_header_row, column=col).value
            if hdr and str(hdr).strip():
                pos_race_names.append(str(hdr).strip())
            else:
                break
        for r in range(pos_header_row + 1, pos_header_row + 15):
            nm = ws_ref2.cell(row=r, column=1).value
            if not nm:
                continue
            nm = str(nm).strip()
            vals = []
            for col in range(2, 2 + len(pos_race_names)):
                v = ws_ref2.cell(row=r, column=col).value
                try:
                    vals.append(int(float(str(v))) if v is not None else None)
                except (ValueError, TypeError):
                    vals.append(None)
            standings_positions[nm] = vals

    data["pos_race_names"]       = pos_race_names
    data["standings_positions"]  = standings_positions

    return data


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — COMPUTE DERIVED DATA
# ─────────────────────────────────────────────────────────────────────────────

def compute(data):
    managers    = data["managers"]
    races_done  = data["races_done"]
    podiums     = data["podiums"]
    finish_dist = data["finish_dist"]
    chips_used  = data["chips_used"]
    budgets     = data["budgets"]
    budget_races= data["budget_race_names"]
    lineups     = data["lineups"]

    n_done = len(races_done)
    n_total = len(data["races"])

    # Sort managers by rank for consistent ordering
    managers_sorted = sorted(managers, key=lambda m: m["rank"])

    # ── Points progression (cumulative per race) ─────────────────────────────
    # data[0] = 0 (pre-season), then cumulative after each completed race
    for m in managers_sorted:
        cum = [0]
        running = 0
        for race in races_done:
            pts = m["scores"].get(race["name"])
            if pts is not None:
                running += pts
            cum.append(running)
        m["cumulative"] = cum

    # ── Per-race rankings ────────────────────────────────────────────────────
    for race in races_done:
        rname = race["name"]
        scores_this_race = [(m["name"], m["scores"].get(rname, 0)) for m in managers_sorted]
        scores_this_race.sort(key=lambda x: -x[1])
        race["ranking"] = scores_this_race  # [(name, pts), ...]
        race["winner"]  = scores_this_race[0][0] if scores_this_race else ""

    # ── Position history — from Reference Tables (read in read_workbook) ──────
    pos_race_names      = data.get("pos_race_names", [])
    standings_positions = data.get("standings_positions", {})

    for m in managers_sorted:
        name = m["name"]
        raw_positions = standings_positions.get(name, [])
        positions = []
        for i, race in enumerate(races_done):
            rname = race["name"]
            if rname in pos_race_names:
                idx = pos_race_names.index(rname)
                if idx < len(raw_positions) and raw_positions[idx] is not None:
                    positions.append(raw_positions[idx])
                else:
                    # fallback: derive from cumulative scores
                    cum_scores = {mm["name"]: sum(mm["scores"].get(rd["name"], 0)
                                  for rd in races_done[:i+1]) for mm in managers_sorted}
                    sorted_cum = sorted(cum_scores.items(), key=lambda x: -x[1])
                    pos_map = {n: j+1 for j, (n, _) in enumerate(sorted_cum)}
                    positions.append(pos_map.get(name))
            else:
                positions.append(None)
        m["positions"] = positions

    # ── Chip usage per manager ───────────────────────────────────────────────
    for m in managers_sorted:
        name = m["name"]
        used = chips_used.get(name, {})
        m["chips"] = {chip: used.get(chip, False) for chip in CHIP_ORDER}
        used_list = [c for c in CHIP_ORDER if m["chips"].get(c)]
        m["chip_label"] = used_list[0] if len(used_list) == 1 else (
                          f"{len(used_list)} chips" if used_list else None)
        m["chip_bg"] = CHIP_STYLES[used_list[0]]["bg"] if len(used_list) == 1 else None
        m["chip_tc"] = CHIP_STYLES[used_list[0]]["tc"] if len(used_list) == 1 else None

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

    # Most consistent = lowest std dev of positions
    import statistics
    def pos_std(m):
        if len(m["positions"]) < 2:
            return 99
        valid = [p for p in m["positions"] if p is not None]
        return statistics.stdev(valid) if len(valid) >= 2 else 99

    most_consistent = min(managers_sorted, key=pos_std)
    pos_desc = ", ".join(f"P{p}" for p in most_consistent["positions"] if p)

    # Biggest swing = largest NET position change R1 -> latest (using overall standings)
    biggest_swing_val = 0
    biggest_swing_m = managers_sorted[0]
    for m in managers_sorted:
        for i in range(1, len(m["positions"])):
            if m["positions"][i] and m["positions"][i-1]:
                swing = abs(m["positions"][i] - m["positions"][i-1])
                if swing > biggest_swing_val:
                    biggest_swing_val = swing
                    biggest_swing_m = m

    data["highlights"] = {
        "best":            best,
        "worst":           worst,
        "best_avg":        best_avg,
        "worst_avg":       worst_avg,
        "most_consistent": (most_consistent, pos_desc),
        "biggest_swing":   (biggest_swing_m, biggest_swing_val),
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
    if isinstance(v, dict): return "{" + ",".join(f"{k}:{js(vv)}" for k, vv in v.items()) + "}"
    return str(v)

def chip_pill(label, bg, tc, small=False):
    size = "9px" if small else "10px"
    pad  = "1px 5px" if small else "1px 7px"
    return (f'<span class="chip-pill" style="background:{bg};color:{tc};'
            f'font-size:{size};padding:{pad}">{label}</span>')


# ── 01 Leaderboard ───────────────────────────────────────────────────────────
def panel_leaderboard(data):
    M   = data["managers_sorted"]
    RD  = data["races_done"]
    NT  = data["n_total"]
    ND  = data["n_done"]

    leader = M[0]["total"] if M else 0
    last   = M[-1]["total"] if M else 0
    gap_span = leader - last

    # Latest podiums line
    pod_cards = ""
    for pod in data["podiums"]:
        if pod["first"] or pod["second"] or pod["third"]:
            pod_cards += (f'<div class="podium-card"><div class="podium-pos">{pod["race"]}</div>'
                          f'<div class="podium-name">1st {pod["first"]} &nbsp; '
                          f'2nd {pod["second"]} &nbsp; 3rd {pod["third"]}</div></div>')

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
        pill  = chip_pill(m["chip_label"], m["chip_bg"], m["chip_tc"]) if m["chip_label"] else ""
        gap   = "Leader" if i == 0 else str(M[i]["total"] - M[0]["total"])
        rows_html += f"""<div class="row">
  <div class="pos-badge" style="{medal}">{i+1}</div>
  <div><div class="manager-name">{m['name']}{pill}</div><div class="team-name">{m['team']}</div>
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

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">F1 Fantasy League — Season Leaderboard · {ND} of {NT} races complete</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{ND}</div><div class="mc-lbl">Races complete</div></div>
  <div class="mc"><div class="mc-val">{NT - ND}</div><div class="mc-lbl">Races remaining</div></div>
  <div class="mc"><div class="mc-val">{leader}</div><div class="mc-lbl">Pts — leader</div></div>
  <div class="mc"><div class="mc-val">{gap_span}</div><div class="mc-lbl">Pts gap P1 to P{len(M)}</div></div>
</div>
<div class="section-label">Latest podiums</div>
<div class="podium-row">{pod_cards}</div>
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

    race_cards_html = ""
    for race in RD:
        rname = race["name"]
        rnd   = race["round"]
        scores_here = [(m, m["scores"].get(rname, 0)) for m in M if rname in m["scores"]]
        scores_here.sort(key=lambda x: -x[1])
        max_pts = scores_here[0][1] if scores_here else 1

        podium_html = ""
        for j in range(min(3, len(scores_here))):
            mm, pts = scores_here[j]
            podium_html += (f'<div class="slot" style="{slot_styles[j]}">'
                            f'<div class="medal" style="{medal_styles[j]}">{j+1}</div>'
                            f'<div><div class="slot-name" style="color:{mm["color"]}">{mm["name"]}</div>'
                            f'<div class="slot-pts">{pts} pts</div></div></div>')

        score_rows = ""
        for j, (mm, pts) in enumerate(scores_here):
            pct = round(max(0, pts) / max_pts * 100) if max_pts > 0 else 0
            bg  = ["#FFD700","#C0C0C0","#CD7F32"][j] if j < 3 else "#2a2a2a"
            tc  = ["#7a5800","#4a4a4a","#5a2d00"][j] if j < 3 else "#888"
            score_rows += (f'<div class="score-row">'
                           f'<div class="pos-dot" style="background:{bg};color:{tc}">{j+1}</div>'
                           f'<div><div class="score-name">{mm["name"]}</div>'
                           f'<div class="bar-bg"><div class="bar-fill" style="width:{pct}%;background:{mm["color"]}"></div></div></div>'
                           f'<div class="score-pts">{pts}</div></div>')

        all_pts   = [s for _, s in scores_here]
        rng       = max(all_pts) - min(all_pts) if all_pts else 0
        r_avg     = round(sum(all_pts) / len(all_pts)) if all_pts else 0
        negatives = sum(1 for s in all_pts if s < 0)
        neg_note  = f"{negatives} went negative" if negatives else "Everyone positive"

        race_cards_html += f"""<div class="race-card">
  <div class="race-header"><span class="race-title">{rname}</span><span class="race-round">Round {rnd}</span></div>
  <div class="podium-strip">{podium_html}</div>
  {score_rows}
  <div><span class="tag">Range: {rng} pts</span><span class="tag">Avg: {r_avg} pts</span><span class="tag">{neg_note}</span></div>
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

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Race Breakdown · {ND} of {NT} races complete</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{ND}</div><div class="mc-lbl">Races complete</div></div>
  <div class="mc"><div class="mc-val">{high}</div><div class="mc-lbl">Highest single race ({high_m})</div></div>
  <div class="mc"><div class="mc-val">{low}</div><div class="mc-lbl">Lowest single race</div></div>
  <div class="mc"><div class="mc-val">{avg}</div><div class="mc-lbl">Avg points per race</div></div>
</div>
<div class="section-label">Round by round</div>
{race_cards_html}
<div class="section-label">Points per race — all managers</div>
<div style="position:relative;height:300px"><canvas id="barChart"></canvas></div>
<div class="legend" id="legend-race"></div>
<script>
(function(){{
new Chart(document.getElementById('barChart'),{{
  type:'bar',
  data:{{labels:{js(race_labels)},datasets:{js(bar_datasets)}}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} pts`}}}}}},
    scales:{{x:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
             y:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}}}}}}
}});
document.getElementById('legend-race').innerHTML={js(legend_html)};
}})();
</script>"""


# ── 03 Head-to-Head ───────────────────────────────────────────────────────────
def panel_h2h(data):
    M  = data["managers_sorted"]
    RD = data["races_done"]

    manager_data_js = {}
    for m in M:
        manager_data_js[m["name"]] = {
            "team":    m["team"],
            "color":   m["color"],
            "pos":     m["rank"],
            "total":   m["total"],
            "races":   [m["scores"].get(r["name"], 0) for r in RD],
            "podiums": [1 if any(
                           pod["race"] == r["name"] and
                           pod[pos_key] == m["name"]
                           for pod in data["podiums"]
                           for pos_key in ["first","second","third"]
                         ) else 0
                        for r in RD],
            "chips":   {c: m["chips"].get(c, False) for c in CHIP_ORDER},
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

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Head-to-Head Comparison</div>
<div style="display:flex;gap:12px;align-items:center;margin-bottom:1.5rem;flex-wrap:wrap">
  <div style="display:flex;align-items:center;gap:8px"><label>Manager A</label><select id="selA" onchange="syncSelects('A')"></select></div>
  <div style="font-size:13px;color:#888;font-weight:500">vs</div>
  <div style="display:flex;align-items:center;gap:8px"><label>Manager B</label><select id="selB" onchange="syncSelects('B')"></select></div>
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
const data={data_js};
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
populateSelect(selA,null,names[0]);
if(names.length>1)populateSelect(selB,names[0],names[1]);
else populateSelect(selB,null,names[0]);
function tickSvg(used,color){{
  if(used)return`<div class="tick" style="background:${{color}}22"><svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M2 6l3 3 5-5" stroke="${{color}}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></div>`;
  return`<div class="tick" style="background:#1e1e1e"><svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M4 4l4 4M8 4l-4 4" stroke="#555" stroke-width="1.5" stroke-linecap="round"/></svg></div>`;
}}
function render(){{
  const nA=selA.value,nB=selB.value,A=data[nA],B=data[nB];
  const aRW=A.races.filter((r,i)=>r>B.races[i]).length,bRW=B.races.filter((r,i)=>r>A.races[i]).length;
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
  document.getElementById('race-rows').innerHTML=raceNames.map((race,i)=>{{
    const ap=A.races[i],bp=B.races[i],aW=ap>bp;
    const aPct=Math.round(Math.abs(ap)/maxP*100),bPct=Math.round(Math.abs(bp)/maxP*100);
    return`<div class="race-row">
      <div style="text-align:right"><div style="font-size:14px;font-weight:500;color:${{aW?A.color:'#888'}}">${{ap}}</div><div style="display:flex;justify-content:flex-end;margin-top:4px"><div style="height:6px;border-radius:3px;width:${{aPct}}%;background:${{A.color}};opacity:${{aW?1:0.4}}"></div></div></div>
      <div class="race-label">${{race}}</div>
      <div><div style="font-size:14px;font-weight:500;color:${{!aW?B.color:'#888'}}">${{bp}}</div><div style="display:flex;margin-top:4px"><div style="height:6px;border-radius:3px;width:${{bPct}}%;background:${{B.color}};opacity:${{!aW?1:0.4}}"></div></div></div></div>`;
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

    # Budget change per race chart
    max_races = max((len(m["budgets"]) for m in M), default=1)
    change_labels = b_races[1:max_races] if len(b_races) >= max_races else []
    change_datasets = []
    for m in M:
        changes = [round(m["budgets"][i] - m["budgets"][i-1], 2)
                   for i in range(1, len(m["budgets"]))]
        change_datasets.append({"label": m["name"], "data": changes,
                                 "backgroundColor": m["color"], "borderWidth": 0})

    legend_html = "".join(
        f'<span><span style="width:10px;height:10px;border-radius:2px;background:{m["color"]};display:inline-block"></span>{m["name"]}</span>'
        for m in M)

    y_min = max(80, int(min_b) - 4)
    y_max = min(120, int(max_b) + 4)

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Budget Tracker · Team values across the season</div>
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
<div style="position:relative;height:300px"><canvas id="changeChart"></canvas></div>
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
new Chart(document.getElementById('changeChart'),{{
  type:'bar',
  data:{{labels:{js(change_labels)},datasets:{js(change_datasets)}}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>{{const v=ctx.parsed.y;return` ${{ctx.dataset.label}}: ${{v>0?'+':''}}${{v.toFixed(1)}}m`;}}}}}}}},
    scales:{{x:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
             y:{{ticks:{{color:'#888',callback:v=>(v>0?'+':'')+v.toFixed(1)+'m'}},grid:{{color:'rgba(255,255,255,0.06)'}}}}}}}}
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

    managers_with_pod = sum(1 for m in M
                            if any(p["first"]==m["name"] or p["second"]==m["name"]
                                   or p["third"]==m["name"] for p in POD))
    diff_winners = len(set(p["first"] for p in POD if p["first"]))

    medal_styles = ["background:#FFD700;color:#7a5800",
                    "background:#C0C0C0;color:#4a4a4a",
                    "background:#CD7F32;color:#5a2d00"]
    slot_styles  = ["background:#2a2200;border:0.5px solid #FFD700",
                    "background:#222;border:0.5px solid #C0C0C0",
                    "background:#221a12;border:0.5px solid #CD7F32"]

    def get_color(name):
        return MANAGER_COLOURS.get(name, "#888")

    cards_html = ""
    for pod in POD:
        filled = pod["first"] or pod["second"] or pod["third"]
        style = "" if filled else 'style="border-style:dashed;opacity:0.5"'
        slots = ""
        for j, (key, pts_key) in enumerate([("first",""), ("second",""), ("third","")]):
            name = pod[key]
            if name:
                # Find points for this manager in this race
                pts = next((m["scores"].get(pod["race"], "") for m in M if m["name"] == name), "")
                pts_html = f'<div class="slot-pts">{pts} pts</div>' if pts != "" else ""
                slots += (f'<div class="slot" style="{slot_styles[j]}">'
                          f'<div class="medal" style="{medal_styles[j]}">{j+1}</div>'
                          f'<div><div class="slot-name" style="color:{get_color(name)}">{name}</div>{pts_html}</div></div>')
            else:
                slots += (f'<div class="slot" style="{slot_styles[j]}">'
                          f'<div class="medal" style="{medal_styles[j]}">{j+1}</div>'
                          f'<div class="tbd">TBC</div></div>')
        cards_html += f"""<div class="race-podium" {style}>
  <div class="race-header"><span class="race-title">{pod['race']}</span></div>
  <div class="podium-slots">{slots}</div>
</div>"""

    # Podium share chart
    p1 = [sum(1 for p in POD if p["first"]==m["name"]) for m in M]
    p2 = [sum(1 for p in POD if p["second"]==m["name"]) for m in M]
    p3 = [sum(1 for p in POD if p["third"]==m["name"]) for m in M]
    names_labels = [m["name"] for m in M]
    max_y = max(max(p1+p2+p3, default=0) + 1, 3)

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Podiums · Race results &amp; podium share</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{ND}</div><div class="mc-lbl">Races complete</div></div>
  <div class="mc"><div class="mc-val">{managers_with_pod}</div><div class="mc-lbl">Managers with a podium</div></div>
  <div class="mc"><div class="mc-val">{diff_winners}</div><div class="mc-lbl">Different race winners</div></div>
  <div class="mc"><div class="mc-val">{len(M) - managers_with_pod}</div><div class="mc-lbl">Managers without a podium</div></div>
</div>
<div class="section-label">Race results</div>
{cards_html}
<div class="section-label">Podium share</div>
<div style="position:relative;height:260px"><canvas id="podiumChart"></canvas></div>
<div class="legend" id="legend-podiums"></div>
<script>
(function(){{
new Chart(document.getElementById('podiumChart'),{{
  type:'bar',
  data:{{labels:{js(names_labels)},datasets:[
    {{label:'1st',data:{js(p1)},backgroundColor:'#FFD700',borderWidth:0}},
    {{label:'2nd',data:{js(p2)},backgroundColor:'#C0C0C0',borderWidth:0}},
    {{label:'3rd',data:{js(p3)},backgroundColor:'#CD7F32',borderWidth:0}}
  ]}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y}}`}}}}}},
    scales:{{x:{{stacked:true,ticks:{{color:'#888'}},grid:{{display:false}}}},
             y:{{stacked:true,max:{max_y},ticks:{{color:'#888',stepSize:1}},grid:{{color:'rgba(255,255,255,0.06)'}}}}}}}}
}});
document.getElementById('legend-podiums').innerHTML=`
  <span><span style="width:10px;height:10px;border-radius:2px;background:#FFD700;display:inline-block"></span>1st place</span>
  <span><span style="width:10px;height:10px;border-radius:2px;background:#C0C0C0;display:inline-block"></span>2nd place</span>
  <span><span style="width:10px;height:10px;border-radius:2px;background:#CD7F32;display:inline-block"></span>3rd place</span>`;
}})();
</script>"""


# ── 06 Stats ─────────────────────────────────────────────────────────────────
def panel_stats(data):
    M   = data["managers_sorted"]
    ND  = data["n_done"]
    HL  = data["highlights"]

    total_race_scores = sum(len(m["scores"]) for m in M)
    chips_used_count  = sum(1 for m in M for c in CHIP_ORDER if m["chips"].get(c))

    # Finish distribution table
    pos_headers = "".join(
        f'<div style="font-size:9px;color:#555;text-align:center">P{i}</div>'
        for i in range(1, 10))
    finish_rows_html = ""
    for m in M:
        fd = data["finish_dist"].get(m["name"], [0]*9)
        pill = chip_pill(m["chip_label"], m["chip_bg"], m["chip_tc"], small=True) if m["chip_label"] else ""
        cells = ""
        for i, v in enumerate(fd):
            if v == 0:
                cls = "f0"
            elif i == 0: cls = "f1"
            elif i == 1: cls = "f2"
            elif i == 2: cls = "f3"
            else:        cls = "fn"
            cells += f'<div style="display:flex;justify-content:center"><div class="fin {cls}">{v if v else ""}</div></div>'
        finish_rows_html += f"""<div class="srow">
  <div style="display:flex;align-items:center;justify-content:center"><div class="dot" style="background:{m['color']}"></div></div>
  <div class="sname">{m['name']}{pill}</div>
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
        hl_row(swing_m["name"], ["Biggest position swing", f"{swing_val} places"])
    )

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Stats · Season overview &amp; highlights</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{total_race_scores}</div><div class="mc-lbl">Races scored so far</div></div>
  <div class="mc"><div class="mc-val">{chips_used_count}</div><div class="mc-lbl">Chips used league-wide</div></div>
  <div class="mc"><div class="mc-val">{cons_m['name']}</div><div class="mc-lbl">Most consistent ({cons_pos})</div></div>
  <div class="mc"><div class="mc-val">{swing_m['name']}</div><div class="mc-lbl">Biggest swing ({swing_val} places)</div></div>
</div>
<div class="section-label">Finish distribution</div>
<div class="card">
  <div style="display:grid;grid-template-columns:26px 1fr repeat(9,26px) 36px;gap:5px;padding:6px 0 2px">
    <div></div><div></div>{pos_headers}<div style="font-size:9px;color:#555;text-align:right">Pts</div>
  </div>
  {finish_rows_html}
</div>
<div class="section-label">Chip usage</div>
<div class="card">{chip_rows_html}</div>
<div class="section-label">Season highlights</div>
<div class="hl-grid">{highlights_html}</div>"""


# ── 07 Position Changes ───────────────────────────────────────────────────────
def panel_positions(data):
    M   = data["managers_sorted"]
    RD  = data["races_done"]
    ND  = data["n_done"]

    if ND == 0:
        return "<h1>🏎 The Undercut Collective</h1><div class='subtitle'>Position Changes · No races complete yet</div>"

    # Most gained / lost
    gains = [(m["name"], m["positions"][0] - m["positions"][-1])
             for m in M if m["positions"] and m["positions"][-1]]
    most_gained  = max(gains, key=lambda x:  x[1]) if gains else ("—", 0)
    most_lost    = min(gains, key=lambda x:  x[1]) if gains else ("—", 0)
    unchanged    = sum(1 for m in M if m["positions"] and len(set(m["positions"])) == 1)

    # Build standings table sorted by current position
    sorted_cur = sorted(M, key=lambda m: m["positions"][-1] if m["positions"] else 99)
    rows_html = ""
    for m in sorted_cur:
        if not m["positions"]:
            continue
        pos_r1  = m["positions"][0]
        pos_cur = m["positions"][-1]
        net     = pos_r1 - pos_cur   # positive = moved up
        if net > 0:
            net_html = (f'<span style="display:inline-block;width:0;height:0;border-left:4px solid transparent;'
                        f'border-right:4px solid transparent;border-bottom:6px solid #1D9E75;margin-right:3px;vertical-align:middle"></span>'
                        f'<span style="color:#1D9E75">+{net}</span>')
        elif net < 0:
            net_html = (f'<span style="display:inline-block;width:0;height:0;border-left:4px solid transparent;'
                        f'border-right:4px solid transparent;border-top:6px solid #E24B4A;margin-right:3px;vertical-align:middle"></span>'
                        f'<span style="color:#E24B4A">{net}</span>')
        else:
            net_html = '<span style="display:inline-block;width:8px;height:2px;background:#555;margin-right:3px;vertical-align:middle;border-radius:1px"></span><span style="color:#888">—</span>'

        pos_cols = "".join(
            f'<div style="display:flex;justify-content:center">'
            f'<div style="font-size:13px;font-weight:500;color:{m["color"]};text-align:center;width:40px">P{p}</div>'
            f'</div>'
            for p in m["positions"] if p is not None)

        rows_html += f"""<div class="change-row">
  <div style="display:flex;align-items:center;justify-content:center"><div class="dot" style="background:{m['color']}"></div></div>
  <div class="mname">{m['name']}</div>
  {pos_cols}
  <div class="net-badge">{net_html}</div>
</div>"""

    # Position timeline chart
    race_labels = [r["name"] for r in RD]
    datasets = []
    for i, m in enumerate(M):
        if not m["positions"]:
            continue
        dash = DASH_PATTERNS[i % len(DASH_PATTERNS)]
        datasets.append({
            "label": m["name"], "data": m["positions"],
            "borderColor": m["color"], "borderDash": dash,
            "borderWidth": 2.5, "pointBackgroundColor": m["color"],
            "pointRadius": 6, "pointHoverRadius": 8,
            "fill": False, "tension": 0
        })

    legend_html = "".join(
        f'<span><span style="width:10px;height:10px;border-radius:2px;background:{m["color"]};display:inline-block;flex-shrink:0"></span>{m["name"]}</span>'
        for m in M)

    # Dynamic column headers
    pos_col_headers = "".join(
        f'<div style="font-size:10px;color:#555;text-align:center">R{r["round"]}</div>'
        for r in RD)

    col_template = "26px 1fr " + " ".join(["40px"] * ND) + " 64px"

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Position Changes · How the standings have shifted</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{most_gained[1]}</div><div class="mc-lbl">Positions gained (most — {most_gained[0]})</div></div>
  <div class="mc"><div class="mc-val">{abs(most_lost[1])}</div><div class="mc-lbl">Positions lost (most — {most_lost[0]})</div></div>
  <div class="mc"><div class="mc-val">{unchanged}</div><div class="mc-lbl">Managers unchanged</div></div>
  <div class="mc"><div class="mc-val">R{RD[-1]['round'] if RD else '—'}</div><div class="mc-lbl">Most recent race</div></div>
</div>
<div class="section-label">Standings movement</div>
<div class="card">
  <div style="display:grid;grid-template-columns:{col_template};gap:10px;padding:6px 0 2px">
    <div></div><div></div>{pos_col_headers}<div style="font-size:10px;color:#555;text-align:right">Net</div>
  </div>
  {rows_html}
</div>
<div class="section-label">Position timeline</div>
<div style="position:relative;height:360px"><canvas id="posChart"></canvas></div>
<div class="legend" id="legend-positions"></div>
<script>
(function(){{
new Chart(document.getElementById('posChart'),{{
  type:'line',
  data:{{labels:{js(race_labels)},datasets:{js(datasets)}}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: P${{ctx.parsed.y}}`}}}}}},
    scales:{{
      x:{{ticks:{{color:'#888',font:{{size:13}}}},grid:{{color:'rgba(255,255,255,0.06)'}}}},
      y:{{reverse:true,min:1,max:{len(M)},ticks:{{color:'#888',stepSize:1,callback:v=>'P'+v}},grid:{{color:'rgba(255,255,255,0.06)'}}}}
    }}}}
}});
document.getElementById('legend-positions').innerHTML={js(legend_html)};
}})();
</script>"""


# ── 08 Team Picks ─────────────────────────────────────────────────────────────
def panel_picks(data):
    M  = data["managers_sorted"]
    RD = data["races_done"]

    # Build available races from lineups
    all_lineup_races = sorted(set(
        rname for m in M for rname in m["lineups"].keys()
    ), key=lambda r: next((rd["round"] for rd in data["races"] if rd["name"] == r), 99))

    if not all_lineup_races:
        return """<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Team Picks · No lineup data available yet</div>"""

    # Driver + constructor popularity across all races available
    driver_counts = {}
    con_counts    = {}
    drs_counts    = {}

    for m in M:
        for rname, picks in m["lineups"].items():
            for pick in picks:
                name = pick["name"]
                # Crude heuristic: constructors are title-cased single words or known names
                # Use the fact that drivers have "." in their name (e.g. "G. Russell")
                is_constructor = "." not in name and len(name.split()) <= 2 and name[0].isupper()
                # Better: if it's in a constructor list — use the fact drivers have initials
                is_driver = "." in name
                if is_driver:
                    driver_counts[name] = driver_counts.get(name, 0) + 1
                    if pick["drs"]:
                        drs_counts[name] = drs_counts.get(name, 0) + 1
                else:
                    con_counts[name] = con_counts.get(name, 0) + 1

    top_driver     = max(driver_counts, key=driver_counts.get) if driver_counts else "—"
    top_con        = max(con_counts,    key=con_counts.get)    if con_counts    else "—"
    top_drs        = max(drs_counts,    key=drs_counts.get)    if drs_counts    else "—"
    top_driver_cnt = driver_counts.get(top_driver, 0)
    top_con_cnt    = con_counts.get(top_con, 0)
    top_drs_cnt    = drs_counts.get(top_drs, 0)

    # Build JS teams array per race
    teams_by_race = {}
    for rname in all_lineup_races:
        teams_by_race[rname] = []
        for m in M:
            picks = m["lineups"].get(rname, [])
            if picks:
                teams_by_race[rname].append({
                    "name":      m["name"],
                    "teamName":  m["team"],
                    "color":     m["color"],
                    "chip":      {"label": m["chip_label"], "bg": m["chip_bg"], "tc": m["chip_tc"]} if m["chip_label"] else None,
                    "picks":     picks,
                })

    # Build popularity bars HTML (from first race for simplicity, TODO: multi-race)
    first_race = all_lineup_races[0]
    d_sorted = sorted(driver_counts.items(), key=lambda x: -x[1])[:8]
    c_sorted = sorted(con_counts.items(),    key=lambda x: -x[1])
    max_d    = d_sorted[0][1] if d_sorted else 1
    max_c    = c_sorted[0][1] if c_sorted else 1

    driver_pop_html = "".join(
        f'<div class="pop-row"><div><div class="pop-name">{n}</div>'
        f'<div class="pop-bar-bg"><div class="pop-bar-fill" style="width:{round(c/max_d*100)}%;background:#378ADD"></div></div></div>'
        f'<div class="pop-count">{c}/{len(M)} teams</div></div>'
        for n, c in d_sorted)

    con_pop_html = "".join(
        f'<div class="pop-row"><div><div class="pop-name">{n}</div>'
        f'<div class="pop-bar-bg"><div class="pop-bar-fill" style="width:{round(c/max_c*100)}%;background:#D85A30"></div></div></div>'
        f'<div class="pop-count">{c}/{len(M)} teams</div></div>'
        for n, c in c_sorted)

    race_options = "".join(
        f'<option value="{rn}">{rn}</option>' for rn in all_lineup_races)

    return f"""<h1>🏎 The Undercut Collective</h1>
<div class="subtitle">Team Picks · Starting lineups &amp; driver popularity</div>
<div class="metric-grid">
  <div class="mc"><div class="mc-val">{len(M)}</div><div class="mc-lbl">Teams</div></div>
  <div class="mc"><div class="mc-val">{top_driver.split('. ')[-1] if '. ' in top_driver else top_driver}</div><div class="mc-lbl">Most picked driver ({top_driver_cnt}×)</div></div>
  <div class="mc"><div class="mc-val">{top_con}</div><div class="mc-lbl">Most picked constructor ({top_con_cnt}×)</div></div>
  <div class="mc"><div class="mc-val">{top_drs.split('. ')[-1] if '. ' in top_drs else top_drs}</div><div class="mc-lbl">Most picked DRS ({top_drs_cnt}×)</div></div>
</div>
<div style="display:flex;gap:12px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
  <label>Race</label>
  <select id="raceSel" onchange="showRace(this.value)">{race_options}</select>
  <label style="margin-left:12px">Manager</label>
  <select id="teamSel" onchange="showTeam(raceSel.value, this.value)"></select>
</div>
<div id="team-display"></div>
<div class="pop-grid">
  <div><div class="section-label" style="margin-top:0">Most picked drivers</div><div class="card"><div id="driver-pop">{driver_pop_html}</div></div></div>
  <div><div class="section-label" style="margin-top:0">Most picked constructors</div><div class="card"><div id="con-pop">{con_pop_html}</div></div></div>
</div>
<script>
(function(){{
const teamsByRace={js(teams_by_race)};
const raceSel=document.getElementById('raceSel');
const teamSel=document.getElementById('teamSel');
function showRace(rname){{
  const teams=teamsByRace[rname]||[];
  const prev=teamSel.value;
  teamSel.innerHTML='';
  teams.forEach(t=>{{const o=document.createElement('option');o.value=t.name;o.textContent=t.name+' \u2014 '+t.teamName;if(t.name===prev)o.selected=true;teamSel.appendChild(o);}});
  if(teams.length>0) showTeam(rname, teamSel.value);
  else document.getElementById('team-display').innerHTML='<div style="padding:1rem;color:#555;font-size:13px">No lineup data for this race yet.</div>';
}}
function showTeam(rname, name){{
  const teams=teamsByRace[rname]||[];
  const t=teams.find(x=>x.name===name);
  if(!t){{document.getElementById('team-display').innerHTML='';return;}}
  const picks=t.picks.map(d=>`<div class="pick" style="${{d.drs?`border-color:${{t.color}};background:${{t.color}}11`:''}}">
    <div class="pick-label">${{d.drs?'DRS boost':'Driver/Constructor'}}</div>
    <div class="pick-name">${{d.name}}${{d.drs?'<span class="drs-badge">DRS</span>':''}}</div>
  </div>`).join('');
  const chipHtml=t.chip?`<span class="chip-pill" style="background:${{t.chip.bg}};color:${{t.chip.tc}};margin-left:auto">${{t.chip.label}}</span>`:'';
  document.getElementById('team-display').innerHTML=`<div class="team-card">
    <div class="team-header">
      <div class="team-dot" style="background:${{t.color}}"></div>
      <div><div class="team-name">${{t.name}}</div><div class="team-sub">${{t.teamName}}</div></div>
      ${{chipHtml}}
    </div>
    <div class="picks-grid">${{picks}}</div>
  </div>`;
}}
showRace(raceSel.value);
}})();
</script>"""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — ASSEMBLE COMBINED HTML
# ─────────────────────────────────────────────────────────────────────────────

SHARED_CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e8e8e6; min-height: 100vh; }
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
  /* leaderboard */
  .podium-row { display: flex; gap: 6px; margin-bottom: 1.5rem; }
  .podium-card { flex: 1; background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 8px; padding: 10px 12px; }
  .podium-pos { font-size: 11px; color: #888; margin-bottom: 2px; }
  .podium-name { font-size: 13px; font-weight: 500; }
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
  .slot { border-radius: 8px; padding: 8px 10px; display: flex; align-items: center; gap: 8px; }
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
  /* podiums */
  .race-podium { background: #1a1a1a; border: 0.5px solid #2a2a2a; border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 10px; }
  .podium-slots { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; }
  .tbd { font-size: 12px; color: #555; font-style: italic; }
  /* stats */
  .srow { display: grid; grid-template-columns: 26px 1fr repeat(9,26px) 36px; align-items: center; gap: 5px; padding: 7px 0; border-bottom: 0.5px solid #2a2a2a; }
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
  .picks-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(130px,1fr)); gap: 6px; }
  .pick { border-radius: 8px; padding: 7px 10px; background: #111; border: 0.5px solid #2a2a2a; }
  .pick-label { font-size: 9px; color: #555; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 2px; }
  .pick-name { font-size: 12px; font-weight: 500; }
  .drs-badge { display: inline-block; font-size: 9px; padding: 1px 5px; border-radius: 10px; background: #FFD700; color: #7a5800; font-weight: 500; margin-left: 4px; }
  .pop-row { display: grid; grid-template-columns: 1fr 80px; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 0.5px solid #2a2a2a; }
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
      <span class="header-badge">{SEASON} Season · {nd}/{nt} races · Last: {last}</span>
    </div>
    <nav class="tab-nav">
{tab_buttons}    </nav>
  </div>
</div>
<div id="tab-content">
{panel_divs}</div>
<script>
function showTab(slug) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + slug).classList.add('active');
  document.getElementById('tab-' + slug).classList.add('active');
  document.getElementById('tab-' + slug).scrollIntoView({{behavior:'smooth',block:'nearest',inline:'center'}});
  try {{ sessionStorage.setItem('uc-tab', slug); }} catch(e) {{}}
}}
try {{
  const t = sessionStorage.getItem('uc-tab');
  if (t && document.getElementById('panel-' + t)) showTab(t);
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

    raw  = read_workbook(WORKBOOK_PATH)
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
    input("Press Enter to close...")


if __name__ == "__main__":
    main()
