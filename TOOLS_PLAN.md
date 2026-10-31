# Tools tab: plan

A new **Tools** tab for The Undercut Collective that helps the league pick better
teams, working the way f1fantasytools.com (F1FT) does. This covers how it works and
where the data comes from, not the look and feel. Written 2026-10-31.

**Status and decisions (Tim, 2026-10-31):**
- The tab is for the whole league ("let's even the playing field"; Tim uses F1FT himself).
- **Built:** Hindsight and the Price predictor (`tools.py`), plus the data behind them
  (`player_gameday_stats`, `player_event_points`, `players.tla/active`). Hindsight matches
  F1FT's Austin result exactly (321 pts, same 7 picks, $131.8M). The predictor's odds come
  from recent form for now and land close to F1FT's (VER +0.24 vs +0.21, MCL +0.27 vs +0.28).
- **Next:** the simulation, built on the 2026 data rather than left for 2027, then the Team
  Calculator on top of it. Tim uses every part of F1FT, so Live Scoring, Statistics and
  the analyzers follow after that.
- **Elite Data is feasible:** `feeds/leaderboard/public/global/list_1_0_1.json` is public
  and lists the global top 500 with each player's current team (pick % in one call).
  DRS % and chips need the opponent team call per player, ~500 calls once per round
  after lock, and the same call can backfill past rounds.

## 1. How F1FT works (researched 2026-10-31)

Everything on F1FT comes back to one table: an **expected points (xPts) and expected
price change (xΔ$) for each of the 22 drivers and 11 constructors**, for the coming
weekend. The tools are views over that table.

| F1FT tool | What it does | Needs |
|---|---|---|
| **Team Calculator** | Ranks every legal team (5 drivers + 2 constructors, under budget) by expected points, best DRS pick included. Starts from your current team, with transfer penalties. Chip toggles (X3 Extra DRS, LL Limitless, WC Wildcard, NN No Negative, AP Auto Pilot), include/exclude filters, max budget, and an option to count expected price changes as points. | xPts, xΔ$, prices |
| **Budget Builder** | For each asset: points last 2 races, xPts this race, the odds of each price change band (e.g. -0.3 / -0.1 / +0.1 / +0.3 for $18.5M+), and xΔ$. | the simulation's points distribution + the price rule |
| **Hindsight** | Team Calculator run on *actual* points of a past round: "the best team you could have had". | actual points (we already store them) |
| **Elite Data** | Pick %, DRS % and chip use of the global top 500, per round. | the top 500's teams |
| Statistics, Live Scoring, Season/Team/League Analyzer | Stats and live views. Mostly things our dashboard already does for the league. | — |

