# Sleeper Draft Assistant

A local draft-day assistant for **Fantasy NFL 2026** (Sleeper, 12 teams, snake,
15 rounds, full PPR, one QB). It reads your live draft from Sleeper, works out
who to take, and says why in plain English.

No paid APIs, no keys, no accounts, nothing deployed. Python standard library
only — **there is nothing to `pip install`.**

---

## Do this tonight (10 minutes)

```bash
cd Nfl
python3 build_data.py      # fetch and cache players, league, projections
python3 app.py             # opens http://localhost:8000
```

`build_data.py` prints a report. Read it. It tells you exactly what it found
and what is missing — in particular whether it got real projections.

Then **run a Sleeper mock draft** with the app open beside it. This is the
non-negotiable step. You want to have seen the screen light up before it
matters.

## On draft day

```bash
python3 app.py
```

Leave it running. It polls Sleeper every 3 seconds. When the draft starts,
Sleeper randomises the order; the app detects your slot within seconds and
shows it in large type along with every pick number you own for all 15 rounds.

---

## The screen

- **YOUR PICK IN: N** — the big number top-left. Green when you have room,
  amber at 2 away, red and flashing when you are on the clock.
- **Take this player** — one name, with a plain-English reason you can act on
  in ten seconds, plus four ranked alternatives and what each one costs you in
  projected points.
- **Draft board** — all 12 teams, every pick, and what each manager still
  needs. If the three managers ahead of you all need a running back, the
  receiver you want is likelier to survive, and the app factors that in.
- **My roster** — filled and empty starting slots, what you still need, and
  loud warnings if the remaining rounds cannot fill a lineup.
- **Value board** — players whose projected scoring outruns where the market is
  drafting them.
- **Queue export** — a ranked list to paste into Sleeper's own draft queue.

### Colour and jargon

`VOR` is "value over replacement" — how many more points a player scores than
the freely available player at his position. It is the only fair way to compare
a quarterback to a receiver. Bigger is better. Everything else on screen is
written in ordinary English on purpose.

---

## Data: what to paste and where

Open the **Data & manual controls** panel, bottom right.

### Projections

The app never uses a data source's own "fantasy points" column. It computes
points from raw projected stats using your league's exact scoring, because
sources disagree about passing touchdowns (yours are worth **4**, not 6) and
fumbles (**-2**), and those disagreements move quarterbacks and running backs
several draft slots.

Paste any projections table from FantasyPros, ESPN or Yahoo. Column order does
not matter, and it handles CSV, tab-separated, and text copied straight out of
a browser table. Headers it understands include:

```
Player, Team, POS, PASS YDS, PASS TD, INT, RUSH YDS, RUSH TD,
REC, REC YDS, REC TD, FUMBLES LOST, BYE
```

If the table only has a total-points column (`FPTS`), it is used as-is and the
player is flagged, because those points were computed under someone else's
scoring rules.

### ADP — use Sleeper's, not ESPN's

ADP drives the survival maths: the odds a player lasts until your next pick.
Sleeper drafters draft off Sleeper's rankings, so **Sleeper ADP is the only one
that gives correct odds.** FantasyPros publishes it free.

Formats accepted:

```
1. Ja'Marr Chase CIN WR 1.2
2. Bijan Robinson ATL RB 2.4
```

or

```
Rank,Player,Team,POS,ADP,Std Dev
1,Ja'Marr Chase,CIN,WR,1.4,0.8
```

If you paste no ADP at all, the app falls back to Sleeper's own internal player
ranking as an estimate and labels it `ADP ESTIMATED`. That works, but real ADP
is sharper — paste it if you can.

You can also load these from files before starting:

```bash
python3 build_data.py --projections-file proj.csv --adp-file adp.csv
```

---

## How it decides

**1. Points** from raw stats using your exact scoring table (`config.py`).

**2. VOR** against 12-team baselines. Your two flex slots are split by
full-PPR tendency (40% RB, 50% WR, 10% TE), which puts replacement level at
QB12, RB34, WR36, TE14, K12, DEF12.

**3. Survival across your next two picks.** Snake drafting is not "take the
best guy", it is "take the guy who won't be there later and wait on the guy who
will". For every candidate:

```
take now = his value + expected best available at your next pick
wait     = best alternative now
           + P(he survives) x his value
           + P(he is gone)  x expected next-best at his position
```

The recommendation is whichever maximises the total across the pair, and the
difference is shown in points.

**4. Bench discount.** VOR measures what a player is worth *in your starting
lineup*. Once a position is full — its own slots plus whatever the flex can
absorb — the next player there only helps if someone gets hurt, so his score is
discounted. This is what stops the app taking a sixth running back ahead of
your first receiver. It is a correction, not an override: a genuinely elite
player still wins.

**5. League-specific rules**, enforced as hard blocks:

