#!/usr/bin/env python3
"""One-time data build: fetch and cache players, projections and ADP.

    python3 build_data.py

Run this the night before the draft. It writes cache/players.json,
cache/projections.json, cache/adp.json and cache/league.json so that app.py
boots instantly - and still boots if Sleeper is unreachable at kickoff.

It also VERIFIES the projections endpoint rather than assuming it. Sleeper's
projections shape is the one genuinely uncertain piece of the data layer, so
this script probes several candidate shapes and tells you exactly which one
answered, what it contained, and what to do if none of them did.
"""

import argparse
import sys
import time

import config
import projections as paste
import scoring
import sleeper

RULE = "-" * 68


def header(text):
    print("\n%s\n%s\n%s" % (RULE, text, RULE))


def build_players(force=False):
    header("1. PLAYER DATABASE")
    try:
        players, source = sleeper.fetch_players(force=force)
    except sleeper.SleeperError as exc:
        print("  FAILED: %s" % exc)
        print("  Without this the app cannot run. Check your internet and retry.")
        return None
    counts = {}
    for player in players.values():
        counts[player["position"]] = counts.get(player["position"], 0) + 1
    print("  %d fantasy-relevant players from %s" % (len(players), source))
    print("  " + "  ".join("%s=%d" % (pos, counts.get(pos, 0))
                           for pos in ("QB", "RB", "WR", "TE", "K", "DEF")))
    print("  cached -> %s" % config.PLAYERS_CACHE)
    return players


def build_league():
    header("2. LEAGUE AND DRAFT")
    try:
        user = sleeper.get_user()
    except sleeper.SleeperError as exc:
        print("  FAILED to resolve username %r: %s" % (config.USERNAME, exc))
        print("  Check the spelling in config.py (USERNAME).")
        return None

    print("  user %s -> user_id %s" % (config.USERNAME, user["user_id"]))
    try:
        leagues = sleeper.get_leagues(user["user_id"])
    except sleeper.SleeperError as exc:
        print("  FAILED to list leagues: %s" % exc)
        return None

    print("  %d league(s) for %s:" % (len(leagues or []), config.SEASON))
    for lg in leagues or []:
        print("     - %s (%s teams, id %s)"
              % (lg.get("name"), lg.get("total_rosters"), lg.get("league_id")))

    league = sleeper.pick_league(leagues)
    if not league:
        print("  FAILED: no league named %r." % config.LEAGUE_NAME)
        print("  Set config.LEAGUE_NAME to one of the names listed above.")
        return None

    warnings = sleeper.verify_league_settings(league)
    if warnings:
        print("\n  !! SETTINGS MISMATCH - read these carefully:")
        for warning in warnings:
            print("     * %s" % warning)
    else:
        print("  settings match config.py (12 teams, full PPR, 4-point passing TDs)")

    drafts = []
    draft = None
    try:
        drafts = sleeper.get_drafts(league["league_id"]) or []
        draft = drafts[0] if drafts else None
    except sleeper.SleeperError as exc:
        print("  could not list drafts: %s" % exc)

    if draft:
        settings = draft.get("settings") or {}
        print("  draft %s: status=%s type=%s rounds=%s teams=%s"
              % (draft.get("draft_id"), draft.get("status"), draft.get("type"),
                 settings.get("rounds"), settings.get("teams")))
        if draft.get("draft_order"):
            slot = draft["draft_order"].get(str(user["user_id"]))
            print("  DRAFT ORDER IS SET. Your slot: %s" % slot)
        else:
            print("  draft order not set yet - normal before the draft starts.")
            print("  app.py polls for it and will show your slot the moment it lands.")
    else:
        print("  no draft object yet.")

    users = {}
    try:
        users = {u["user_id"]: (u.get("display_name") or u.get("username"))
                 for u in (sleeper.get_league_users(league["league_id"]) or [])}
        print("  %d managers: %s" % (len(users), ", ".join(sorted(users.values()))))
    except sleeper.SleeperError as exc:
        print("  could not list managers: %s" % exc)

    sleeper.cache_write(config.LEAGUE_CACHE, {
        "user": user, "league": league, "draft": draft, "users": users,
        "fetched_at": time.time(),
    })
    print("  cached -> %s" % config.LEAGUE_CACHE)
    return {"user": user, "league": league, "draft": draft, "users": users}