**Where the xPts come from (F1FT's "rhter" simulation):** each driver gets a
qualifying pace (Qpace: weighted past qualifying results plus FP1/FP2/FP3 times), a
race pace (Rpace: Qpace plus how hard it is to overtake at that track, made worse by
grid penalties), a DNF chance and a fastest-lap chance, plus judgement calls (team
orders, known penalties, wet weather). It samples a weekend ~10,000 times (each
driver's pace from a normal distribution, DNFs at random), **scores every run with the
F1 Fantasy rules**, and averages the results. It's re-run after each practice session
("Mexico. Post-FP2."). The odds in Budget Builder are just the share of runs in which
an asset scores enough for each price band.

## 2. Data we can get (all checked from Sentinel, 2026-10-31)

| Source | What | Status |
|---|---|---|
| F1 Fantasy `feeds/drivers/{round}_en.json` (already fetched) | Price, previous price, points per session, pick % and DRS %, season totals per scoring type (`AdditionalStats`), `NoNegativePoints` | in use |
| F1 Fantasy `feeds/popup/playerstats_{id}.json` (**new**) | **Every past round's points broken down by scoring item**, for drivers (race and qualifying position, positions gained, overtakes, fastest lap, Driver of the Day, not classified...) and constructors (both drivers Q2/Q3, fastest pit stop, ...), plus the price before and after each round. No login needed. | works, the main training data |
| F1 Fantasy `feeds/schedule/raceday_en.json` | Session times, deadlines, lock state, budget cap, transfers allowed | works |
| F1 Fantasy `feeds/limits/constraints.json` | Current gameday, deadline, max team value | works |
| Our DB | Every league manager's team, budget, free transfers and chips left, per round | in use |
| **OpenF1** (`api.openf1.org`, free, no key) | **Every practice/qualifying/race lap**, sector times, speed traps, tyre stints and compounds, pit stops, weather, race control (penalties). Mexico FP2 was there within hours. | works, ~1 s per call |
| **Jolpica** (Ergast replacement, already used for the calendar) | Results, qualifying, grids, DNFs, pit stops for every season back to 1950: track history and overtaking difficulty | works |
| Global top 500 teams | F1 Fantasy's public leaderboard + the opponent team endpoint we already use | not checked; maybe ~500 calls a round |

Things the data **doesn't** give us: weather forecasts (Open-Meteo would, free),
grid penalties announced before practice (race control in OpenF1 only once a session runs),
and betting odds (skip).

**F1's price rule, worked out from our 2026 history:** a price changes after each race
based on the asset's **average points per $M over the last 3 races it took part in**
(this one included), against its current price: under 0.6 / 0.6–0.9 / 0.9–1.2 / 1.2+
gives -0.3 / -0.1 / +0.1 / +0.3 for $18.5M and up, and -0.6 / -0.2 / +0.2 / +0.6
under. Prices stay between $3.0M and $34.0M, and an asset that didn't race doesn't
move. That matches 605 of 608 changes so far (`tools.price_change`).

## 3. What we'd build

Same split as F1FT: **a model that makes the xPts/xΔ$ table on Sentinel, and tools
in the page that use it.** The site stays static on GitHub Pages: the table is baked
into the page as JSON, and all the calculating happens in the browser (enumerating
every team is ~1.45 million combinations, well under a second in JS).

### Tools, in build order

1. **Hindsight** (first, because it needs no predictions): best possible team for
   any past round from real points, and "your team vs the best team", for each league
   manager. It checks the optimiser (team rules, DRS, chips, budget, price changes)
   against real scores before any prediction rides on it.
2. **Price predictor** (our Budget Builder): price-change bands and odds per asset,
   from the price rule plus the model's points distribution. "What do I need to score
   to rise", and what each manager's budget will be after the race.
3. **Team Calculator**: best teams by expected points, starting from a chosen
   manager's real current team (we know everyone's team, bank and free transfers,
   which F1FT can't do without a login), transfer penalty counted, chip toggles,
   include/exclude, max budget, and xΔ$ as points if wanted.
4. **League extras** (ours, not on F1FT): who in the league owns what, differentials
   ("an asset nobody else here has"), chips each manager has left, and a running
   score of how the model's predictions did against what actually happened.
5. Later, maybe: **Elite Data** from the global top 500, if fetching their teams turns
   out to be cheap and allowed.

### The prediction model, in steps

- **Step A, baseline (weeks before the sim is ready):** xPts from recent form,
  weighted toward the last few rounds, split by scoring item using the playerstats
  breakdown, plus each driver's history at that track from Jolpica. Simple, but it
  lets the tools ship and gives the sim a benchmark to beat.
- **Step B, simulation (like rhter):** Qpace from past qualifying + this weekend's
  practice (OpenF1: best laps on the softs, long-run averages from stints); Rpace from
  Qpace plus a per-track overtaking factor (from Jolpica: positions gained in past races
  there); DNF rate per driver/team; fastest lap from race pace. Run ~10,000 weekends,
  score each with the full F1 Fantasy rules (positions, positions gained/lost,
  overtakes, fastest lap, DOTD, DNF, constructor qualifying bonuses, pit stops, sprint
  weekends), and keep the averages and the spread.
- **Refresh:** after FP1, FP2, FP3/sprint qualifying and qualifying, from the existing
  15-minute pipeline. The weekend window already starts 1 hour before FP1; a pre-weekend
  run (Thursday) needs a small change to the timer gate.
- **Check it before launch:** re-run the 2026 season round by round using only what
  was known at the time, and compare with real points. The goal is clearly better than
  "last 3 races' average". Keep scoring it every weekend after launch.

### Where it fits in the code

- `tools_data.py`: fetches playerstats, OpenF1 and Jolpica into new tables
  (`player_event_points`, `session_laps`, `track_history`), cached so each weekend
  only fetches what's new.
- `tools_model.py`: baseline, then the simulation; writes a `predictions` table
  (round, made after which session, asset, xPts, spread, xΔ$, band odds).
- `build_dashboard.py`: new Tools tab; reads `predictions` and the league tables,
  and writes the JS calculator.
- Sentinel has no numpy. Pure Python is fast enough for ~10,000 weekends × 22 drivers
  (seconds), or numpy in a venv under the project; no root changes either way.
- Covered by the off-site backup already (`~/f1-fantasy` is in it).

## 4. Rough size

| Piece | Effort |
|---|---|
| Data fetchers + tables (playerstats, OpenF1, Jolpica) | 1 session |
| Hindsight + optimiser in JS | 1 session |
| Price rule (finish tier B) + predictor | 1 session |
| Baseline model + Team Calculator | 1–2 sessions |
| Simulation model + backtest | 2–3 sessions, the bulk of the work |
| League extras | 1 session |

Steps 1–3 are useful on their own and could be live within a couple of race weekends.
There are 5 rounds left this season (Mexico, Brazil, Las Vegas, Qatar, Abu Dhabi), so the
simulation is realistically a 2027 thing, with 2026 as its training and test data.

## 5. Questions for Tim

1. The tab is visible to the whole league, so everyone gets the same edge. Fine, or
   should Tools be Tim-only (e.g. a separate page behind Cloudflare Access)?
2. Start with Hindsight + the price predictor this season, and the simulation for
   2027? Or go straight for the full model?
3. Is Elite Data (what the global top 500 pick) worth the extra fetching?
4. Anything F1FT does that you rely on that isn't covered here?
