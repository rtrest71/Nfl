#!/usr/bin/env python3
"""Pre-draft sanity check. Run this after build_data.py, before the draft.

    python3 preflight.py

Reads only the local cache - no network needed. It prints what the app
actually believes about your league so a human can eyeball it, and ends with a
pass/warn verdict. The point is to catch a wrong ADP source or a broken
scoring table now, not at pick 4.
"""

import sys
import time

import config
import draftstate
import scoring
import sleeper
import valuation

RULE = "=" * 72
WARNINGS = []
FAILURES = []


def header(text):
    print("\n%s\n%s\n%s" % (RULE, text, RULE))


def warn(text):
    WARNINGS.append(text)


def fail(text):
    FAILURES.append(text)


def load():
    players = sleeper.cache_read(config.PLAYERS_CACHE, {}) or {}
    proj_blob = sleeper.cache_read(config.PROJECTIONS_CACHE, {}) or {}
    adp_blob = sleeper.cache_read(config.ADP_CACHE, {}) or {}
    league_blob = sleeper.cache_read(config.LEAGUE_CACHE, {}) or {}
    return (players,
            proj_blob.get("players", proj_blob) or {},
            adp_blob.get("players", adp_blob) or {},
            proj_blob, adp_blob, league_blob)


def section_data(players, projections, adp, proj_blob, adp_blob, league_blob):
    header("1. DATA")
    if not players:
        fail("No player database. Run: python3 build_data.py")
        return
    print("  players       %d" % len(players))
    print("  projections   %d   (source: %s)"
          % (len(projections), proj_blob.get("source", "unknown")))
    print("  adp           %d   (source: %s)"
          % (len(adp), adp_blob.get("source", "unknown")))

    age = sleeper.cache_age_hours(config.PLAYERS_CACHE)
    if age is not None:
        print("  cache age     %.1f hours" % age)

    if not projections:
        fail("No projections. Every player will score zero.")
    if not adp:
        warn("No ADP. Survival odds will fall back to Sleeper's ranking estimate.")
    if proj_blob.get("estimated"):
        warn("Projections are ESTIMATED from prior seasons, not real projections.")

    league = league_blob.get("league")
    draft = league_blob.get("draft")
    if league:
        print("  league        %s (%s teams)"
              % (league.get("name"), league.get("total_rosters")))
        for message in sleeper.verify_league_settings(league):
            warn(message)
    else:
        fail("League not resolved. The app cannot read your live draft.")

    if draft:
        settings = draft.get("settings") or {}
        print("  draft         status=%s type=%s rounds=%s"
              % (draft.get("status"), draft.get("type"), settings.get("rounds")))
        user = league_blob.get("user") or {}
        slot = draftstate.find_my_slot(draft, user.get("user_id"))
        if slot:
            print("  YOUR SLOT     %d" % slot)
            print("  your picks    %s"
                  % ", ".join(str(p) for p in draftstate.my_picks(slot)))
        else:
            print("  your slot     not set yet (normal before the draft starts)")
    else:
        warn("No draft object found for the league yet.")


def section_scoring(players, projections, league_blob):
    header("2. SCORING - is it really YOUR league's table?")
    league = league_blob.get("league")
    settings = None
    if league:
        live = sleeper.live_scoring_settings(league)
        if live:
            settings = dict(config.SCORING)
            settings.update(live)
            print("  using LIVE league settings from Sleeper")
        else:
            print("  using config.py (league carried no scoring settings)")
    else:
        print("  using config.py")

    active = settings or config.SCORING
    checks = [("rec", 1.0, "full PPR"), ("pass_td", 4.0, "4-point passing TDs"),
              ("fum_lost", -2.0, "-2 per fumble lost"),
              ("pass_int", -1.0, "-1 per interception")]
    for key, expected, label in checks:
        value = active.get(key)
        flag = "ok " if value is not None and abs(value - expected) < 1e-6 else "!! "
        print("  [%s] %-22s %s = %s" % (flag.strip(), label, key, value))
        if flag.startswith("!!"):
            fail("Scoring mismatch on %s: expected %s, got %s"
                 % (key, expected, value))

    # Show one quarterback's arithmetic in full, so the 4-point TD is visible.
    qb = None
    best = -1.0
    for pid, record in projections.items():
        player = players.get(pid)
        if not player or player.get("position") != "QB":
            continue
        points = scoring.fantasy_points(record.get("stats") or {}, "QB", settings)
        if points > best:
            best, qb = points, (pid, player, record)
    if qb:
        pid, player, record = qb
        print("\n  worked example - %s" % player["name"])
        parts = scoring.score_breakdown(record.get("stats") or {}, "QB", settings)
        for key in sorted(parts, key=lambda k: -abs(parts[k])):
            if abs(parts[key]) >= 0.5:
                print("     %-12s %8.1f pts" % (key, parts[key]))
        print("     %-12s %8.1f pts  TOTAL" % ("", best))
        six = scoring.fantasy_points(record.get("stats") or {}, "QB",
                                     dict(active, pass_td=6.0))
        print("     (in a 6-point-passing-TD league he would score %.1f - "
              "that %.1f point gap is your edge)" % (six, six - best))


