#!/usr/bin/env python3
"""Live draft inspector. Asks Sleeper what is actually true, right now.

    python3 draftinfo.py

Run it whenever you want the truth rather than a cached guess:

  * before the draft, to confirm the round count and see every draft object
    the league carries (leagues often keep leftover practice drafts);
  * the moment the draft starts, to read the randomised order and your slot.

It prints the exact command to start the app with the right settings.
"""

import sys

import config
import draftstate
import sleeper


def main():
    print("\nLive draft check - asking Sleeper directly\n" + "-" * 60)

    try:
        user = sleeper.get_user()
    except sleeper.SleeperError as exc:
        print("  Could not reach Sleeper: %s" % exc)
        print("  Check your internet. The app still runs from cache:")
        print("     python3 app.py --offline --slot <your slot>")
        return 1

    print("  you           %s  (user_id %s)" % (config.USERNAME, user["user_id"]))

    try:
        leagues = sleeper.get_leagues(user["user_id"])
    except sleeper.SleeperError as exc:
        print("  Could not list leagues: %s" % exc)
        return 1

    league = sleeper.pick_league(leagues)
    if not league:
        print("  No league named %r. Leagues on this account:" % config.LEAGUE_NAME)
        for other in leagues or []:
            print("     - %s" % other.get("name"))
        return 1
    print("  league        %s  (%s teams, league_id %s)"
          % (league.get("name"), league.get("total_rosters"), league["league_id"]))

    roster_positions = league.get("roster_positions") or []
    if roster_positions:
        print("  roster slots  %s" % " ".join(roster_positions))
        print("  -> %d players per team" % len(roster_positions))

    try:
        drafts = sleeper.get_drafts(league["league_id"]) or []
    except sleeper.SleeperError as exc:
        print("  Could not list drafts: %s" % exc)
        return 1

    print("\n  DRAFTS ON THIS LEAGUE (%d):" % len(drafts))
    for draft in drafts:
        print("     %s" % sleeper.describe_draft(draft))

    draft, others = sleeper.pick_draft(drafts)
    if not draft:
        print("  No draft object yet.")
        return 1
    if others:
        print("\n  Using: %s" % sleeper.describe_draft(draft))
        print("  (ignoring %d other draft(s) above)" % len(others))

    settings = draft.get("settings") or {}
    rounds = int(settings.get("rounds") or 0)
    teams = int(settings.get("teams") or 0)

    print("\n  THE DRAFT THAT MATTERS")
    print("     draft_id    %s" % draft.get("draft_id"))
    print("     status      %s" % draft.get("status"))
    print("     type        %s" % draft.get("type"))
    print("     teams       %s" % teams)
    print("     rounds      %s" % rounds)
    print("     seconds     %s per pick" % settings.get("pick_timer"))

    problems = []
    if rounds and rounds != config.ROUNDS:
        problems.append(
            "Round count is %d, but config.ROUNDS is %d. If %d is correct, "
            "start the app with:  python3 app.py --rounds %d"
            % (rounds, config.ROUNDS, rounds, rounds))
    if rounds and roster_positions and rounds < len(roster_positions):
        problems.append(
            "Only %d rounds for a %d-player roster - the draft cannot fill "
            "your lineup. Check this with your commissioner."
            % (rounds, len(roster_positions)))
    if teams and teams != config.TEAMS:
        problems.append("Team count is %d, not %d. VOR baselines assume %d."
                        % (teams, config.TEAMS, config.TEAMS))

    # Managers, so you can see who is who even with team names hidden.
    try:
        users = sleeper.get_league_users(league["league_id"]) or []
    except sleeper.SleeperError:
        users = []
    names = {u["user_id"]: (u.get("display_name") or u.get("username"))
             for u in users}

    order = draft.get("draft_order") or {}
    print("\n  DRAFT ORDER")
    if not order:
        print("     Not published yet. This is NORMAL before the draft starts -")
        print("     Sleeper randomises it at kickoff. The app polls for it and")
        print("     will show your slot within seconds of the draft opening.")
        print("     Run this again right after it starts if you want to confirm.")
    else:
        by_slot = {}
        for user_id, slot in order.items():
            by_slot[int(slot)] = names.get(str(user_id), "user %s" % user_id)
        for slot in sorted(by_slot):
            mine = "  <-- YOU" if by_slot[slot] == config.USERNAME else ""
            print("     %2d. %s%s" % (slot, by_slot[slot], mine))

    slot = draftstate.find_my_slot(draft, user["user_id"])
    print("\n  YOUR SLOT     %s" % (slot if slot else "not assigned yet"))
    if slot:
        picks = draftstate.my_picks(
            slot, teams or config.TEAMS, rounds or config.ROUNDS,
            draft.get("type") or "snake", int(settings.get("reversal_round") or 0))
        print("  YOUR PICKS    %s" % ", ".join(str(p) for p in picks))

    if problems:
        print("\n  !! ATTENTION")
        for item in problems:
            print("     * %s" % item)

    # Mock drafts are separate draft objects with no league attached, so they
    # never appear under the league's drafts. Find them so the app can follow
    # one for practice.
    try:
        all_drafts = sleeper.get_user_drafts(user["user_id"]) or []
    except sleeper.SleeperError:
        all_drafts = []

    league_ids = {str(l.get("league_id")) for l in (leagues or [])}
    mocks = [d for d in all_drafts if sleeper.draft_is_mock(d, league_ids)]
    if mocks:
        print("\n  MOCK / PRACTICE DRAFTS ON YOUR ACCOUNT (%d)" % len(mocks))
        for mock in mocks[:8]:
            print("     %s" % sleeper.describe_draft(mock))
        newest = mocks[0]
        print("\n  To practise against one, start the app with:")
        print("     ./start.command --draft-id %s" % newest.get("draft_id"))
        print("  Then restart WITHOUT --draft-id before the real draft.")
    else:
        print("\n  No mock drafts found on your account.")
        print("  Start one in Sleeper (Mock Draft lobby), then run this again")
        print("  to get the id. Or take it from the draft's web address:")
        print("     sleeper.com/draft/nfl/<the long number is the draft id>")

    print("\n  START THE APP WITH:")
    command = "     python3 app.py"
    if rounds and rounds != config.ROUNDS:
        command += " --rounds %d" % rounds
    if others:
        command += " --draft-id %s" % draft.get("draft_id")
    print(command)
    if not slot:
        print("     (add --slot N only if your slot never appears once drafting)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
