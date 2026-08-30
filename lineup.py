#!/usr/bin/env python3
"""Who to start this week.

    python3 lineup.py
    python3 lineup.py --week 5

Reads the roster Sleeper actually holds for you, scores every player under
your league's exact rules, and prints the best legal lineup those players can
field - plus what to change if your current Sleeper lineup differs.
"""

import argparse
import sys

import config
import team

RULE = "=" * 68


def main():
    parser = argparse.ArgumentParser(description="Weekly start/sit advice.")
    parser.add_argument("--week", type=int, default=None)
    args = parser.parse_args()

    try:
        ctx = team.load_context(week=args.week)
    except team.TeamError as exc:
        print("\n  %s\n" % exc)
        return 1

    best = team.best_lineup(ctx["owned"])

    print("\n%s" % RULE)
    print("  WEEK %d LINEUP  -  %s" % (ctx["week"], ctx["league"].get("name")))
    print("  projections: %s" % ctx["projection_source"])
    print(RULE)

    print("\n  START THESE %d" % len(best["starters"]))
    print("     %-6s %-24s %-4s %-4s %7s"
          % ("slot", "player", "pos", "team", "proj"))
    for player in best["starters"]:
        flag = ""
        if player.get("injury_status"):
            flag = "  <- %s" % player["injury_status"]
        print("     %-6s %-24s %-4s %-4s %7.1f%s"
              % (player["slot"], player["name"][:24], player["position"],
                 player.get("team") or "", player["points"], flag))
    print("     %-6s %-24s %-4s %-4s %7.1f"
          % ("", "PROJECTED TOTAL", "", "", best["total"]))

    if best["unfilled"]:
        print("\n  !! %d starting slot(s) cannot be filled. You are short a"
              % best["unfilled"])
        print("     position - check the waiver wire before kickoff.")

    if best["bench"]:
        print("\n  BENCH")
        for player in best["bench"]:
            note = ""
            ok, reason = team.playable(player)
            if not ok:
                note = "  (%s)" % reason
            print("     %-24s %-4s %-4s %7.1f%s"
                  % (player["name"][:24], player["position"],
                     player.get("team") or "", player["points"], note))

    # Compare against whatever is currently set in Sleeper.
    current = set(ctx["starters_ids"])
    recommended = {p["player_id"] for p in best["starters"]}
    if current and current != recommended:
        bench_these = current - recommended
        start_these = recommended - current
        by_id = {p["player_id"]: p for p in ctx["owned"]}
        print("\n  CHANGES TO MAKE IN SLEEPER")
        for pid in sorted(start_these,
                          key=lambda i: -(by_id.get(i, {}).get("points") or 0)):
            player = by_id.get(pid)
            if player:
                print("     START  %-24s %-4s  %5.1f pts"
                      % (player["name"][:24], player["position"], player["points"]))
        for pid in sorted(bench_these,
                          key=lambda i: -(by_id.get(i, {}).get("points") or 0)):
            player = by_id.get(pid)
            if player:
                ok, reason = team.playable(player)
                print("     BENCH  %-24s %-4s  %5.1f pts%s"
                      % (player["name"][:24], player["position"], player["points"],
                         "  (%s)" % reason if not ok else ""))
        gained = best["total"] - _total_of(current, by_id)
        if gained > 0.05:
            print("\n     Worth about %.1f points this week." % gained)
    elif current:
        print("\n  Your Sleeper lineup already matches this. Nothing to change.")

    if best["unavailable"]:
        print("\n  CANNOT PLAY - do not start these, whatever the projection")
        for player, reason in best["unavailable"]:
            print("     %-24s %-4s  %s"
                  % (player["name"][:24], player["position"], reason))

    print("\n%s" % RULE)
    if ctx["projection_source"].startswith("season"):
        print("  These are season averages, not this week's matchup. Good for")
        print("  ranking your own players against each other; weaker for a")
        print("  specific week. Sleeper's own weekly projections were not")
        print("  reachable when this ran.")
    print("  Trade offer to check?   python3 trade.py --give \"...\" --get \"...\"")
    print("%s\n" % RULE)
    return 0


def _total_of(player_ids, by_id):
    players = [by_id[p] for p in player_ids if p in by_id]
    total, _, _ = __import__("simulation").optimal_lineup(players)
    return total


if __name__ == "__main__":
    sys.exit(main())