def section_board(players, projections, adp, league_blob):
    header("3. THE BOARD - what the app actually thinks")
    league = league_blob.get("league")
    settings = None
    if league:
        live = sleeper.live_scoring_settings(league)
        if live:
            settings = dict(config.SCORING)
            settings.update(live)

    board = valuation.build_board(players, projections, adp, settings,
                                  current_round=1)
    if not board:
        fail("Could not build a board.")
        return None

    print("  replacement level (a player at this rank is 'free'):")
    baselines = config.baselines()
    for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
        sample = [p for p in board if p["position"] == position]
        base = sample[0]["baseline"] if sample else 0.0
        print("     %-4s %s%-3d  %7.1f pts"
              % (position, position, baselines.get(position, 0), base))

    print("\n  TOP 20 BY VALUE OVER REPLACEMENT:")
    print("     %-3s %-24s %-4s %6s %7s %6s %6s"
          % ("#", "player", "pos", "proj", "vor", "adp", "gap"))
    for rank, player in enumerate(board[:20], start=1):
        print("     %-3d %-24s %-4s %6.0f %7.1f %6s %6s"
              % (rank, player["name"][:24], player["position"], player["points"],
                 player["vor"],
                 ("%.1f" % player["adp"]) if player["adp"] else "-",
                 ("%+d" % player["value_gap"]) if player["value_gap"] else "0"))

    positions = [p["position"] for p in board[:20]]
    qb_count = positions.count("QB")
    if qb_count > 3:
        warn("%d quarterbacks in the top 20 by VOR. In a 1-QB league that is "
             "suspicious - check that pass_td is 4 and not 6." % qb_count)
    if positions.count("K") or positions.count("DEF"):
        warn("A kicker or defense appears in the top 20 by VOR. Something is "
             "wrong with the projections for those positions.")
    return board


def section_scarcity(board):
    header("4. POSITIONAL SCARCITY - why the app waits on QB and kicker")
    print("  The gap between the best and the last STARTABLE player at each")
    print("  position is what actually matters. Big gap = draft early.\n")
    print("     %-4s %-24s %8s   %-24s %8s %9s"
          % ("pos", "best available", "pts", "replacement level", "pts", "GAP"))

    gaps = {}
    baselines = config.baselines()
    for position in ("RB", "WR", "TE", "QB", "DEF", "K"):
        group = sorted([p for p in board if p["position"] == position],
                       key=lambda p: p["points"], reverse=True)
        if not group:
            continue
        rank = baselines.get(position, 12)
        replacement = group[min(rank, len(group)) - 1]
        gap = group[0]["points"] - replacement["points"]
        gaps[position] = gap
        print("     %-4s %-24s %8.1f   %-24s %8.1f %9.1f"
              % (position, group[0]["name"][:24], group[0]["points"],
                 replacement["name"][:24], replacement["points"], gap))

    print()
    if gaps.get("QB") is not None and gaps.get("RB") is not None:
        if gaps["QB"] < gaps["RB"] or gaps["QB"] < gaps.get("WR", 0):
            print("  -> Confirmed with YOUR data: the best QB is worth less over")
            print("     replacement than the best RB or WR. Waiting on QB is right.")
        else:
            warn("Your data says QB has a BIGGER gap than RB/WR. That is unusual "
                 "in a 1-QB league - if it holds, consider relaxing "
                 "config.QB_UNLOCK_ROUND from 8 to about 6.")
    if gaps.get("K") is not None:
        print("  -> Kicker gap is %.1f points across a whole season (about %.1f a"
              % (gaps["K"], gaps["K"] / 17.0))
        print("     week). That is why kickers go in round 14, not round 8.")