def build_projections(players, league=None):
    header("3. PROJECTIONS  (the uncertain one - verifying, not assuming)")
    print("  probing candidate endpoint shapes:")
    rows, report = sleeper.probe_projections(config.SEASON)

    scoring_settings = None
    if league:
        live = sleeper.live_scoring_settings(league)
        if live:
            scoring_settings = dict(config.SCORING)
            scoring_settings.update(live)

    if rows:
        by_player = {}
        for row in rows:
            pid = row["player_id"]
            if pid not in players:
                continue
            by_player[pid] = {"stats": row["stats"], "source": "sleeper"}
        print("\n  matched %d of %d rows onto the player database"
              % (len(by_player), len(rows)))

        sample = _sample(by_player, players, scoring_settings)
        if sample:
            print("  sanity check - top projected scorers under YOUR scoring:")
            for line in sample:
                print("     %s" % line)

        adp = sleeper.extract_adp(rows)
        if adp:
            print("  this endpoint also carries ADP: %d players" % len(adp))
            sleeper.cache_write(config.ADP_CACHE, {
                "players": {pid: {"adp": value} for pid, value in adp.items()},
                "byes": {}, "source": "sleeper projections", "updated_at": time.time(),
            })
            print("  cached ADP -> %s" % config.ADP_CACHE)

        sleeper.cache_write(config.PROJECTIONS_CACHE, {
            "players": by_player, "source": "sleeper", "updated_at": time.time(),
            "probe_report": report,
        })
        print("  cached -> %s" % config.PROJECTIONS_CACHE)
        return by_player

    print("\n  NONE of the candidate endpoints returned scoreable projections.")
    print("  This is the case the spec warned about. Two ways forward:")
    print("    a) PASTE a projections table into the app's Data panel (preferred).")
    print("       FantasyPros / ESPN / Yahoo all show free projections in a browser.")
    print("    b) Let this script derive an ESTIMATE from prior-season stats below.")
    return None


def build_estimate(players, league=None):
    """Derive ESTIMATED projections from prior-season actual stats.

    Weighted toward the most recent season and adjusted for games played.
    Everything produced here is flagged so the UI can label it ESTIMATED.
    """
    header("3b. FALLBACK - ESTIMATE FROM PRIOR SEASONS")
    try:
        season = int(config.SEASON)
    except ValueError:
        season = 2026

    weights = {season - 1: 0.65, season - 2: 0.35}
    gathered = {}
    for year, weight in weights.items():
        rows, _ = sleeper.probe_projections(
            str(year), candidates=sleeper.STATS_CANDIDATES, verbose=False)
        if not rows:
            print("  %s: no stats available" % year)
            continue
        print("  %s: %d players" % (year, len(rows)))
        for row in rows:
            pid = row["player_id"]
            if pid not in players:
                continue
            stats = scoring.normalize_stats(row["stats"])
            games = stats.get("gp") or stats.get("games") or 17.0
            if not games:
                continue
            # Per-game rates, projected forward over a 17-game season.
            per_game = {k: v / games for k, v in stats.items()
                        if k in scoring.SCORABLE}
            bucket = gathered.setdefault(pid, {})
            for key, value in per_game.items():
                bucket[key] = bucket.get(key, 0.0) + value * weight * 17.0

    if not gathered:
        print("  No prior-season stats available either.")
        print("  You MUST paste projections in the app before the draft.")
        return None

    out = {pid: {"stats": stats, "source": "prior seasons", "estimated": True}
           for pid, stats in gathered.items()}
    print("  built ESTIMATED projections for %d players" % len(out))
    print("  every one of these is labelled ESTIMATED in the app.")

    scoring_settings = None
    if league:
        live = sleeper.live_scoring_settings(league)
        if live:
            scoring_settings = dict(config.SCORING)
            scoring_settings.update(live)
    for line in _sample(out, players, scoring_settings):
        print("     %s" % line)

    sleeper.cache_write(config.PROJECTIONS_CACHE, {
        "players": out, "source": "prior-season estimate", "estimated": True,
        "updated_at": time.time(),
    })
    print("  cached -> %s" % config.PROJECTIONS_CACHE)
    return out


