#!/usr/bin/env python3
"""Should you accept this trade?

    python3 trade.py --give "Kyren Williams" --get "Brock Bowers"
    python3 trade.py --give "Breece Hall, Parker Washington" --get "Puka Nacua"

Judged on the only thing that decides games: what your best STARTING LINEUP
scores before and after. A trade that adds points to your bench is not a good
trade, however good the player looks.
"""

import argparse
import sys

import config
import simulation
import team

RULE = "=" * 70


def main():
    parser = argparse.ArgumentParser(description="Evaluate a trade offer.")
    parser.add_argument("--give", default="",
                        help='Players you would send, comma separated.')
    parser.add_argument("--get", default="",
                        help='Players you would receive, comma separated.')
    parser.add_argument("--week", type=int, default=None)
    args = parser.parse_args()

    give_names = [n.strip() for n in args.give.split(",") if n.strip()]
    get_names = [n.strip() for n in args.get.split(",") if n.strip()]
    if not give_names or not get_names:
        print('\n  Name both sides:\n'
              '     python3 trade.py --give "Player A" --get "Player B"\n')
        return 1

    try:
        ctx = team.load_context(week=args.week, quiet=True)
    except team.TeamError as exc:
        print("\n  %s\n" % exc)
        return 1

    giving, missing_give = team.resolve_names(give_names, ctx)
    getting, missing_get = team.resolve_names(get_names, ctx)

    for name in missing_give + missing_get:
        print("  ! could not find '%s'" % name)

    owned_ids = {p["player_id"] for p in ctx["owned"]}
    not_yours = [p for p in giving if p["player_id"] not in owned_ids]
    for player in not_yours:
        print("  ! %s is not on your roster - are you reading the offer the "
              "right way round?" % player["name"])
    # Drop anyone already on the roster from the incoming side. Warning and
    # then evaluating anyway put the same player in two starting slots and
    # reported a lineup that cannot exist.
    already = [p for p in getting if p["player_id"] in owned_ids]
    for player in already:
        print("  ! you already own %s - ignoring him on the incoming side"
              % player["name"])
    getting = [p for p in getting if p["player_id"] not in owned_ids]

    seen, deduped = set(), []
    for player in getting:
        if player["player_id"] not in seen:
            seen.add(player["player_id"])
            deduped.append(player)
    getting = deduped

    giving = [p for p in giving if p["player_id"] in owned_ids]

    if not giving or not getting:
        print("\n  Nothing left to evaluate on one side of this trade.\n")
        return 1

    before = team.best_lineup(ctx["owned"])
    give_ids = {p["player_id"] for p in giving}
    after_roster = [p for p in ctx["owned"] if p["player_id"] not in give_ids]
    after_roster.extend(getting)
    after = team.best_lineup(after_roster)

    print("\n%s" % RULE)
    print("  TRADE CHECK  -  week %d, %s"
          % (ctx["week"], ctx["league"].get("name")))
    print("  projections: %s" % ctx["projection_source"])
    print(RULE)

    print("\n  YOU SEND")
    for player in giving:
        _line(player, before)
    print("\n  YOU GET")
    for player in getting:
        _line(player, after)

    delta = after["total"] - before["total"]
    print("\n%s" % RULE)
    print("  YOUR STARTING LINEUP")
    print("     now            %7.1f" % before["total"])
    print("     after trade    %7.1f" % after["total"])
    print("     difference     %+7.1f  points per week" % delta)
    print(RULE)

    verdict, detail = _verdict(delta, before, after, giving, getting)
    print("\n  %s" % verdict)
    for line in detail:
        print("     %s" % line)

    # Roster size and legality.
    size_after = len(after_roster)
    if size_after > config.ROSTER_SIZE:
        print("\n  ROSTER LIMIT: you would hold %d players for %d spots. You "
              "must drop %d." % (size_after, config.ROSTER_SIZE,
                                 size_after - config.ROSTER_SIZE))
        droppable = sorted(after["bench"], key=lambda p: p["points"])[:3]
        if droppable:
            print("  Cheapest to drop: %s"
                  % ", ".join("%s (%.0f)" % (p["name"], p["points"])
                              for p in droppable))
    if after["unfilled"] > before["unfilled"]:
        print("\n  !! This leaves %d starting slot(s) you cannot fill."
              % after["unfilled"])

    _depth_warning(before, after)

    print("\n  Lineup after this trade would be:")
    for player in after["starters"]:
        moved = "  <- new" if player["player_id"] in {
            p["player_id"] for p in getting} else ""
        print("     %-6s %-24s %7.1f%s"
              % (player["slot"], player["name"][:24], player["points"], moved))
    print()
    return 0


def _line(player, lineup):
    starting = player["player_id"] in {p["player_id"] for p in lineup["starters"]}
    where = "starter" if starting else "bench"
    flag = "  (%s)" % player["injury_status"] if player.get("injury_status") else ""
    print("     %-24s %-4s %-4s %7.1f   %s%s"
          % (player["name"][:24], player["position"], player.get("team") or "",
             player["points"], where, flag))


def _verdict(delta, before, after, giving, getting):
    detail = []

    # Depth matters beyond the starting eleven: a trade that is neutral this
    # week but hands away your only backup at a position is not neutral.
    given_starters = sum(
        1 for p in giving
        if p["player_id"] in {s["player_id"] for s in before["starters"]})
    if given_starters:
        detail.append("You are sending %d current starter(s)." % given_starters)

    if delta >= 8:
        return "ACCEPT. This clearly improves your lineup.", detail
    if delta >= 3:
        detail.append("A real gain, but not enormous - worth taking.")
        return "LEAN ACCEPT.", detail
    if delta > -3:
        detail.append("Inside the noise of a projection. Decide on the things")
        detail.append("the numbers cannot see: schedule, your read on the")
        detail.append("players, whether you need depth or a star.")
        return "TOO CLOSE TO CALL.", detail
    if delta > -8:
        detail.append("You would be giving up a few points a week for nothing")
        detail.append("obvious in return.")
        return "LEAN REJECT.", detail
    return "REJECT. This makes your lineup meaningfully worse.", detail


def _depth_warning(before, after):
    def counts(lineup):
        out = {}
        for player in lineup["starters"] + lineup["bench"]:
            ok, _ = team.playable(player)
            if ok:
                out[player["position"]] = out.get(player["position"], 0) + 1
        return out

    b, a = counts(before), counts(after)
    for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
        need = config.STARTERS.get(position, 0)
        if a.get(position, 0) < need:
            print("\n  !! After this you would have %d healthy %s(s) for %d "
                  "starting slot(s)." % (a.get(position, 0), position, need))
        elif b.get(position, 0) > a.get(position, 0) == need:
            print("\n  Note: this leaves you with exactly %d %s(s) and no "
                  "cover if one gets hurt." % (need, position))


if __name__ == "__main__":
    sys.exit(main())