| Rule | Why |
|---|---|
| No QB before round 8 | One QB starts, 12 teams need 12 of 32. Passing TDs are only 4 points. Taking a QB early is the single most common beginner mistake. |
| No K or DEF before round 14 | The gap between the best and worst kicker is small and unpredictable. |
| No backups before round 12 | **No IR slots and only 5 bench spots.** A handcuff occupies a roster spot all season for nothing. |
| Injury risk penalised heavily | Same reason. Most of your league will ignore this. |
| Last picks must fill empty starting slots | You cannot win with an incomplete lineup. |

**6. Tiers.** A tier breaks where the gap between consecutive players at a
position exceeds 1.5x the running average gap. When the recommended player is
last in his tier you get a **TIER BREAK** warning — that is the moment waiting
gets expensive. Tight end especially falls off a cliff.

Everything above is tunable at the top of `config.py`.

---

## TROUBLESHOOTING — DRAFT DAY

Work down the list. Nothing here requires restarting the draft.

### The live pick feed stalls or the board stops updating

The header shows `feed failing` and a red banner appears after three
consecutive failures. **The app keeps every pick it already had** — it does not
reset.

1. Wait 15 seconds. Most stalls clear themselves.
2. Meanwhile, use **Manual override**: type a name, click *Mark taken*
   (or *Taken by me* for your own picks). *Undo* reverses the last one.
   You can also click the **taken** button on any row of the player pool.
3. You can drive the entire draft this way if Sleeper's API goes down
   completely. The recommendations work identically.

### Your slot never appears (shows `?`)

Normal before the draft starts — Sleeper randomises the order at kickoff.

If the draft has started and it still shows `?`, look at the Sleeper app for
your position in the order, then use **Force draft slot** in the Data panel.
Slot 1 is the first pick of round 1. Everything recomputes instantly.

Or start the app with it:

```bash
python3 app.py --slot 7
```

### No projections / every player shows 0 points

The recommendation card will say so. Paste a projections table into the Data
panel — that path does not depend on any API and is the reason it exists.

Fastest source under time pressure: search "FantasyPros 2026 projections",
select the table, copy, paste, click **Load projections**. It reports how many
players matched. Anything under a few hundred means you grabbed only part of
the table.

### The league won't resolve

`build_data.py` lists every league on your account. If the name does not match,
edit `LEAGUE_NAME` in `config.py` to one of the names it printed and rerun.

If Sleeper is unreachable entirely, run offline off the cache:

```bash
python3 app.py --offline --slot 7
```

You lose the live feed and mark picks by hand, but the valuation engine, the
recommendations and the queue all work.

### The app says the scoring does not match

Believe it. It compares your live league settings against `config.py` and warns
loudly on any difference. Live settings win — update `config.SCORING` to match
and restart. If it warns about **superflex or two quarterbacks**, stop and fix
it: the QB strategy assumes one QB and would be badly wrong.

### The page is blank or shows "server down"

The server stopped. Restart it — no state is lost that matters, since picks are
re-read from Sleeper on the first poll. Manual overrides are the exception and
would need re-entering.

```bash
python3 app.py
```

### Port 8000 is already in use

```bash
python3 app.py --port 8080
```

### Everything is broken and you are on the clock

Take the top name in the **Queue export** box. It is regenerated every three
seconds, it already respects every roster rule, and it is never a bad pick.

---

## Files

```
app.py              local server, Sleeper proxy, polling loop
build_data.py       one-time fetch and cache, with a verification report
config.py           league settings, exact scoring table, all tunable knobs
scoring.py          fantasy points from raw stats
sleeper.py          API client, disk cache, endpoint probing
projections.py      paste parsing and player-name matching
valuation.py        VOR, survival, tiers, risk, recommendations
draftstate.py       snake pick numbering, rosters, runs, needs
templates/index.html   the interface
cache/              players.json, projections.json, adp.json, league.json
tests/              unit tests and a full simulated mock draft
```

Run the tests any time:

```bash
python3 -m unittest discover -s tests -v
```

The suite includes a complete simulated 12-team, 15-round snake draft driven
through the real application code, which checks that the slot is detected, no
quarterback goes before round 8, no kicker before round 14, and the final
roster can field a legal lineup.

---

## Known limits — read before you rely on it

- **The Sleeper projections endpoint is probed, not assumed.** It was not
  reachable from the machine this was built on, so `build_data.py` tries five
  candidate endpoint shapes on *your* machine and reports which one answered.
  If none do, it falls back to an estimate from prior-season stats (labelled
  `ESTIMATED` everywhere) and then to the paste box. **The paste box is the
  path that is guaranteed to work — use it.**
- **Bye weeks come only from pasted data.** Nothing invents them. Paste a table
  with a `BYE` column to switch on bye-conflict warnings.
- **Some risk factors from the spec are not implemented**, because Sleeper's
  free data does not carry them: target share, snap-share trend, red-zone
  usage, vacated targets, coaching and offensive-line changes. What *is*
  implemented from real data: injury status, age curves, depth-chart position,
  and the value gap between production and ADP. Nothing is fabricated to fill
  the gap.
- **Not built:** the 500-run mock-draft simulation and the playoff-schedule
  tiebreaker (Phase 3 in the spec).
- Projected points are projections. The app is a decision aid under a
  two-minute clock, not an oracle.
