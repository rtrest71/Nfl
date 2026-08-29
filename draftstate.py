"""Live draft state: pick numbering, rosters, runs, needs.

The draft order is randomised when the draft starts, so nothing here assumes a
slot. We poll the draft object until `status` flips to `drafting`, read
`draft_order` (user_id -> slot), and recompute every pick number we own.
"""

import config
import valuation


# ---------------------------------------------------------------------------
# Snake pick numbering
# ---------------------------------------------------------------------------

def pick_number(round_no, slot, teams=None, draft_type="snake", reversal_round=0):
    """Overall pick number for a given round and draft slot.

    Odd rounds run 1..12, even rounds run 12..1. A "third round reversal"
    setting flips the direction from that round onward, which Sleeper exposes
    as settings.reversal_round.
    """
    teams = teams or config.TEAMS
    if draft_type == "linear":
        forward = True
    else:
        forward = (round_no % 2 == 1)
        if reversal_round and round_no >= reversal_round:
            forward = not forward
    offset = slot if forward else (teams - slot + 1)
    return (round_no - 1) * teams + offset


def my_picks(slot, teams=None, rounds=None, draft_type="snake", reversal_round=0):
    """Every pick number I own, for all rounds."""
    teams = teams or config.TEAMS
    rounds = rounds or config.ROUNDS
    return [pick_number(r, slot, teams, draft_type, reversal_round)
            for r in range(1, rounds + 1)]


def slot_of_pick(pick_no, teams=None, draft_type="snake", reversal_round=0):
    """Which draft slot owns a given overall pick number."""
    teams = teams or config.TEAMS
    round_no = (pick_no - 1) // teams + 1
    index = (pick_no - 1) % teams + 1
    if draft_type == "linear":
        forward = True
    else:
        forward = (round_no % 2 == 1)
        if reversal_round and round_no >= reversal_round:
            forward = not forward
    slot = index if forward else (teams - index + 1)
    return round_no, slot


def find_my_slot(draft, user_id):
    """Read my draft slot from the live draft object.

    Before the draft starts `draft_order` is null or absent - that is expected,
    not an error. Returns None until Sleeper randomises the order.
    """
    if not draft or not user_id:
        return None
    order = draft.get("draft_order") or {}
    slot = order.get(str(user_id))
    if slot:
        return int(slot)

    # Some drafts expose only slot_to_roster_id; cross-reference it.
    slot_to_roster = draft.get("slot_to_roster_id") or {}
    for slot_key, roster_id in slot_to_roster.items():
        if str(roster_id) == str(user_id):
            return int(slot_key)
    return None


# ---------------------------------------------------------------------------
# Pick analysis
# ---------------------------------------------------------------------------

def analyze(picks, players, teams=None, draft_type="snake", reversal_round=0):
    """Turn the raw picks list into the state the rest of the app needs."""
    teams = teams or config.TEAMS
    picks = sorted(picks or [], key=lambda p: p.get("pick_no") or 0)

    taken = set()
    rosters = {}          # draft slot -> list of position strings
    roster_players = {}   # draft slot -> list of player summaries
    by_user = {}          # user_id -> draft slot
    history = []

    for pick in picks:
        pid = str(pick.get("player_id") or "")
        if not pid:
            continue
        taken.add(pid)
        slot = pick.get("draft_slot")
        if slot is None and pick.get("pick_no"):
            _, slot = slot_of_pick(pick["pick_no"], teams, draft_type, reversal_round)
        player = players.get(pid) or {}
        pos = (player.get("position")
               or (pick.get("metadata") or {}).get("position")
               or "?").upper()
        rosters.setdefault(slot, []).append(pos)
        roster_players.setdefault(slot, []).append({
            "player_id": pid,
            "name": player.get("name") or _metadata_name(pick),
            "position": pos,
            "team": player.get("team") or (pick.get("metadata") or {}).get("team"),
            "pick_no": pick.get("pick_no"),
            "round": pick.get("round"),
        })
        if pick.get("picked_by"):
            by_user[str(pick["picked_by"])] = slot
        history.append({
            "pick_no": pick.get("pick_no"),
            "round": pick.get("round"),
            "slot": slot,
            "player_id": pid,
            "name": player.get("name") or _metadata_name(pick),
            "position": pos,
        })

    return {
        "taken": taken,
        "rosters": rosters,
        "roster_players": roster_players,
        "slot_by_user": by_user,
        "history": history,
        "pick_count": len(history),
    }


def _metadata_name(pick):
    meta = pick.get("metadata") or {}
    name = " ".join(x for x in (meta.get("first_name"), meta.get("last_name")) if x)
    return name.strip() or str(pick.get("player_id") or "unknown")


def detect_run(history, threshold=None, window=None):
    """Flag a positional run: N+ players at one position in the last M picks."""
    threshold = threshold or config.RUN_THRESHOLD
    window = window or config.RUN_WINDOW
    recent = history[-window:]
    counts = {}
    for pick in recent:
        pos = pick.get("position")
        if pos in ("RB", "WR", "TE", "QB"):
            counts[pos] = counts.get(pos, 0) + 1
    runs = [{"position": pos, "count": n, "window": len(recent)}
            for pos, n in counts.items() if n >= threshold]
    runs.sort(key=lambda r: r["count"], reverse=True)
    return runs


def opponent_needs(rosters, slots_before_me):
    """How many of the managers picking before my next turn need each position.

    If the three managers ahead of me all need a running back, the receiver I
    want is likelier to survive - that feeds the survival maths as an ADP shift.
    """
    counts = {}
    for slot in slots_before_me:
        needs = valuation.roster_needs(rosters.get(slot, []))
        for pos in ("QB", "RB", "WR", "TE"):
            if needs.get(pos, 0) > 0:
                counts[pos] = counts.get(pos, 0) + 1
        # A team short at flex will take the best available RB or WR.
        if needs.get("FLEX", 0) > 0 and needs.get("RB", 0) <= 0 and needs.get("WR", 0) <= 0:
            counts["RB"] = counts.get("RB", 0) + 0.5
            counts["WR"] = counts.get("WR", 0) + 0.5
    return counts