def section_adp(board, adp):
    header("5. ADP SANITY - are the survival odds trustworthy?")
    if not adp:
        warn("No real ADP loaded.")
        print("  none loaded - the app will estimate from Sleeper's ranking.")
        return

    with_adp = [p for p in board if p.get("adp")]
    values = sorted(p["adp"] for p in with_adp)
    print("  players with ADP   %d" % len(with_adp))
    print("  range              %.1f to %.1f" % (values[0], values[-1]))
    early = [p for p in with_adp if p["adp"] <= 12]
    print("  first-round ADP    %d players inside pick 12" % len(early))
    for player in sorted(early, key=lambda p: p["adp"])[:12]:
        print("     %5.1f  %-24s %s" % (player["adp"], player["name"][:24],
                                        player["position"]))

    if values[0] > 5:
        warn("The lowest ADP is %.1f. Real ADP should start near 1.0 - this may "
             "not be draft-position data." % values[0])
    inside = sum(1 for v in values if v <= 180)
    if inside < 120:
        warn("Only %d players have an ADP inside the 180 picks of your draft. "
             "Survival odds will be weak beyond the early rounds." % inside)
    else:
        print("\n  %d players priced inside your 180-pick draft - good coverage."
              % inside)

    kdef = [p for p in with_adp if p["position"] in ("K", "DEF")]
    if kdef:
        earliest = min(kdef, key=lambda p: p["adp"])
        print("  earliest kicker/defense off the board: %s at %.1f"
              % (earliest["name"], earliest["adp"]))
        if earliest["adp"] < 100:
            warn("A kicker or defense has an ADP inside pick 100, which is odd.")


def section_dryrun(board, league_blob):
    header("6. DRY RUN - what it would recommend at pick 1")
    draft = league_blob.get("draft") or {}
    settings = draft.get("settings") or {}
    teams = int(settings.get("teams") or config.TEAMS)

    for slot in (1, 6, 12):
        picks = draftstate.my_picks(slot, teams)
        state = {
            "taken": set(), "round": 1, "current_pick": picks[0],
            "my_next_pick": picks[1], "picks_until_next": picks[1] - picks[0],
            "my_roster": [], "my_remaining_picks": picks,
            "needs": valuation.roster_needs([]), "picks_left": len(picks),
            "opponent_needs": {},
        }
        result = valuation.recommend(board, state)
        top = result.get("top")
        if not top:
            fail("No recommendation produced for slot %d." % slot)
            continue
        player = top["player"]
        print("\n  FROM SLOT %-2d (picks %d then %d):" % (slot, picks[0], picks[1]))
        print("     -> %s  %s %s   proj %.0f, vor %.1f, adp %s"
              % (player["name"], player["position"], player.get("team") or "",
                 player["points"], player["vor"],
                 ("%.1f" % player["adp"]) if player["adp"] else "-"))
        print("        %s" % top["reason"][:150])
        alts = ", ".join("%s (%s)" % (a["player"]["name"], a["player"]["position"])
                         for a in result["alternatives"][:3])
        if alts:
            print("        alternatives: %s" % alts)
        if player["position"] in ("QB", "K", "DEF"):
            fail("It recommended a %s with the first pick. That should be "
                 "impossible - do not draft until this is fixed."
                 % player["position"])


def section_queue(board):
    header("7. QUEUE EXPORT - your autopick insurance")
    picks = draftstate.my_picks(6)
    state = {
        "taken": set(), "round": 1, "current_pick": picks[0],
        "my_next_pick": picks[1], "picks_until_next": picks[1] - picks[0],
        "my_roster": [], "my_remaining_picks": picks,
        "needs": valuation.roster_needs([]), "picks_left": len(picks),
        "opponent_needs": {},
    }
    queue = valuation.build_queue(board, state)
    print("  first 15 of %d (from slot 6, as an example):" % len(queue))
    for i, player in enumerate(queue[:15], start=1):
        print("     %2d. %-26s %s" % (i, player["name"][:26], player["position"]))
    positions = [p["position"] for p in queue[:10]]
    if "K" in positions or "DEF" in positions:
        fail("A kicker or defense is in the first 10 of the queue.")


def main():
    print("\nSleeper Draft Assistant - pre-draft check")
    print("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    players, projections, adp, proj_blob, adp_blob, league_blob = load()
    section_data(players, projections, adp, proj_blob, adp_blob, league_blob)
    if not players or not projections:
        _verdict()
        return 1

    section_scoring(players, projections, league_blob)
    board = section_board(players, projections, adp, league_blob)
    if board:
        section_scarcity(board)
        section_adp(board, adp)
        section_dryrun(board, league_blob)
        section_queue(board)
    _verdict()
    return 1 if FAILURES else 0


def _verdict():
    header("VERDICT")
    if FAILURES:
        print("  NOT READY - fix these first:")
        for item in FAILURES:
            print("   [X] %s" % item)
    if WARNINGS:
        print("  worth a look:")
        for item in WARNINGS:
            print("   [!] %s" % item)
    if not FAILURES and not WARNINGS:
        print("  ALL CLEAR. Run a Sleeper mock draft with the app open, then go.")
    elif not FAILURES:
        print("\n  No blockers. Run a Sleeper mock draft with the app open, then go.")
    print("\n  Next:  python3 app.py")


if __name__ == "__main__":
    sys.exit(main())
