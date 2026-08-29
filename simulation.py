"""Forward mock-draft simulation (spec section 8, phase 3).

Plays the rest of the draft out hundreds of times and reports the distribution
of your projected STARTING LINEUP total - the only number that actually decides
your season. Bench players score nothing, so a roster's value is the lineup it
can field, not the sum of its parts.

Two design choices make the output trustworthy:

* **Common random numbers.** Run i uses the same seed for every candidate, so
  candidate A and candidate B face an identical draft: the same opponents reach
  for the same players in the same order. The comparison between them is then
  paired, which strips out nearly all the simulation noise and lets us say "A
  beat B in 68% of identical drafts" rather than comparing two noisy averages.

* **The simulated you drafts the way the app actually recommends** - same
  positional blocks, same bench discount - so the forecast reflects the tool
  you are really using, not an idealised drafter.

Everything is plain Python: no numpy, no dependencies.
"""

import random
import statistics

import config
import draftstate

# How deep to simulate. Only 180 players come off the board in a 12x15 draft,
# so a pool of the top few hundred by ADP is complete for our purposes.
POOL_SIZE = 240
POSITION_RESERVE = 16      # extra players kept per position so K/DEF never run dry
# The simulated you considers the best few available at EVERY position, then
# scores them with the bench discount applied. Scanning a single value-ordered
# list instead would hide whole positions behind a run of higher-VOR players at
# a position you have already filled - which is exactly the mistake the bench
# discount exists to prevent.
CANDIDATES_PER_POSITION = 4
OPPONENT_SCAN_WINDOW = 40  # how far an opponent will reach past his best option

# A typical Sleeper drafter's positional habits, used for the other 11 teams.
OPPONENT_CAPS = {"QB": 2, "RB": 7, "WR": 8, "TE": 3, "K": 1, "DEF": 1}
OPPONENT_QB_SECOND_ROUND = 10   # nobody takes a backup QB before this round
OPPONENT_KDEF_ROUND = 13        # nobody takes a kicker or defense before this


def optimal_lineup(players):
    """Best legal starting lineup from a set of drafted players.

    Fills each mandatory slot with the best players at that position, then the
    two flex slots with the best remaining RB/WR/TE. That ordering is optimal
    for this roster shape, because flex is a superset of the flex-eligible
    positions and the mandatory slots can only be filled by their own position.

    Returns (total_points, chosen_players, unfilled_slots).
    """
    by_position = {}
    for player in players:
        by_position.setdefault(player["position"], []).append(player)
    for group in by_position.values():
        group.sort(key=lambda p: p.get("points") or 0.0, reverse=True)

    chosen = []
    used = set()
    unfilled = 0
    total = 0.0

    for position, count in config.STARTERS.items():
        group = by_position.get(position, [])
        for slot in range(count):
            if slot < len(group):
                player = group[slot]
                used.add(id(player))
                chosen.append(player)
                total += player.get("points") or 0.0
            else:
                unfilled += 1

    flex_pool = []
    for position in config.FLEX_ELIGIBLE:
        for player in by_position.get(position, []):
            if id(player) not in used:
                flex_pool.append(player)
    flex_pool.sort(key=lambda p: p.get("points") or 0.0, reverse=True)

    for slot in range(config.FLEX_SLOTS):
        if slot < len(flex_pool):
            player = flex_pool[slot]
            chosen.append(player)
            total += player.get("points") or 0.0
        else:
            unfilled += 1

    return round(total, 2), chosen, unfilled