def slots_between(current_pick, my_next_pick, teams=None,
                  draft_type="snake", reversal_round=0):
    """Draft slots that pick between now and my next turn."""
    teams = teams or config.TEAMS
    if not my_next_pick or my_next_pick <= current_pick:
        return []
    out = []
    for pick_no in range(current_pick, my_next_pick):
        _, slot = slot_of_pick(pick_no, teams, draft_type, reversal_round)
        out.append(slot)
    return out


def bye_conflicts(roster_players, byes=None):
    """Warn when two or more starters at the same position share a bye week."""
    byes = byes or {}
    grouped = {}
    for player in roster_players:
        bye = player.get("bye") or byes.get(player.get("team"))
        if not bye:
            continue
        pos = player.get("position")
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        grouped.setdefault((pos, bye), []).append(player["name"])

    warnings = []
    for (pos, bye), names in grouped.items():
        if len(names) >= 2:
            warnings.append({
                "position": pos, "bye": bye, "players": names,
                "message": "%d of your %ss are all off in week %d: %s."
                           % (len(names), pos, bye, ", ".join(names)),
            })
    return warnings


def roster_report(my_roster_players):
    """Filled versus empty starting slots, and needs ranked by urgency."""
    positions = [p["position"] for p in my_roster_players]
    needs = valuation.roster_needs(positions)

    slots = []
    counts = dict(needs)
    for pos, required in config.STARTERS.items():
        filled = required - max(0, counts.get(pos, 0))
        for i in range(required):
            slots.append({"slot": pos, "filled": i < filled})
    flex_remaining = counts.get("FLEX", 0)
    for i in range(config.FLEX_SLOTS):
        slots.append({"slot": "FLEX", "filled": i < (config.FLEX_SLOTS - flex_remaining)})

    bench_used = max(0, len(positions) - (sum(config.STARTERS.values()) + config.FLEX_SLOTS
                                          - valuation.required_slots_remaining(needs)))
    for i in range(config.BENCH):
        slots.append({"slot": "BN", "filled": i < min(bench_used, config.BENCH)})

    # Urgency: scarce positions first, then how many slots are open.
    urgency = {"RB": 3, "WR": 3, "TE": 2, "QB": 1, "K": 0, "DEF": 0, "FLEX": 2}
    ranked = sorted(
        [(pos, n) for pos, n in needs.items() if n > 0],
        key=lambda item: (urgency.get(item[0], 0), item[1]), reverse=True)

    return {
        "needs": needs,
        "slots": slots,
        "ranked_needs": [{"position": pos, "count": n} for pos, n in ranked],
        "counts": {pos: positions.count(pos) for pos in set(positions)},
        "total": len(positions),
    }


def imbalance_warnings(my_roster_players, picks_left):
    """Loud warnings when the remaining rounds cannot fill every starting slot."""
    positions = [p["position"] for p in my_roster_players]
    needs = valuation.roster_needs(positions)
    must_fill = valuation.required_slots_remaining(needs)
    warnings = []

    if picks_left < must_fill:
        warnings.append(
            "You have %d picks left but %d empty starting slots. You cannot fill "
            "a full lineup - draft only positions you still need."
            % (picks_left, must_fill))
    elif picks_left == must_fill and must_fill > 0:
        missing = ", ".join("%s x%d" % (pos, n) for pos, n in needs.items() if n > 0)
        warnings.append(
            "Every remaining pick must fill a starting slot: %s." % missing)

    counts = {pos: positions.count(pos) for pos in set(positions)}
    if counts.get("RB", 0) >= 5 and counts.get("WR", 0) <= 1:
        warnings.append("You have %d running backs and %d receivers. This is a "
                        "full-PPR league - you need receivers."
                        % (counts.get("RB", 0), counts.get("WR", 0)))
    if counts.get("WR", 0) >= 6 and counts.get("RB", 0) <= 1:
        warnings.append("You have %d receivers and %d running backs. You still need "
                        "to fill two starting running back slots."
                        % (counts.get("WR", 0), counts.get("RB", 0)))
    if counts.get("QB", 0) >= 2:
        warnings.append("You have %d quarterbacks but only one can start."
                        % counts.get("QB", 0))
    return warnings


def tier_cliff_alerts(board, taken, positions=("TE", "RB", "WR", "QB")):
    """Flag positions where only a couple of players remain in the top tier.

    Tight ends in particular fall off a cliff: after the top few, they are
    replacement level for a long stretch.
    """
    alerts = []
    for pos in positions:
        available = [p for p in board
                     if p["position"] == pos and p["player_id"] not in taken]
        if not available:
            continue
        available.sort(key=lambda p: p["vor"], reverse=True)
        top_tier = available[0].get("tier")
        remaining = [p for p in available if p.get("tier") == top_tier]
        if 0 < len(remaining) <= 2:
            drop = 0.0
            below = [p for p in available if p.get("tier") != top_tier]
            if below:
                drop = round(remaining[-1]["vor"] - below[0]["vor"], 1)
            if drop >= 8:
                alerts.append({
                    "position": pos,
                    "remaining": len(remaining),
                    "drop": drop,
                    "names": [p["name"] for p in remaining],
                    "message": "%s CLIFF - only %d left at the top level, then a "
                               "%.0f point drop." % (pos, len(remaining), drop),
                })
    return alerts
