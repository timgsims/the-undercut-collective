"""Provisional weekend points, worked out from driver scores.

F1 Fantasy posts every driver's and constructor's points for a session well
before it stores each team's total: for Austin 2026 qualifying the driver
points were up at 12:08 NZT and our league's team totals at 14:15. The
official app adds each team up itself in the meantime, so the dashboard does
the same. fetch_f1_data.py stores the result with race_results.is_provisional
= 1, replaces it with F1's own total as soon as F1 stores one, and the
dashboard marks it "provisional" until then.

Checked 2026-10-31 against every manager's official score for rounds 1-18
(144 scores): 143 exact. The one miss is a Final Fix round (Tim, Monaco, 6
points off), which isn't modelled: the API doesn't say which pick was swapped
in for which session. F1's own later scoring corrections can't be foreseen
either."""


def team_points(picks, pts, no_neg_pts, types, captain, mega, transfers,
                subs_allowed, sub_cost, chip=None, inactive_penalty=None):
    """picks: player ids. pts: {player_id: points so far this weekend}.
    no_neg_pts: {player_id: the feed's NoNegativePoints}, used only on a No
    Negative round. types: {player_id: "driver"|"constructor"}. chip: the
    chip played this round, if any. inactive_penalty: the API's (negative)
    inactive_driver_penality_points. Returns the points, or None if any pick
    has no points yet (the drivers feed hasn't caught up)."""
    if not picks or any(pts.get(p) is None for p in picks):
        return None
    if chip == "No Negative":
        # NoNegativePoints, not max(0, points): the chip removes each
        # negative scoring item, so a -12 can become +8 (Lawson, Miami).
        score = {p: float(no_neg_pts[p]) if no_neg_pts.get(p) is not None
                 else max(0.0, float(pts[p])) for p in picks}
    else:
        score = {p: float(pts[p]) for p in picks}
    if chip == "Auto Pilot":
        # DRS goes to whichever driver scores most.
        drivers = [p for p in picks if types.get(p) != "constructor"]
        if drivers:
            captain = max(drivers, key=lambda p: score[p])
    total = sum(score.values())
    if mega and mega in score:
        total += 2 * score[mega]        # Extra DRS: 3x
    if captain and captain in score and captain != mega:
        total += score[captain]         # DRS: 2x
    if chip not in ("Wildcard", "Limitless"):
        total -= max(0, (transfers or 0) - (subs_allowed or 0)) * (sub_cost or 0)
    total += inactive_penalty or 0
    return total