class Context:
    """Everything the simulation needs, flattened into parallel lists.

    Integer indices and plain lists rather than dicts: this loop runs a couple
    of million times per request and attribute lookups dominate the cost.
    """

    def __init__(self, board, state, shape, my_slot, opponent_rosters, my_players):
        self.teams = shape["teams"]
        self.rounds = shape["rounds"]
        self.draft_type = shape["type"]
        self.reversal = shape["reversal_round"]
        self.total_picks = self.teams * self.rounds
        self.current_pick = state["current_pick"]
        self.my_slot = my_slot
        self.taken = set(state["taken"])

        # Already on my roster: only position and points matter from here.
        self.my_players = [{"position": p["position"], "points": p.get("points") or 0.0,
                            "name": p.get("name")} for p in my_players]
        self.opponent_counts = {}
        for slot in range(1, self.teams + 1):
            counts = {}
            for position in (opponent_rosters.get(slot) or []):
                counts[position] = counts.get(position, 0) + 1
            self.opponent_counts[slot] = counts

        available = [p for p in board if p["player_id"] not in self.taken]
        by_adp = sorted(
            available,
            key=lambda p: p["adp"] if p.get("adp") is not None else 9999.0)

        keep = list(by_adp[:POOL_SIZE])
        seen = {p["player_id"] for p in keep}
        # Guarantee enough of every position to finish a draft, or the late
        # rounds would stall when every kicker in the pool is gone.
        for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
            extra = 0
            for player in by_adp:
                if extra >= POSITION_RESERVE:
                    break
                if player["position"] != position:
                    continue
                if player["player_id"] not in seen:
                    keep.append(player)
                    seen.add(player["player_id"])
                extra += 1

        self.players = keep
        self.size = len(keep)
        self.pid = [p["player_id"] for p in keep]
        self.name = [p["name"] for p in keep]
        self.position = [p["position"] for p in keep]
        self.points = [p.get("points") or 0.0 for p in keep]
        self.adj_vor = [p.get("adj_vor") or 0.0 for p in keep]
        self.depth = [p.get("depth_chart_order") or 1 for p in keep]
        self.adp = [(p["adp"] if p.get("adp") is not None else 300.0) for p in keep]
        self.sd = [max(2.0, p.get("adp_stdev") or 8.0) for p in keep]

        # Value order overall, and per position so no position can be hidden
        # behind a run of higher-VOR players somewhere else.
        self.value_order = sorted(range(self.size),
                                  key=lambda i: self.adj_vor[i], reverse=True)
        self.by_position = {}
        for index in self.value_order:
            self.by_position.setdefault(self.position[index], []).append(index)
        self.index_of = {pid: i for i, pid in enumerate(self.pid)}

        # Precompute which slot picks at each remaining pick number, and how
        # many picks I have left from there - both are read in the hot loop.
        self.slot_at = {}
        self.round_at = {}
        for pick_no in range(self.current_pick, self.total_picks + 1):
            rnd, slot = draftstate.slot_of_pick(
                pick_no, self.teams, self.draft_type, self.reversal)
            self.slot_at[pick_no] = slot
            self.round_at[pick_no] = rnd

        self.my_picks_left_at = {}
        remaining = 0
        for pick_no in range(self.total_picks, self.current_pick - 1, -1):
            if self.slot_at[pick_no] == self.my_slot:
                remaining += 1
            self.my_picks_left_at[pick_no] = remaining


def _my_allowed(ctx, index, round_no, counts, picks_left, needs_left):
    """The same hard rules the live recommendation engine enforces."""
    position = ctx.position[index]

    if position == "QB":
        if counts.get("QB", 0) >= 1:
            return False
        if round_no < config.QB_UNLOCK_ROUND:
            return False
    if position == "K":
        if round_no < config.K_UNLOCK_ROUND or counts.get("K", 0) >= 1:
            return False
    if position == "DEF":
        if round_no < config.DEF_UNLOCK_ROUND or counts.get("DEF", 0) >= 1:
            return False
    if (position in ("RB", "WR") and ctx.depth[index] >= 2
            and round_no < config.HANDCUFF_BLOCK_ROUND):
        return False

    # Every remaining pick must fill an empty starting slot once they run level.
    if picks_left <= needs_left:
        starters = config.STARTERS.get(position, 0)
        have = counts.get(position, 0)
        fills = have < starters
        if not fills and position in config.FLEX_ELIGIBLE:
            used_flex = sum(max(0, counts.get(pos, 0) - config.STARTERS.get(pos, 0))
                            for pos in config.FLEX_ELIGIBLE)
            fills = used_flex < config.FLEX_SLOTS
        if not fills:
            return False

    cap = {"QB": 2, "TE": 2, "K": 1, "DEF": 1, "RB": 6, "WR": 7}.get(position, 6)
    return counts.get(position, 0) < cap


def _lineup_multiplier(position, counts):
    """Bench discount, matching valuation.lineup_multiplier on position counts."""
    have = counts.get(position, 0)
    starters = config.STARTERS.get(position, 0)
    if have < starters:
        return 1.0
    if position in config.FLEX_ELIGIBLE:
        used_flex = sum(max(0, counts.get(pos, 0) - config.STARTERS.get(pos, 0))
                        for pos in config.FLEX_ELIGIBLE)
        if used_flex < config.FLEX_SLOTS:
            return 1.0
    return config.BENCH_VALUE_MULTIPLIER


