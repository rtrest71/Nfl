"""The valuation engine: VOR, survival probability, tiers, and risk.

Three layers, per spec section 4:

  1. VOR      - convert raw projected points into value over the replacement
                player at that position, so a QB and a WR are comparable.
  2. Survival - snake drafting is not "take the best guy", it is "take the guy
                who will not be there at your next pick, and wait on the one
                who will". Everything is scored across a PAIR of picks.
  3. Risk     - balanced profile: floor early, upside late, injury punished
                hard because this league has no IR slots.
"""

import math

import config
import scoring as scoring_mod

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


# ---------------------------------------------------------------------------
# Small maths helpers
# ---------------------------------------------------------------------------

def normal_cdf(z):
    """Phi(z) - the standard normal CDF, via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def survival_probability(adp_mean, adp_stdev, pick_no):
    """P(player is still on the board when pick `pick_no` comes around).

    P = 1 - Phi((pick_no - adp_mean) / adp_stdev)

    A player whose ADP is far before our pick has survival near zero; one whose
    ADP is far after has survival near one.
    """
    if adp_mean is None:
        return 0.5
    stdev = max(float(adp_stdev or 0.0), config.ADP_STDEV_MIN)
    return max(0.0, min(1.0, 1.0 - normal_cdf((pick_no - adp_mean) / stdev)))


def default_stdev(adp_mean):
    """Estimate ADP spread when the source gives none.

    Consensus is tight at the top and loose late, so the band widens.
    """
    if adp_mean is None:
        return config.ADP_STDEV_MIN
    fraction = (config.ADP_STDEV_LATE_FRACTION
                if adp_mean > config.ADP_STDEV_LATE_THRESHOLD
                else config.ADP_STDEV_FRACTION)
    return max(config.ADP_STDEV_MIN, adp_mean * fraction)


def expected_best(candidates, pick_no, key="adj_vor"):
    """Expected value of the best player still available at `pick_no`.

    Under independence, E[max] = sum over players in value order of
    value_i * P(i survives) * product over better players of P(that one is gone).
    """
    total = 0.0
    all_gone = 1.0
    for player in sorted(candidates, key=lambda p: p.get(key, 0.0), reverse=True):
        p_survive = survival_probability(
            player.get("adp"), player.get("adp_stdev"), pick_no)
        total += player.get(key, 0.0) * p_survive * all_gone
        all_gone *= (1.0 - p_survive)
        if all_gone < 1e-4:
            break
    return total


# ---------------------------------------------------------------------------
# Board construction
# ---------------------------------------------------------------------------

def compute_points(player, projection, scoring_settings=None):
    """Projected fantasy points for one player, from raw stats.

    A pasted table may carry a ready-made points column; we use it only when
    there are no raw stats to score, and mark it so the UI can say so.
    """
    if not projection:
        return 0.0, "none"
    stats = projection.get("stats") or {}
    if stats:
        pts = scoring_mod.fantasy_points(
            stats, player.get("position"), scoring_settings)
        if pts:
            return pts, projection.get("source", "stats")
    if projection.get("points_override") is not None:
        return float(projection["points_override"]), "source points column"
    return 0.0, "none"


def _position_baselines(by_position):
    """Replacement-level points for each position, from the config baselines."""
    wanted = config.baselines()
    out = {}
    for pos, players in by_position.items():
        rank = wanted.get(pos)
        if not rank:
            out[pos] = 0.0
            continue
        ranked = sorted(players, key=lambda p: p["points"], reverse=True)
        if not ranked:
            out[pos] = 0.0
        elif len(ranked) >= rank:
            out[pos] = ranked[rank - 1]["points"]
        else:
            # Fewer players than the baseline rank: use the worst we have, so
            # VOR stays defined rather than exploding.
            out[pos] = ranked[-1]["points"]
    return out


def assign_tiers(players):
    """Cut tiers where the VOR gap exceeds ~1.5x the running average gap.

    Mutates each player dict with `tier` and `tier_last` (True when a player is
    the last of his tier, which is what drives the TIER BREAK warning).
    """
    ranked = sorted(players, key=lambda p: p["vor"], reverse=True)
    if not ranked:
        return
    tier = 1
    gaps = []
    for idx, player in enumerate(ranked):
        if idx > 0:
            gap = ranked[idx - 1]["vor"] - player["vor"]
            average = sum(gaps) / len(gaps) if gaps else 0.0
            if (len(gaps) >= config.TIER_MIN_PLAYERS
                    and average > 0
                    and gap > config.TIER_GAP_MULTIPLIER * average):
                tier += 1
                gaps = []
            else:
                gaps.append(gap)
        player["tier"] = tier
    for idx, player in enumerate(ranked):
        nxt = ranked[idx + 1] if idx + 1 < len(ranked) else None
        player["tier_last"] = bool(nxt and nxt["tier"] != player["tier"])
        player["tier_size"] = sum(1 for p in ranked if p["tier"] == player["tier"])
        if player["tier_last"] and nxt:
            following = [p for p in ranked[idx + 1:] if p["tier"] == nxt["tier"]]
            player["next_tier_size"] = len(following)
            player["next_tier_drop"] = round(player["vor"] - nxt["vor"], 1)
        else:
            player["next_tier_size"] = 0
            player["next_tier_drop"] = 0.0


def risk_adjustment(player, current_round):
    """Balanced-profile adjustment. Returns (delta_points, reasons).

    Rounds 1-6 weight floor and reliability; rounds 7-15 weight upside. Only
    factors we can actually derive from Sleeper's data are applied - nothing
    here is invented. Factors the spec lists that need target-share or
    red-zone data are surfaced only if a pasted table supplies them.
    """
    delta = 0.0
    reasons = []
    pos = (player.get("position") or "").upper()
    late = current_round >= config.UPSIDE_CROSSOVER_ROUND

    # Injury, weighted heavily: no IR slots and only 5 bench spots.
    status = player.get("injury_status")
    if status:
        penalty = config.INJURY_PENALTY.get(status)
        if penalty is None:
            penalty = config.INJURY_PENALTY.get(str(status).title(), 0.0)
        if penalty:
            # Early picks are where an injury hurts most; late fliers can absorb it.
            scaled = penalty * (1.0 if not late else 0.6)
            delta -= scaled
            reasons.append("injury risk (%s) - no IR slot in this league" % status)

    # Age curve.
    age = player.get("age")
    if isinstance(age, (int, float)) and age:
        low, high = config.AGE_SWEET_SPOT.get(pos, (0, 99))
        if low <= age <= high:
            delta += config.AGE_BOOST
            reasons.append("age %d is the productive window for a %s" % (int(age), pos))
        if pos == "RB" and age >= config.RB_AGE_CLIFF:
            delta -= config.RB_AGE_CLIFF_PENALTY
            reasons.append("running back aged %d - production usually falls off" % int(age))
        if pos == "WR" and age >= config.WR_AGE_CLIFF:
            delta -= config.WR_AGE_CLIFF_PENALTY
            reasons.append("receiver aged %d" % int(age))

    # Depth chart: a backup is a wasted roster spot in a 5-bench, no-IR league.
    order = player.get("depth_chart_order")
    if isinstance(order, (int, float)) and order and order >= 2:
        if pos == "RB":
            delta -= config.BACKUP_RB_PENALTY * (1.0 if not late else 0.5)
            reasons.append("listed behind another back on the depth chart")
        elif pos == "WR":
            delta -= config.BACKUP_WR_PENALTY * (1.0 if not late else 0.5)
            reasons.append("not a starting receiver on his own team")

    # Value gap: the market is sleeping on him.
    gap = player.get("value_gap")
    if isinstance(gap, (int, float)) and gap > 0:
        boost = min(config.VALUE_GAP_MAX_BOOST, gap * config.VALUE_GAP_POINTS_PER_PICK)
        delta += boost
        if gap >= 12:
            reasons.append("going %d picks later than his production deserves" % int(gap))

    # Late-round upside tilt: reward young players with room to grow.
    if late:
        exp = player.get("years_exp")
        if isinstance(exp, (int, float)) and exp is not None and exp <= 2 and pos in ("RB", "WR", "TE"):
            delta += 5.0
            reasons.append("young enough to break out")

    return delta, reasons


def build_board(players, projections, adp, scoring_settings=None, current_round=1,
                byes=None):
    """Assemble the full valuation board.

    Returns a list of player dicts sorted by adjusted VOR, each carrying the
    numbers the UI needs: points, vor, adp, tier, value gap and risk reasons.
    """
    byes = byes or {}
    board = []
    for pid, player in players.items():
        pos = (player.get("position") or "").upper()
        if pos not in POSITIONS:
            continue
        projection = (projections or {}).get(pid)
        points, source = compute_points(player, projection, scoring_settings)
        entry = dict(player)
        entry.update({
            "player_id": pid,
            "position": pos,
            "points": round(points, 2),
            "points_source": source,
            "estimated": bool(projection and projection.get("estimated")),
        })
        bye = None
        if projection and projection.get("bye"):
            bye = projection["bye"]
        elif byes.get(entry.get("team")):
            bye = byes[entry["team"]]
        entry["bye"] = bye
        board.append(entry)

    by_position = {}
    for entry in board:
        by_position.setdefault(entry["position"], []).append(entry)

    baselines = _position_baselines(by_position)
    for entry in board:
        entry["baseline"] = round(baselines.get(entry["position"], 0.0), 2)
        entry["vor"] = round(entry["points"] - entry["baseline"], 2)

    # Positional rank by points, and tiers by VOR gap.
    for pos, group in by_position.items():
        for rank, entry in enumerate(
                sorted(group, key=lambda p: p["points"], reverse=True), start=1):
            entry["pos_rank"] = rank
            entry["pos_label"] = "%s%d" % (pos, rank)
        assign_tiers(group)

    # ADP, with Sleeper's own search_rank as the fallback ordering when no ADP
    # has been pasted. search_rank is Sleeper's internal popularity ordering,
    # which is a far better proxy for Sleeper ADP than any other free source.
    adp = adp or {}
    have_real_adp = bool(adp)
    fallback_order = sorted(
        [e for e in board if e.get("search_rank") is not None],
        key=lambda e: e["search_rank"])
    fallback_rank = {e["player_id"]: idx + 1 for idx, e in enumerate(fallback_order)}

    for entry in board:
        record = adp.get(entry["player_id"])
        if record and record.get("adp"):
            entry["adp"] = float(record["adp"])
            entry["adp_source"] = "pasted"
            entry["adp_stdev"] = float(record.get("stdev") or default_stdev(entry["adp"]))
        else:
            rank = fallback_rank.get(entry["player_id"])
            if rank:
                entry["adp"] = float(rank)
                entry["adp_source"] = "sleeper search rank (estimated)"
                # Wider band, because this is a proxy rather than real ADP.
                entry["adp_stdev"] = default_stdev(float(rank)) * 1.3
            else:
                entry["adp"] = None
                entry["adp_source"] = "unknown"
                entry["adp_stdev"] = config.ADP_STDEV_MIN
        if record and record.get("bye") and not entry.get("bye"):
            entry["bye"] = record["bye"]
    if not have_real_adp:
        for entry in board:
            entry["adp_estimated"] = True

    # Value gap: where the market has him versus where his production says.
    #
    # Both ranks are computed within the DRAFTABLE pool only. Ranking across
    # every player in the database made the gap meaningless - a kicker with an
    # ADP in the thousands scored +2900 and saturated the value boost, which
    # said nothing except "nobody drafts this player".
    draftable = [e for e in board
                 if e["adp"] is not None and e["adp"] <= config.VALUE_GAP_ADP_LIMIT]

    for entry in board:
        entry["draftable"] = False
        entry["value_gap"] = 0
        entry["vor_rank"] = None
        entry["adp_rank"] = None

    for rank, entry in enumerate(
            sorted(draftable, key=lambda e: e["vor"], reverse=True), start=1):
        entry["vor_rank"] = rank
        entry["draftable"] = True
    for rank, entry in enumerate(
            sorted(draftable, key=lambda e: e["adp"]), start=1):
        entry["adp_rank"] = rank
    for entry in draftable:
        entry["value_gap"] = entry["adp_rank"] - entry["vor_rank"]

    for entry in board:
        delta, reasons = risk_adjustment(entry, current_round)
        entry["risk_delta"] = round(delta, 2)
        entry["risk_reasons"] = reasons
        entry["adj_vor"] = round(entry["vor"] + delta, 2)

    board.sort(key=lambda e: e["adj_vor"], reverse=True)
    for rank, entry in enumerate(board, start=1):
        entry["overall_rank"] = rank
    return board


# ---------------------------------------------------------------------------
# Eligibility - the league-specific hard blocks from spec section 5
# ---------------------------------------------------------------------------

def roster_needs(roster_positions):
    """Which starting slots are still empty, given the positions I have drafted.

    Fills mandatory starters first, then the two flex slots from the leftovers.
    Returns a dict of position -> unfilled starter count, plus flex remaining.
    """
    counts = {}
    for pos in roster_positions:
        counts[pos] = counts.get(pos, 0) + 1

    needs = {}
    leftovers = dict(counts)
    for pos, required in config.STARTERS.items():
        have = min(leftovers.get(pos, 0), required)
        leftovers[pos] = leftovers.get(pos, 0) - have
        needs[pos] = required - have

    flex_filled = sum(leftovers.get(pos, 0) for pos in config.FLEX_ELIGIBLE)
    flex_remaining = max(0, config.FLEX_SLOTS - flex_filled)
    needs["FLEX"] = flex_remaining
    return needs


def required_slots_remaining(needs):
    return sum(v for k, v in needs.items() if v > 0)


def eligible(player, state):
    """Can this player be recommended right now? Returns (bool, reason).

    These are the soft constraints from spec section 5, encoded as hard blocks
    because under a 2-minute clock a warning you can override is a warning you
    will click through by accident.
    """
    pos = player["position"]
    rnd = state["round"]

    if player["player_id"] in state["taken"]:
        return False, "already drafted"

    if pos == "QB":
        if state["needs"].get("QB", 0) <= 0:
            return False, "you already have your starting quarterback"
        if rnd < config.QB_UNLOCK_ROUND:
            fallen = (player["adp"] is not None
                      and state["current_pick"] - player["adp"] >= config.QB_ELITE_ADP_FALL)
            elite = player["vor"] >= config.QB_ELITE_STEAL_VOR
            if not (fallen and elite):
                return False, ("only one QB starts in this league - wait until round %d"
                               % config.QB_UNLOCK_ROUND)

    if pos == "K":
        if rnd < config.K_UNLOCK_ROUND:
            return False, "kickers go in round %d" % config.K_UNLOCK_ROUND
        if state["needs"].get("K", 0) <= 0:
            return False, "you already have a kicker"

    if pos == "DEF":
        if rnd < config.DEF_UNLOCK_ROUND:
            return False, "defenses go in round %d" % config.DEF_UNLOCK_ROUND
        if state["needs"].get("DEF", 0) <= 0:
            return False, "you already have a defense"

    # No IR and 5 bench spots: no room for handcuff backups until the very end.
    # The one exception is the backup to a back you already own - that is not a
    # handcuff, it is insurance on your most valuable asset.
    order = player.get("depth_chart_order")
    if (isinstance(order, (int, float)) and order and order >= 2 and pos in ("RB", "WR")
            and rnd < config.HANDCUFF_BLOCK_ROUND):
        if not (rnd >= config.OWN_HANDCUFF_UNLOCK_ROUND and is_own_handcuff(player, state)):
            return False, "backup on his own team - no bench room for handcuffs here"

    # Roster completion: if I have exactly as many picks left as empty starting
    # slots, every remaining pick must fill one.
    picks_left = state["picks_left"]
    must_fill = required_slots_remaining(state["needs"])
    if picks_left <= must_fill:
        fills = state["needs"].get(pos, 0) > 0 or (
            pos in config.FLEX_ELIGIBLE and state["needs"].get("FLEX", 0) > 0)
        if not fills:
            return False, "you must fill empty starting slots with your last picks"

    # Do not stack a position far beyond what can start.
    have = sum(1 for p in state["my_roster"] if p == pos)
    cap = {"QB": 2, "TE": 2, "K": 1, "DEF": 1, "RB": 6, "WR": 7}.get(pos, 6)
    if have >= cap:
        return False, "you have enough at %s" % pos

    return True, ""


def is_own_handcuff(player, state):
    """True when this player backs up a running back already on my roster.

    With no IR slots, an injury to your lead back and no replacement ends your
    season. Backing up your OWN starter is the highest-leverage bench pick
    available; backing up someone else's is a wasted roster spot.
    """
    if (player.get("position") or "").upper() != "RB":
        return False
    team = player.get("team")
    if not team or team == "FA":
        return False
    for owned in state.get("my_players") or []:
        if (owned.get("position") == "RB"
                and owned.get("team") == team
                and owned.get("player_id") != player.get("player_id")):
            return True
    return False


def lineup_multiplier(position, roster_positions):
    """Discount a player who would sit on the bench rather than start.

    VOR is the value a player delivers in your starting lineup. Once a position
    is full - its own starting slots plus whatever the flex slots can absorb -
    the next player there only contributes as injury cover, and his score is
    discounted accordingly. This is what keeps a full-PPR roster balanced
    without ever overriding VOR with a hunch about a position.
    """
    counts = {}
    for pos in roster_positions:
        counts[pos] = counts.get(pos, 0) + 1

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


def opponent_adp_shift(position, state):
    """Shift a position's effective ADP earlier when opponents need it.

    If the managers picking before my next turn are short at this position, its
    players will go sooner than raw ADP suggests. Capped so it can nudge the
    survival maths but never overwhelm the real ADP signal.
    """
    needy = state.get("opponent_needs", {}).get(position, 0)
    if not needy:
        return 0.0
    return min(config.OPPONENT_NEED_MAX_SHIFT,
               needy * config.OPPONENT_NEED_SHIFT_PER_MANAGER)


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------

def plain_reason(player, state, p_survive, edge, tier_break):
    """A plain-English reason a beginner can act on in ten seconds.

    The user has never watched an NFL game, so: no jargon, no acronyms beyond
    the position, and always an actionable "why now".
    """
    bits = []
    gap_to_next = state.get("picks_until_next")

    if p_survive is not None and gap_to_next:
        pct = int(round(p_survive * 100))
        gap_phrase = ("the one pick until your next turn" if gap_to_next == 1
                      else "the %d picks until your next turn" % gap_to_next)
        if pct <= 15:
            bits.append("He almost certainly will not last %s." % gap_phrase)
        elif pct <= 45:
            bits.append("Only about a %d%% chance he lasts %s." % (pct, gap_phrase))
        elif pct >= 80:
            bits.append("He will probably still be here at your next pick, but "
                        "nobody better is likely to be.")
        else:
            bits.append("Roughly a coin flip whether he lasts to your next turn.")

    if player.get("own_handcuff"):
        bits.append("He is the backup to a running back you already own. If yours "
                    "gets hurt, this is the man who takes over - and this league "
                    "has no injured-reserve spot to absorb that.")

    needs = state.get("needs") or {}
    if needs.get(player["position"], 0) > 0:
        bits.append("He fills an empty starting slot on your team.")
    elif player.get("lineup_multiplier", 1.0) < 1.0:
        bits.append("He would start only if someone ahead of him gets hurt, so he "
                    "is worth less to you than his raw ranking suggests.")

    if player.get("value_gap", 0) >= 10:
        bits.append("Other drafters are letting him slide about %d picks past what "
                    "his projected scoring is worth." % int(player["value_gap"]))

    if tier_break:
        bits.append("He is the last of his group - the next %s are meaningfully worse."
                    % (("%d %ss" % (player.get("next_tier_size", 0), player["position"]))
                       if player.get("next_tier_size") else "few"))

    if player["position"] in ("WR", "RB") and player.get("points"):
        bits.append("Every catch is a full point in this league, and he is projected "
                    "for %.0f points." % player["points"])
    elif player.get("points"):
        bits.append("Projected for %.0f points this season." % player["points"])

    if edge is not None and edge > 0:
        bits.append("Taking him now rather than gambling on him lasting is worth "
                    "about %.1f points." % edge)

    for reason in player.get("risk_reasons", [])[:1]:
        bits.append("Worth knowing: %s." % reason)

    return " ".join(bits[:4])


def recommend(board, state, limit=5):
    """Rank the available players across my next TWO picks.

    take_now = VOR(player) + E[best VOR available at my next pick]
    wait     = E[VOR of best alternative now]
               + P(survives) * VOR(player)
               + (1 - P(survives)) * E[next-best at that position]

    We recommend whichever candidate maximises total expected VOR across the
    pair, and report the delta in points so the reasoning is visible.
    """
    available = [p for p in board if p["player_id"] not in state["taken"]]
    candidates = []
    for player in available:
        ok, reason = eligible(player, state)
        if ok:
            candidates.append(player)
        else:
            player["blocked_reason"] = reason

    if not candidates:
        return {"top": None, "alternatives": [], "candidates": []}

    next_pick = state.get("my_next_pick")
    # Apply the opponent-need shift to each candidate's effective ADP, and
    # discount anyone who would be a bench player on my roster as it stands.
    for player in candidates:
        shift = opponent_adp_shift(player["position"], state)
        player["adp_effective"] = (player["adp"] - shift) if player["adp"] else None
        player["opponent_shift"] = shift
        multiplier = lineup_multiplier(player["position"], state["my_roster"])
        player["lineup_multiplier"] = multiplier
        value = player["adj_vor"] * multiplier
        # Insurance on a back you already own is worth more than his raw value
        # suggests, because what it protects against is losing your season.
        player["own_handcuff"] = (state["round"] >= config.OWN_HANDCUFF_UNLOCK_ROUND
                                  and is_own_handcuff(player, state))
        if player["own_handcuff"]:
            value += config.OWN_HANDCUFF_BONUS
        player["lineup_vor"] = round(value, 2)
        player["would_start"] = multiplier >= 1.0

    def survives(player):
        if not next_pick:
            return None
        return survival_probability(
            player.get("adp_effective") if player.get("adp_effective") is not None
            else player.get("adp"),
            player.get("adp_stdev"), next_pick)

    # Only the plausible top of the board matters for the expected-max maths,
    # and truncating keeps this fast enough to run every 3 seconds.
    pool = sorted(candidates, key=lambda p: p["lineup_vor"], reverse=True)[:80]

    scored = []
    for player in pool:
        others = [p for p in pool if p["player_id"] != player["player_id"]]
        p_survive = survives(player)

        if next_pick:
            best_next = expected_best(others, next_pick, key="lineup_vor")
            same_pos = [p for p in others if p["position"] == player["position"]]
            best_next_pos = expected_best(same_pos, next_pick, key="lineup_vor")
        else:
            best_next = best_next_pos = 0.0

        best_now = others[0]["lineup_vor"] if others else 0.0

        take_now = player["lineup_vor"] + best_next
        if p_survive is None:
            wait = best_now
        else:
            wait = (best_now
                    + p_survive * player["lineup_vor"]
                    + (1.0 - p_survive) * best_next_pos)

        entry = {
            "player": player,
            "take_now": round(take_now, 2),
            "wait": round(wait, 2),
            "edge": round(take_now - wait, 2),
            "p_survive": None if p_survive is None else round(p_survive, 3),
        }
        scored.append(entry)

    scored.sort(key=lambda e: e["take_now"], reverse=True)
    top = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None

    # When the top options are within a point of each other the pick genuinely
    # does not matter. Say so, rather than presenting a hair-splitting number
    # as if it were a finding - especially since the take-now-versus-wait edge
    # can drift slightly negative between interchangeable players.
    gap_to_second = (top["take_now"] - runner_up["take_now"]) if runner_up else 0.0
    close_call = bool(runner_up and gap_to_second < 1.0)

    for entry in scored[:limit]:
        player = entry["player"]
        tier_break = bool(player.get("tier_last") and player.get("next_tier_size"))
        entry["tier_break"] = tier_break
        entry["reason"] = plain_reason(
            player, state, entry["p_survive"], entry["edge"], tier_break)
        entry["cost_vs_top"] = round(top["take_now"] - entry["take_now"], 2)

    return {
        "top": top,
        "alternatives": scored[1:limit],
        "delta_to_next": round(gap_to_second, 2),
        "close_call": close_call,
        "candidates": scored[:limit],
    }


def value_board(board, state, limit=15):
    """Top positive-value-gap players still available.

    The "underdogs who statistically should succeed" panel: players whose
    projected production outruns where the market is drafting them.
    """
    available = [p for p in board
                 if p["player_id"] not in state["taken"]
                 and p.get("draftable")
                 and p.get("value_gap", 0) > 0
                 and p.get("points", 0) > 0
                 # A bargain you would never start is not a bargain.
                 and p.get("vor", 0) > 0
                 and p["position"] not in ("K", "DEF")]
    available.sort(key=lambda p: (p["value_gap"], p["vor"]), reverse=True)
    return available[:limit]


def build_queue(board, state, length=None):
    """A ranked queue to paste into Sleeper's own draft queue.

    Insurance: if the wifi drops or the clock runs out, Sleeper autopicks from
    this instead of grabbing something random. Regenerated as the draft goes.
    """
    length = length or config.QUEUE_LENGTH
    available = [p for p in board if p["player_id"] not in state["taken"]]

    queue = []
    # Walk forward through my remaining picks so the queue respects the
    # positional blocks rather than front-loading kickers.
    simulated_roster = list(state["my_roster"])
    simulated_taken = set(state["taken"])
    my_remaining = state.get("my_remaining_picks") or []

    # Plan every remaining ROUND, not just the pick numbers we happen to know.
    # Before the draft starts the slot is unknown, so we own no pick numbers
    # yet - and a queue built from that produced three players instead of
    # forty, which is precisely when the queue matters most.
    planned_rounds = max(1, config.ROUNDS - state["round"] + 1)

    for offset in range(planned_rounds):
        pick_no = (my_remaining[offset] if offset < len(my_remaining)
                   else (state.get("current_pick") or 1))
        sim_state = dict(state)
        sim_state["round"] = state["round"] + offset
        sim_state["taken"] = simulated_taken
        sim_state["my_roster"] = simulated_roster
        sim_state["needs"] = roster_needs(simulated_roster)
        sim_state["picks_left"] = planned_rounds - offset
        sim_state["current_pick"] = pick_no

        # Re-rank for the roster as it will look at that pick, so the queue
        # fills empty starting slots rather than stacking one position.
        ranked = sorted(
            available,
            key=lambda p: p["adj_vor"] * lineup_multiplier(p["position"],
                                                           simulated_roster),
            reverse=True)

        picked = 0
        for player in ranked:
            if player["player_id"] in simulated_taken:
                continue
            ok, _ = eligible(player, sim_state)
            if not ok:
                continue
            queue.append(player)
            simulated_taken.add(player["player_id"])
            # Only the top choice advances the simulated roster - the other two
            # are backups for this same pick, not extra players on the team.
            if picked == 0:
                simulated_roster.append(player["position"])
            picked += 1
            # A few options per pick, so the queue survives players being sniped.
            if picked >= 3:
                break
        if len(queue) >= length:
            break

    return queue[:length]
