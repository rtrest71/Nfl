#!/usr/bin/env python3
"""The final check. Run this shortly before the draft.

    python3 readiness.py

Answers one question: is this thing ready to draft with? It checks the data,
the live connection to Sleeper, that it is pointed at the right draft, and that
the queue is loaded - then prints GO or NOT READY with the reason.
"""

import sys
import time

import config
import draftstate
import sleeper
import valuation

RULE = "=" * 70
PROBLEMS = []
NOTES = []


def check(label, ok, detail="", fatal=True):
    print("  [%s] %-38s %s" % ("x" if ok else " ", label, detail))
    if not ok:
        (PROBLEMS if fatal else NOTES).append("%s - %s" % (label, detail))
    return ok


def main():
    print("\n%s\n  DRAFT READINESS CHECK  -  %s\n%s"
          % (RULE, time.strftime("%A %d %B, %H:%M"), RULE))

    # ---------------------------------------------------------------- data
    print("\n  DATA")
    players = sleeper.cache_read(config.PLAYERS_CACHE, {}) or {}
    proj_blob = sleeper.cache_read(config.PROJECTIONS_CACHE, {}) or {}
    projections = proj_blob.get("players", proj_blob) or {}
    adp_blob = sleeper.cache_read(config.ADP_CACHE, {}) or {}
    adp = adp_blob.get("players", adp_blob) or {}

    check("Player database", bool(players), "%d players" % len(players))
    check("Projections", len(projections) > 200, "%d players" % len(projections))
    check("ADP", len(adp) > 200, "%d players (%s)"
          % (len(adp), adp_blob.get("source", "?")), fatal=False)

    if not players or not projections:
        return finish()

    # ------------------------------------------------------------ live link
    print("\n  LIVE CONNECTION TO SLEEPER")
    league = draft = None
    users = {}
    try:
        user = sleeper.get_user()
        check("Sleeper reachable", True, "user %s" % config.USERNAME)
        leagues = sleeper.get_leagues(user["user_id"]) or []
        league = sleeper.pick_league(leagues)
        check("League found", bool(league),
              (league or {}).get("name") or config.LEAGUE_NAME)
        if league:
            drafts = sleeper.get_drafts(league["league_id"]) or []
            draft, others = sleeper.pick_draft(drafts)
            settings = (draft or {}).get("settings") or {}
            check("Draft found", bool(draft),
                  "id %s" % (draft or {}).get("draft_id"))
            check("Only one draft on the league", not others,
                  "%d others" % len(others), fatal=False)
            check("12 teams", int(settings.get("teams") or 0) == config.TEAMS,
                  "%s" % settings.get("teams"))
            check("15 rounds", int(settings.get("rounds") or 0) == config.ROUNDS,
                  "%s" % settings.get("rounds"))
            check("Snake draft", (draft or {}).get("type") == "snake",
                  "%s" % (draft or {}).get("type"))
            users = {u["user_id"]: (u.get("display_name") or u.get("username"))
                     for u in (sleeper.get_league_users(league["league_id"]) or [])}
            check("All 12 managers visible", len(users) == config.TEAMS,
                  "%d found" % len(users))
    except sleeper.SleeperError as exc:
        check("Sleeper reachable", False, str(exc)[:44])

    # --------------------------------------------------------------- scoring
    print("\n  SCORING")
    live = sleeper.live_scoring_settings(league) if league else {}
    if live:
        check("Using your league's live settings", True,
              "%d values" % len(live))
        check("Full PPR", abs(live.get("rec", 0) - 1.0) < 1e-6,
              "rec = %s" % live.get("rec"))
        check("Passing TDs worth 4", abs(live.get("pass_td", 0) - 4.0) < 1e-6,
              "pass_td = %s" % live.get("pass_td"))
    else:
        check("Live scoring settings", False,
              "falling back to config.py", fatal=False)

    # ----------------------------------------------------------------- board
    print("\n  THE BOARD")
    merged = None
    if live:
        merged = dict(config.SCORING)
        merged.update(live)
    board = valuation.build_board(players, projections, adp, merged,
                                  current_round=1)
    check("Board builds", len(board) > 200, "%d players ranked" % len(board))

    top = board[0] if board else None
    check("Top of the board is a skill player",
          bool(top) and top["position"] in ("RB", "WR"),
          "%s (%s)" % (top["name"], top["position"]) if top else "-")

    state = {"taken": set(), "round": 1, "current_pick": 1, "my_roster": [],
             "my_players": [], "my_remaining_picks": [],
             "needs": valuation.roster_needs([]), "picks_left": config.ROUNDS,
             "opponent_needs": {}, "my_next_pick": None}
    queue = valuation.build_queue(board, state)
    check("Queue export full", len(queue) >= config.QUEUE_LENGTH,
          "%d players" % len(queue))
    early = [p["position"] for p in queue[:20]]
    check("No kicker or defense early in the queue",
          "K" not in early and "DEF" not in early,
          ", ".join(dict.fromkeys(early[:6])))

    # ------------------------------------------------------------------ slot
    print("\n  DRAFT ORDER")
    slot = None
    if draft:
        slot = draftstate.find_my_slot(draft, (draft.get("league_id") and
                                               sleeper.cache_read(
                                                   config.LEAGUE_CACHE, {})
                                               .get("user", {}).get("user_id")))
        status = draft.get("status")
        if status == "pre_draft":
            print("      Draft has not started. Your slot appears automatically")
            print("      within a second of it opening - nothing for you to do.")
        elif slot:
            picks = draftstate.my_picks(slot)
            print("      YOUR SLOT: %d" % slot)
            print("      Your picks: %s" % ", ".join(str(p) for p in picks))
        print("      status: %s" % status)

    if users:
        print("\n      Managers Sleeper reports for this league:")
        print("      " + ", ".join(sorted(users.values())))
        print("      These fill the draft board automatically at kickoff.")

    return finish(queue)


def finish(queue=None):
    print("\n%s" % RULE)
    if PROBLEMS:
        print("  NOT READY")
        for item in PROBLEMS:
            print("   [X] %s" % item)
    else:
        print("  GO. Everything checks out.")
    if NOTES:
        print("\n  Worth knowing:")
        for item in NOTES:
            print("   [!] %s" % item)

    if queue and not PROBLEMS:
        print("\n  PASTE THIS INTO SLEEPER'S DRAFT QUEUE NOW:\n")
        for i, player in enumerate(queue[:15], start=1):
            print("   %2d. %-26s %s" % (i, player["name"], player["position"]))
        print("   ... %d more in the app's Queue Export box" % (len(queue) - 15))

    print("\n%s" % RULE)
    print("  Then leave it running:   ./start.command")
    print("%s\n" % RULE)
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