def _needs_left(counts):
    """Empty mandatory starting slots given a roster's position counts."""
    total = 0
    for position, required in config.STARTERS.items():
        total += max(0, required - counts.get(position, 0))
    used_flex = sum(max(0, counts.get(pos, 0) - config.STARTERS.get(pos, 0))
                    for pos in config.FLEX_ELIGIBLE)
    total += max(0, config.FLEX_SLOTS - used_flex)
    return total


def _opponent_allowed(ctx, index, round_no, counts):
    """A plausible rival: fills a lineup, waits on kickers, takes one QB."""
    position = ctx.position[index]
    if counts.get(position, 0) >= OPPONENT_CAPS.get(position, 6):
        return False
    if position in ("K", "DEF") and round_no < OPPONENT_KDEF_ROUND:
        return False
    if position == "QB" and counts.get("QB", 0) >= 1 and round_no < OPPONENT_QB_SECOND_ROUND:
        return False
    return True


def _run_once(ctx, forced_index, seed):
    """Play the rest of the draft out once. Returns the lineup total."""
    rng = random.Random(seed)
    taken = set()
    for pid in ctx.taken:
        index = ctx.index_of.get(pid)
        if index is not None:
            taken.add(index)

    my_counts = {}
    my_roster = list(ctx.my_players)
    for player in my_roster:
        my_counts[player["position"]] = my_counts.get(player["position"], 0) + 1
    opponent_counts = {slot: dict(counts)
                       for slot, counts in ctx.opponent_counts.items()}

    # One noisy ADP ordering per run: this IS the randomness of the draft, and
    # it is identical across candidates because the seed is.
    adp, sd = ctx.adp, ctx.sd
    order = sorted(range(ctx.size), key=lambda i: adp[i] + rng.gauss(0.0, sd[i]))
    cursor = 0

    forced = forced_index
    for pick_no in range(ctx.current_pick, ctx.total_picks + 1):
        slot = ctx.slot_at[pick_no]
        round_no = ctx.round_at[pick_no]
        choice = None

        if slot == ctx.my_slot:
            if forced is not None and forced not in taken:
                choice = forced
                forced = None
            else:
                picks_left = ctx.my_picks_left_at[pick_no]
                needs = _needs_left(my_counts)
                best_score = None
                for position, ordered in ctx.by_position.items():
                    considered = 0
                    for index in ordered:
                        if index in taken:
                            continue
                        considered += 1
                        if considered > CANDIDATES_PER_POSITION:
                            break
                        if not _my_allowed(ctx, index, round_no, my_counts,
                                           picks_left, needs):
                            continue
                        score = ctx.adj_vor[index] * _lineup_multiplier(
                            position, my_counts)
                        if best_score is None or score > best_score:
                            best_score, choice = score, index
                if choice is None:
                    # Everything is blocked: never forfeit a pick, take the best
                    # player left rather than ending the draft a man short.
                    for index in ctx.value_order:
                        if index not in taken:
                            choice = index
                            break
            if choice is not None:
                position = ctx.position[choice]
                my_counts[position] = my_counts.get(position, 0) + 1
                my_roster.append({"position": position,
                                  "points": ctx.points[choice],
                                  "name": ctx.name[choice]})
        else:
            counts = opponent_counts[slot]
            while cursor < ctx.size and order[cursor] in taken:
                cursor += 1
            probe = cursor
            scanned = 0
            while probe < ctx.size and scanned < OPPONENT_SCAN_WINDOW:
                index = order[probe]
                if index not in taken and _opponent_allowed(ctx, index, round_no, counts):
                    choice = index
                    break
                probe += 1
                scanned += 1
            if choice is None:
                # Nothing sensible left: take the best available outright.
                probe = cursor
                while probe < ctx.size:
                    if order[probe] not in taken:
                        choice = order[probe]
                        break
                    probe += 1
            if choice is not None:
                position = ctx.position[choice]
                counts[position] = counts.get(position, 0) + 1

        if choice is not None:
            taken.add(choice)

    total, _, unfilled = optimal_lineup(my_roster)
    return total, unfilled


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 1)


def _histogram(values, bins=12):
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [{"low": round(low, 1), "high": round(high, 1), "count": len(values)}]
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, int((value - low) / width))
        counts[index] += 1
    return [{"low": round(low + i * width, 1),
             "high": round(low + (i + 1) * width, 1),
             "count": counts[i]} for i in range(bins)]