def build_durability(players):
    """Games played in prior seasons - a real stand-in for injury history.

    Sleeper does not publish an injury history, but it does publish per-season
    stats, and games played is the signal that matters: a projection quietly
    assumes seventeen games, and a player who has managed twelve two years
    running will not deliver it.
    """
    header("5. DURABILITY  (games played in prior seasons)")
    try:
        season = int(config.SEASON)
    except ValueError:
        season = 2026

    seasons = [season - n for n in range(1, config.DURABILITY_SEASONS + 1)]
    gathered = {}
    for year in seasons:
        rows, _ = sleeper.probe_projections(
            str(year), candidates=sleeper.STATS_CANDIDATES, verbose=False)
        if not rows:
            print("  %s: no stats available" % year)
            continue
        counted = 0
        for row in rows:
            pid = row["player_id"]
            if pid not in players:
                continue
            stats = scoring.normalize_stats(row["stats"])
            games = stats.get("gp") or stats.get("games")
            if games is None:
                continue
            gathered.setdefault(pid, {})[str(year)] = float(games)
            counted += 1
        print("  %s: games played for %d players" % (year, counted))

    if not gathered:
        print("  No prior-season stats reachable. Durability stays unknown, and")
        print("  the app will not guess - no player is penalised for it.")
        return None

    out = {}
    for pid, by_year in gathered.items():
        values = list(by_year.values())
        average = sum(values) / len(values)
        missed = max(0.0, config.FULL_SEASON_GAMES - average)
        out[pid] = {
            "seasons": by_year,
            "avg_games": round(average, 1),
            "avg_missed": round(missed, 1),
        }

    print("  built durability for %d players" % len(out))
    fragile = sorted(
        [(v["avg_missed"], players[pid]["name"], players[pid]["position"], v)
         for pid, v in out.items()
         if v["avg_missed"] >= config.DURABILITY_MIN_GAMES_MISSED
         and players[pid]["position"] in ("QB", "RB", "WR", "TE")],
        reverse=True)
    if fragile:
        print("\n  Most games missed per season (these get penalised):")
        for missed, name, position, record in fragile[:12]:
            years = " ".join("%s:%.0f" % (y, g)
                             for y, g in sorted(record["seasons"].items()))
            print("     %-24s %-3s missed %4.1f/season   %s"
                  % (name[:24], position, missed, years))

    sleeper.cache_write(config.DURABILITY_CACHE, {
        "players": out, "seasons": seasons, "updated_at": time.time()})
    print("\n  cached -> %s" % config.DURABILITY_CACHE)
    return out


def _sample(by_player, players, scoring_settings, limit=8):
    scored = []
    for pid, record in by_player.items():
        player = players.get(pid)
        if not player:
            continue
        points = scoring.fantasy_points(
            record.get("stats") or {}, player.get("position"), scoring_settings)
        if points:
            scored.append((points, player))
    scored.sort(reverse=True, key=lambda item: item[0])
    return ["%-24s %-3s %6.1f pts" % (p["name"], p["position"], pts)
            for pts, p in scored[:limit]]


def load_paste_file(path, players, kind):
    header("4. PASTED %s FROM FILE" % kind.upper())
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print("  could not read %s: %s" % (path, exc))
        return None

    index = paste.PlayerIndex(players)
    if kind == "projections":
        parsed, report = paste.apply_projection_paste(text, players, index)
        cache_path, payload = config.PROJECTIONS_CACHE, {
            "players": parsed, "source": "paste file", "updated_at": time.time()}
    else:
        parsed, report = paste.apply_adp_paste(text, players, index)
        byes = {}
        for pid, rec in parsed.items():
            if rec.get("bye"):
                team = (players.get(pid) or {}).get("team")
                if team:
                    byes[team] = rec["bye"]
        cache_path, payload = config.ADP_CACHE, {
            "players": parsed, "byes": byes, "source": "paste file",
            "updated_at": time.time()}

    print("  parsed %d rows, matched %d, unmatched %d"
          % (report["parsed"], report["matched"], report["unmatched_count"]))
    if report["unmatched"]:
        print("  unmatched names (fix these by hand if they matter):")
        for name in report["unmatched"][:15]:
            print("     - %s" % name)
    if parsed:
        sleeper.cache_write(cache_path, payload)
        print("  cached -> %s" % cache_path)
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Build the local data cache.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch the player database even if cached today.")
    parser.add_argument("--projections-file",
                        help="Load projections from a CSV/table file instead of the API.")
    parser.add_argument("--adp-file", help="Load Sleeper ADP from a CSV/table file.")
    parser.add_argument("--skip-league", action="store_true")
    args = parser.parse_args()

    print("\nSleeper Draft Assistant - data build")
    print("league %r, season %s, user %s"
          % (config.LEAGUE_NAME, config.SEASON, config.USERNAME))

    players = build_players(force=args.force)
    if not players:
        return 1

    league_bundle = None if args.skip_league else build_league()
    league = (league_bundle or {}).get("league")

    proj = None
    if args.projections_file:
        proj = load_paste_file(args.projections_file, players, "projections")
    if not proj:
        proj = build_projections(players, league)
    if not proj:
        proj = build_estimate(players, league)

    if args.adp_file:
        load_paste_file(args.adp_file, players, "adp")

    durability = build_durability(players)

    header("SUMMARY")
    have_adp = sleeper.cache_read(config.ADP_CACHE, {}) or {}
    print("  players      %d" % len(players))
    print("  projections  %s" % (len(proj) if proj else "NONE - paste them in the app"))
    print("  adp          %s" % (len(have_adp.get("players", {})) or
                                 "none - app will estimate from Sleeper's ranking"))
    print("  league       %s" % ("resolved" if league else "NOT RESOLVED"))
    print("  durability   %s" % (len(durability) if durability
                                 else "unknown - nobody penalised for it"))
    print("\n  Next:  python3 app.py")
    if not proj:
        print("  Then paste a projections table in the Data panel before drafting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
