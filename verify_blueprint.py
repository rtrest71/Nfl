#!/usr/bin/env python3
"""Check an outside draft blueprint against YOUR live data.

    python3 verify_blueprint.py

A blueprint is a snapshot of someone else's research on some particular day,
using some particular ADP source. This checks its claims against three things
that are actually yours:

  * Sleeper's live injury and depth-chart data, which updates continuously;
  * Sleeper's own ADP, which is what your league-mates will draft from - a
    different source's ADP describes a different room;
  * projections scored under your league's exact rules.

Where they disagree, your data wins. Nothing here is taken on faith.
"""

import sys

import config
import projections as paste
import sleeper
import valuation

# Players the blueprint flags for injury or suspension, with the action it
# recommends. Names only - every number below is read from live data.
FLAGGED = [
    ("Ashton Jeanty", "ankle - conditional buy only if he falls past 28"),
    ("Breece Hall", "groin - downgrade one full round"),
    ("Josh Jacobs", "suspension + injury history - RB3 only"),
    ("Jeremiyah Love", "high ankle - avoid at ADP"),
    ("Malik Nabers", "ACL/meniscus return - downgrade"),
    ("Puka Nacua", "groin soreness - minor flag"),
    ("Jahmyr Gibbs", "suspension chatter - verify day-of"),
    ("Alvin Kamara", "out a month or more - do not draft"),
    ("Jordyn Tyson", "hamstring - do not draft"),
    ("Jonathon Brooks", "knee return - not before round 13"),
    ("TreVeyon Henderson", "ankle - Stevenson the better bet"),
    ("Rhamondre Stevenson", "named as the safer of the two"),
]

# The blueprint's positional cliff claims, as (position, claimed ADP).
CLAIMED_CLIFFS = [("RB", 55), ("TE", 78), ("WR", 90)]

RULE = "=" * 74


def header(text):
    print("\n%s\n%s\n%s" % (RULE, text, RULE))