def _summarise(values):
    return {
        "runs": len(values),
        "mean": round(statistics.fmean(values), 1) if values else 0.0,
        "median": round(statistics.median(values), 1) if values else 0.0,
        "stdev": round(statistics.pstdev(values), 1) if len(values) > 1 else 0.0,
        "p10": _percentile(values, 0.10),
        "p90": _percentile(values, 0.90),
        "min": round(min(values), 1) if values else 0.0,
        "max": round(max(values), 1) if values else 0.0,
        "histogram": _histogram(values),
    }


def run(board, state, shape, my_slot, opponent_rosters, my_players,
        candidate_ids, runs=500, seed=20260830, progress=None):
    """Simulate the rest of the draft `runs` times for each candidate.

    Returns a dict with one entry per candidate plus a paired head-to-head
    comparison against the best candidate. Because every candidate faces the
    same seeded drafts, that comparison is far more sensitive than the gap
    between two means.
    """
    if not my_slot:
        return {"error": "Draft slot is not known yet, so there is nothing to "
                         "simulate. Set it in the Data panel to run this early."}

    ctx = Context(board, state, shape, my_slot, opponent_rosters, my_players)
    if ctx.current_pick > ctx.total_picks:
        return {"error": "The draft is over."}

    candidates = []
    for pid in candidate_ids:
        index = ctx.index_of.get(pid)
        if index is not None:
            candidates.append((pid, index))
    if not candidates:
        return {"error": "None of those players are still available."}

    seeds = [seed + i for i in range(runs)]
    by_id = {p["player_id"]: p for p in board}
    results = {}
    totals_by_candidate = {}
    unfilled_by_candidate = {}

    total_work = len(candidates) * runs
    done = 0
    for pid, index in candidates:
        totals, unfilled_runs = [], 0
        for run_seed in seeds:
            total, unfilled = _run_once(ctx, index, run_seed)
            totals.append(total)
            if unfilled:
                unfilled_runs += 1
            done += 1
            if progress and done % 200 == 0:
                progress(done, total_work)
        totals_by_candidate[pid] = totals
        unfilled_by_candidate[pid] = unfilled_runs
        summary = _summarise(totals)
        summary.update({
            "player_id": pid,
            "name": ctx.name[index],
            "position": ctx.position[index],
            "team": (by_id.get(pid) or {}).get("team"),
            "incomplete_lineup_runs": unfilled_runs,
        })
        results[pid] = summary

    best_pid = max(results, key=lambda pid: results[pid]["mean"])
    best_totals = totals_by_candidate[best_pid]
    for pid, summary in results.items():
        if pid == best_pid:
            summary["beats_best_pct"] = None
            summary["mean_gap"] = 0.0
            continue
        totals = totals_by_candidate[pid]
        diffs = [a - b for a, b in zip(totals, best_totals)]
        wins = sum(1 for d in diffs if d > 0)
        ties = sum(1 for d in diffs if abs(d) < 1e-9)
        summary["beats_best_pct"] = round(100.0 * wins / len(diffs), 1)
        summary["ties_best_pct"] = round(100.0 * ties / len(diffs), 1)
        summary["mean_gap"] = round(statistics.fmean(diffs), 1)

    ranked = sorted(results.values(), key=lambda r: r["mean"], reverse=True)
    return {
        "ok": True,
        "runs": runs,
        "from_pick": ctx.current_pick,
        "round": ctx.round_at.get(ctx.current_pick),
        "pool_size": ctx.size,
        "best": best_pid,
        "candidates": ranked,
        "summary": _verdict(ranked),
    }


def _verdict(ranked):
    """One plain-English sentence a beginner can act on."""
    if not ranked:
        return ""
    best = ranked[0]
    if len(ranked) == 1:
        return ("Taking %s projects to about %.0f points from your starting "
                "lineup." % (best["name"], best["mean"]))
    second = ranked[1]
    gap = best["mean"] - second["mean"]
    loses = second.get("beats_best_pct")

    if gap < 2.0:
        return ("%s and %s finish within %.1f points of each other across %d "
                "simulated drafts - this is a coin flip, so take whichever you "
                "prefer." % (best["name"], second["name"], gap, best["runs"]))
    confidence = ""
    if loses is not None:
        confidence = (" %s came out ahead in only %.0f%% of identical drafts."
                      % (second["name"], loses))
    return ("Taking %s projects to %.0f points from your starting lineup, about "
            "%.0f more than %s.%s"
            % (best["name"], best["mean"], gap, second["name"], confidence))
