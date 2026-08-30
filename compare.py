#!/usr/bin/env python3
"""Settle a draft argument with your own data.

    python3 compare.py "Josh Allen" "Brock Bowers"
    python3 compare.py --pick 20 --slot 8 "Lamar Jackson" "Trey McBride"

Give it any players and it plays the rest of the draft out 500 times for each
one, then reports what your projected STARTING LINEUP scores in each case. It
deliberately ignores the app's positional rules for the players you name -
that is the point: it measures what breaking a rule actually costs.

Every candidate faces the same 500 simulated drafts, so the comparison is
paired: "this player finished ahead in 12% of identical drafts" is a far
sharper answer than two averages side by side.
"""

import argparse
import sys

import config
import draftstate
import projections as paste
import simulation
import sleeper
import valuation

RULE = "=" * 74


def main():
    parser = argparse.ArgumentParser(
        description="Compare draft choices over 500 simulated drafts.")
    parser.add_argument("players", nargs="*",
                        help="Players to compare. The app's own pick is added "
                             "automatically as the benchmark.")
    parser.add_argument("--pick", type=int, default=None,
                        help="Overall pick number to decide at (default: your "
                             "next real pick, or 1 before the draft).")
    parser.add_argument("--slot", type=int, default=None,
                        help="Draft slot to assume (default: your live slot, "
                             "or 6 before the order is set).")
    parser.add_argument("--runs", type=int, default=500)
    args = parser.parse_args()

    players = sleeper.cache_read(config.PLAYERS_CACHE, {}) or {}
    proj_blob = sleeper.cache_read(config.PROJECTIONS_CACHE, {}) or {}
    projections = proj_blob.get("players", proj_blob) or {}
    adp_blob = sleeper.cache_read(config.ADP_CACHE, {}) or {}
    adp = adp_blob.get("players", adp_blob) or {}
    league_blob = sleeper.cache_read(config.LEAGUE_CACHE, {}) or {}
    league = league_blob.get("league")
    draft = league_blob.get("draft")

    if not players or not projections:
        print("No cached data. Run: python3 build_data.py")
        return 1

    settings = None
    if league:
        live = sleeper.live_scoring_settings(league)
        if live:
            settings = dict(config.SCORING)
            settings.update(live)

    # Work out where we are deciding from.
    slot = args.slot
    if not slot and draft:
        user = (league_blob.get("user") or {}).get("user_id")
        slot = draftstate.find_my_slot(draft, user)
    assumed = False
    if not slot:
        slot, assumed = 6, True

    my_picks = draftstate.my_picks(slot)
    pick = args.pick or my_picks[0]
    round_no, _ = draftstate.slot_of_pick(pick)

    board = valuation.build_board(players, projections, adp, settings,
                                  current_round=round_no)
    by_id = {p["player_id"]: p for p in board}
    index = paste.PlayerIndex(players)

    print("\n%s" % RULE)
    print("  DECIDING AT PICK %d  (round %d, slot %d%s)"
          % (pick, round_no, slot, " - assumed" if assumed else ""))
    print("  %d simulated drafts per candidate" % args.runs)
    print(RULE)

    # Resolve the named players.
    candidates, missing = [], []
    for name in args.players:
        pid = index.match(name)
        if pid and pid in by_id:
            candidates.append(pid)
        else:
            missing.append(name)
    for name in missing:
        print("  ! could not find '%s'" % name)

    # Add the app's own choice as the benchmark, so there is always something
    # to measure the named players against.
    remaining = [p for p in my_picks if p >= pick]
    state = {
        "taken": set(), "round": round_no, "current_pick": pick,
        "my_next_pick": remaining[1] if len(remaining) > 1 else None,
        "picks_until_next": (remaining[1] - pick) if len(remaining) > 1 else None,
        "my_roster": [], "my_players": [], "my_remaining_picks": remaining,
        "needs": valuation.roster_needs([]), "picks_left": len(remaining),
        "opponent_needs": {},
    }
    recommendation = valuation.recommend(board, state)
    app_pick = None
    if recommendation.get("top"):
        app_pick = recommendation["top"]["player"]["player_id"]
        if app_pick not in candidates:
            candidates.insert(0, app_pick)

    if not candidates:
        print("  Nothing to compare. Name at least one player.")
        return 1

    print("\n  CANDIDATES (raw value before any simulation)")
    print("     %-26s %-4s %7s %8s %7s"
          % ("player", "pos", "proj", "vor", "adp"))
    for pid in candidates:
        entry = by_id[pid]
        tag = "  <- app's pick" if pid == app_pick else ""
        print("     %-26s %-4s %7.0f %8.1f %7s%s"
              % (entry["name"][:26], entry["position"], entry["points"],
                 entry["vor"],
                 ("%.1f" % entry["adp"]) if entry.get("adp") else "-", tag))

    shape = {"teams": config.TEAMS, "rounds": config.ROUNDS,
             "type": "snake", "reversal_round": 0}
    result = simulation.run(board, state, shape, slot, {}, [],
                            candidates, runs=args.runs)
    if result.get("error"):
        print("\n  %s" % result["error"])
        return 1

    print("\n  AFTER %d SIMULATED DRAFTS - your starting lineup's season total"
          % args.runs)
    print("     %-26s %-4s %8s %15s %9s %7s"
          % ("player", "pos", "avg", "likely range", "vs best", "wins"))
    for entry in result["candidates"]:
        beats = entry.get("beats_best_pct")
        print("     %-26s %-4s %8.0f %7.0f - %-7.0f %9s %6s"
              % (entry["name"][:26], entry["position"], entry["mean"],
                 entry["p10"], entry["p90"],
                 ("-" if beats is None else "%+.1f" % entry["mean_gap"]),
                 ("best" if beats is None else "%.0f%%" % beats)))

    print("\n  %s" % result["summary"])

    best = result["candidates"][0]
    if app_pick and best["player_id"] != app_pick:
        print("\n  NOTE: the simulation prefers %s over the app's own pick."
              % best["name"])
        print("  The app blocks some positions early by rule; this ignores that.")
        print("  If the gap is small, the rule is still the safer habit.")

    print("\n  How to read 'wins': across the SAME simulated drafts, how often")
    print("  that player's roster beat the top one. Under 25% is a real")
    print("  difference. Near 50% means the choice does not matter.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