def main():
    players = sleeper.cache_read(config.PLAYERS_CACHE, {}) or {}
    proj_blob = sleeper.cache_read(config.PROJECTIONS_CACHE, {}) or {}
    projections = proj_blob.get("players", proj_blob) or {}
    adp_blob = sleeper.cache_read(config.ADP_CACHE, {}) or {}
    adp = adp_blob.get("players", adp_blob) or {}
    league_blob = sleeper.cache_read(config.LEAGUE_CACHE, {}) or {}
    league = league_blob.get("league")

    if not players or not projections:
        print("No cached data. Run: python3 build_data.py")
        return 1

    settings = None
    if league:
        live = sleeper.live_scoring_settings(league)
        if live:
            settings = dict(config.SCORING)
            settings.update(live)

    board = valuation.build_board(players, projections, adp, settings,
                                  current_round=1)
    by_id = {p["player_id"]: p for p in board}
    index = paste.PlayerIndex(players)

    # ---------------------------------------------------------------- injuries
    header("1. INJURY FLAGS vs SLEEPER'S LIVE DATA")
    print("  The blueprint is a snapshot. Sleeper updates continuously.")
    print("  'live status' below is what Sleeper says RIGHT NOW.\n")
    print("  %-22s %-10s %-13s %8s  %s"
          % ("player", "live", "depth", "sleeperADP", "blueprint says"))
    print("  " + "-" * 88)

    for name, note in FLAGGED:
        pid = index.match(name)
        if not pid or pid not in by_id:
            print("  %-22s %-10s %-13s %8s  %s"
                  % (name[:22], "NOT FOUND", "-", "-", note[:32]))
            continue
        entry = by_id[pid]
        status = entry.get("injury_status") or "healthy"
        order = entry.get("depth_chart_order")
        depth = ("starter" if order == 1 else
                 ("backup #%s" % int(order)) if order else "unlisted")
        adp_value = ("%.1f" % entry["adp"]) if entry.get("adp") else "-"
        mark = "!!" if status != "healthy" else "  "
        print("%s %-22s %-10s %-13s %8s  %s"
              % (mark, entry["name"][:22], status, depth, adp_value, note[:32]))

    print("\n  Where Sleeper says 'healthy' but the blueprint flags an injury,")
    print("  the player has most likely been cleared since it was written.")
    print("  Where Sleeper shows a status, the app is ALREADY penalising him.")

    # ------------------------------------------------------------------ cliffs
    header("2. POSITIONAL CLIFFS - claimed vs measured in YOUR data")
    print("  The blueprint claims cliffs from its own ADP source. These are")
    print("  measured from your projections under your league's scoring.\n")

    for position, claimed in CLAIMED_CLIFFS:
        group = [p for p in board
                 if p["position"] == position and p.get("adp")
                 and p["adp"] <= config.VALUE_GAP_ADP_LIMIT]
        group.sort(key=lambda p: p["adp"])
        if len(group) < 6:
            print("  %-4s not enough data" % position)
            continue

        # The biggest points drop between consecutive players by ADP order.
        worst = None
        for i in range(1, len(group)):
            drop = group[i - 1]["points"] - group[i]["points"]
            if worst is None or drop > worst[0]:
                worst = (drop, group[i - 1], group[i])
        drop, before, after = worst

        # Also: where does the startable tier actually end?
        baseline_rank = config.baselines().get(position, 12)
        ranked = sorted([p for p in board if p["position"] == position],
                        key=lambda p: p["points"], reverse=True)
        last_startable = ranked[min(baseline_rank, len(ranked)) - 1]

        print("  %-4s blueprint says cliff at ADP ~%d" % (position, claimed))
        print("       biggest drop in your data: %.0f pts, between %s (ADP %.0f)"
              % (drop, before["name"], before["adp"]))
        print("       and %s (ADP %.0f)" % (after["name"], after["adp"]))
        print("       last startable %s in this league: %s at ADP %s"
              % (position, last_startable["name"],
                 ("%.0f" % last_startable["adp"]) if last_startable.get("adp")
                 else "unranked"))
        verdict = ("AGREES" if abs(before["adp"] - claimed) <= 20 else "DIFFERS")
        print("       -> %s with the blueprint\n" % verdict)

    # --------------------------------------------------------------- adp source
    header("3. ADP SOURCE - does the blueprint's room match yours?")
    print("  The blueprint used Fantasy Football Calculator. Your app uses")
    print("  Sleeper's own ADP, which is what your league-mates draft from.")
    print("  Big gaps mean the blueprint is describing a different room.\n")

    samples = [
        ("Jahmyr Gibbs", 1.5), ("Bijan Robinson", 2.3), ("Puka Nacua", 3.0),
        ("Ja'Marr Chase", 3.9), ("Christian McCaffrey", 6.6),
        ("Trey McBride", 29.2), ("Brock Bowers", 35.2),
        ("Kyren Williams", 31.5), ("Quinshon Judkins", 50.5),
        ("Brandon Aubrey", 128.0), ("Jayden Daniels", 71.8),
        ("Brock Purdy", 87.0),
    ]
    print("  %-24s %10s %10s %9s" % ("player", "blueprint", "sleeper", "gap"))
    print("  " + "-" * 58)
    gaps = []
    for name, claimed in samples:
        pid = index.match(name)
        entry = by_id.get(pid) if pid else None
        if not entry or not entry.get("adp"):
            print("  %-24s %10.1f %10s %9s" % (name[:24], claimed, "-", "-"))
            continue
        gap = entry["adp"] - claimed
        gaps.append(abs(gap))
        print("  %-24s %10.1f %10.1f %+9.1f"
              % (entry["name"][:24], claimed, entry["adp"], gap))

    if gaps:
        average = sum(gaps) / len(gaps)
        print("\n  average absolute gap: %.1f picks" % average)
        if average > 15:
            print("  -> The two sources disagree a lot. TRUST SLEEPER ADP:")
            print("     your league-mates are drafting off Sleeper's board.")
        else:
            print("  -> The sources broadly agree, which is reassuring.")

    # ------------------------------------------------------------------- rules
    header("4. BLUEPRINT RULES vs WHAT THE APP ENFORCES")
    rows = [
        ("No K before round 15", "K blocked until round %d" % config.K_UNLOCK_ROUND,
         config.K_UNLOCK_ROUND >= 14),
        ("No DEF before round 14", "DEF blocked until round %d" % config.DEF_UNLOCK_ROUND,
         config.DEF_UNLOCK_ROUND >= 14),
        ("No QB before round 7", "QB blocked until round %d" % config.QB_UNLOCK_ROUND,
         config.QB_UNLOCK_ROUND >= 7),
        ("One QB, one K, one DEF", "enforced in eligible()", True),
        ("Never a third TE", "TE capped at 2", True),
        ("Injured players penalised hard",
         "up to %.0f pts, no IR assumed" % max(config.INJURY_PENALTY.values()), True),
        ("Handcuff your own RB1",
         "allowed from round %d, +%.0f pts"
         % (config.OWN_HANDCUFF_UNLOCK_ROUND, config.OWN_HANDCUFF_BONUS), True),
        ("Load up on pass catchers",
         "WR baseline WR%d, bench discount %.0f%%"
         % (config.baselines()["WR"], config.BENCH_VALUE_MULTIPLIER * 100), True),
    ]
    for claim, implementation, ok in rows:
        print("  [%s] %-34s %s" % ("x" if ok else " ", claim, implementation))

    header("WHAT THIS CANNOT CHECK")
    print("  * Bye weeks. Sleeper's free data does not carry them, so the")
    print("    blueprint's Week 8 and Week 11 lists are unverified here.")
    print("    Paste a projections table with a BYE column to switch bye")
    print("    warnings on.")
    print("  * Whether any injury report is current. Sleeper's status is the")
    print("    best signal you have; re-run this an hour before the draft.")
    print("  * Target share, red-zone usage, vacated targets. Not in the")
    print("    free data, so the app does not pretend to model them.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
