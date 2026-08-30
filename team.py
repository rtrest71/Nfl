"""Your live roster, scored under your league's rules.

Shared by lineup.py and trade.py. Everything in-season starts the same way:
pull the roster Sleeper actually holds for you, score each player, and work out
the best legal lineup those players can field.
"""

import config
import projections as paste
import scoring
import simulation
import sleeper

SLOT_ORDER = ["QB", "RB", "WR", "TE", "FLEX", "K", "DEF"]


class TeamError(Exception):
    pass


def load_context(week=None, quiet=False):
    """Resolve league, roster and projections. Returns a context dict.

    Falls back to cached data wherever Sleeper is unreachable, so a bad
    connection on a Sunday morning does not leave you without a lineup.
    """
    players = sleeper.cache_read(config.PLAYERS_CACHE, {}) or {}
    if not players:
        raise TeamError("No player database. Run: python3 build_data.py")

    league_blob = sleeper.cache_read(config.LEAGUE_CACHE, {}) or {}
    user = league_blob.get("user")
    league = league_blob.get("league")

    # Refresh league and user from the API when we can; cache otherwise.
    try:
        user = sleeper.get_user()
        leagues = sleeper.get_leagues(user["user_id"])
        live_league = sleeper.pick_league(leagues)
        if live_league:
            league = live_league
    except sleeper.SleeperError as exc:
        if not league:
            raise TeamError("Cannot reach Sleeper and no cached league: %s" % exc)
        if not quiet:
            print("  (offline - using cached league)")

    if not user or not league:
        raise TeamError("Could not resolve your league. Run: python3 draftinfo.py")

    # Which week are we in?
    if week is None:
        try:
            week = int((sleeper.get_state() or {}).get("week") or 1)
        except (sleeper.SleeperError, TypeError, ValueError):
            week = 1
    week = max(1, int(week))

    scoring_settings = None
    live = sleeper.live_scoring_settings(league)
    if live:
        scoring_settings = dict(config.SCORING)
        scoring_settings.update(live)

    # Projections: this week's if Sleeper has them, otherwise the season-long
    # numbers spread across the remaining games. Which one we used is reported,
    # because a start/sit call made on a season average is a weaker call.
    projections, source = _weekly_projections(players, week, quiet=quiet)

    roster = None
    try:
        rosters = sleeper.get_rosters(league["league_id"])
        roster = sleeper.my_roster(rosters, user["user_id"])
    except sleeper.SleeperError as exc:
        raise TeamError("Could not read your roster from Sleeper: %s" % exc)
    if not roster:
        raise TeamError("Sleeper has no roster for %s in this league."
                        % config.USERNAME)

    owned = []
    for pid in (roster.get("players") or []):
        player = players.get(str(pid))
        if not player:
            continue
        owned.append(_score(player, projections, scoring_settings))
    owned.sort(key=lambda p: p["points"], reverse=True)

    return {
        "week": week,
        "league": league,
        "user": user,
        "players": players,
        "projections": projections,
        "projection_source": source,
        "scoring": scoring_settings,
        "roster": roster,
        "owned": owned,
        "index": paste.PlayerIndex(players),
        "starters_ids": [str(p) for p in (roster.get("starters") or [])],
    }


def _weekly_projections(players, week, quiet=False):
    """This week's projections if available; season-long spread if not."""
    try:
        rows, _ = sleeper.probe_weekly_projections(config.SEASON, week)
        if rows:
            out = {}
            for row in rows:
                if row["player_id"] in players:
                    out[row["player_id"]] = {"stats": row["stats"]}
            if len(out) > 50:
                return out, "week %d projections" % week
    except sleeper.SleeperError:
        pass

    blob = sleeper.cache_read(config.PROJECTIONS_CACHE, {}) or {}
    season = blob.get("players", blob) or {}
    if not season:
        raise TeamError("No projections at all. Run: python3 build_data.py")

    # Spread season totals over a single game. Counting stats divide; rates do
    # not. Dividing points-allowed by seventeen drops every defense into the
    # "1-6 allowed" bucket and then scores it as a full season - which is how a
    # defense ends up projected for 127 points in a week.
    out = {}
    for pid, record in season.items():
        stats = {}
        for key, value in (record.get("stats") or {}).items():
            if not isinstance(value, (int, float)):
                continue
            if key in ("gp", "games"):
                continue
            if key.startswith("pts_allow"):
                # Already per game unless it is clearly a season total.
                stats[key] = (value / config.FULL_SEASON_GAMES
                              if value > 60 else value)
            else:
                stats[key] = value / config.FULL_SEASON_GAMES
        stats["gp"] = 1.0     # one game, so the defense scorer does not scale up
        out[pid] = {"stats": stats}
    if not quiet:
        print("  (no weekly projections from Sleeper - using season averages,"
              " which are weaker for a single week)")
    return out, "season average per game"


def _score(player, projections, scoring_settings):
    record = projections.get(player["player_id"]) or {}
    points = scoring.fantasy_points(
        record.get("stats") or {}, player.get("position"), scoring_settings)
    return {
        "player_id": player["player_id"],
        "name": player.get("name"),
        "position": (player.get("position") or "").upper(),
        "team": player.get("team"),
        "points": round(points, 2),
        "injury_status": player.get("injury_status"),
        "depth_chart_order": player.get("depth_chart_order"),
        "years_exp": player.get("years_exp"),
    }


def playable(player):
    """Can this player realistically be started? Returns (bool, reason)."""
    status = player.get("injury_status")
    if status in ("IR", "PUP", "NA", "Out", "Suspended", "Sus", "DNR"):
        return False, status
    if player.get("points", 0) <= 0:
        return False, "no projection"
    return True, ""


def best_lineup(owned, allow_risky=True):
    """The best legal lineup these players can field.

    Players who cannot play are excluded outright - an optimiser that starts a
    player on injured reserve is worse than useless.
    """
    pool = []
    benched_by_injury = []
    for player in owned:
        ok, reason = playable(player)
        if ok or allow_risky is False:
            if ok:
                pool.append(player)
            else:
                benched_by_injury.append((player, reason))
        else:
            benched_by_injury.append((player, reason))

    total, chosen, unfilled = simulation.optimal_lineup(pool)
    chosen_ids = {p["player_id"] for p in chosen}
    bench = [p for p in owned if p["player_id"] not in chosen_ids]
    bench.sort(key=lambda p: p["points"], reverse=True)

    return {
        "total": total,
        "starters": _label_slots(chosen),
        "bench": bench,
        "unfilled": unfilled,
        "unavailable": benched_by_injury,
    }


def _label_slots(chosen):
    """Assign each chosen player the roster slot he is filling."""
    remaining = list(chosen)
    labelled = []
    for position, count in config.STARTERS.items():
        at_position = [p for p in remaining if p["position"] == position]
        at_position.sort(key=lambda p: p["points"], reverse=True)
        for player in at_position[:count]:
            labelled.append(dict(player, slot=position))
            remaining.remove(player)
    for player in sorted(remaining, key=lambda p: p["points"], reverse=True):
        labelled.append(dict(player, slot="FLEX"))

    order = {slot: i for i, slot in enumerate(SLOT_ORDER)}
    labelled.sort(key=lambda p: (order.get(p["slot"], 99), -p["points"]))
    return labelled


def pending_trades(context):
    """Trade offers sitting in your Sleeper inbox, already evaluated.

    Sleeper reports a proposed trade as a transaction with status "pending",
    so an offer can be read and scored before you have looked at it. `adds`
    maps a player onto the roster receiving him, `drops` onto the roster
    losing him - so both sides of my half of the deal are right there.
    """
    league = context["league"]
    roster_id = (context["roster"] or {}).get("roster_id")
    if not roster_id:
        return []

    week = context["week"]
    seen, offers = set(), []
    # A trade proposed late in one week can still be open in the next, so look
    # at this week and the one before it.
    for probe in {max(1, week - 1), week}:
        try:
            transactions = sleeper.get_transactions(league["league_id"], probe) or []
        except sleeper.SleeperError:
            continue
        for txn in transactions:
            if txn.get("type") != "trade":
                continue
            if str(txn.get("status") or "").lower() != "pending":
                continue
            txn_id = str(txn.get("transaction_id") or "")
            if txn_id in seen:
                continue
            if roster_id not in (txn.get("roster_ids") or []):
                continue
            seen.add(txn_id)

            adds = txn.get("adds") or {}
            drops = txn.get("drops") or {}
            incoming = [str(p) for p, rid in adds.items() if rid == roster_id]
            outgoing = [str(p) for p, rid in drops.items() if rid == roster_id]
            if not incoming and not outgoing:
                continue

            offers.append(evaluate_offer(context, outgoing, incoming, txn))
    offers.sort(key=lambda o: o.get("delta", 0), reverse=True)
    return offers


def evaluate_offer(context, give_ids, get_ids, txn=None):
    """Score one trade by what it does to the starting lineup."""
    owned = {p["player_id"]: p for p in context["owned"]}
    giving = [owned[p] for p in give_ids if p in owned]
    getting = []
    for pid in get_ids:
        if pid in owned:
            continue
        player = context["players"].get(pid)
        if player:
            getting.append(_score(player, context["projections"],
                                  context["scoring"]))

    before = best_lineup(context["owned"])
    give_set = set(give_ids)
    after_roster = [p for p in context["owned"]
                    if p["player_id"] not in give_set] + getting
    after = best_lineup(after_roster)
    delta = round(after["total"] - before["total"], 1)

    if delta >= 8:
        verdict, tone = "ACCEPT", "good"
    elif delta >= 3:
        verdict, tone = "LEAN ACCEPT", "good"
    elif delta > -3:
        verdict, tone = "TOO CLOSE TO CALL", "even"
    elif delta > -8:
        verdict, tone = "LEAN REJECT", "bad"
    else:
        verdict, tone = "REJECT", "bad"

    notes = []
    if len(after_roster) > config.ROSTER_SIZE:
        notes.append("You would hold %d players for %d spots - you must drop %d."
                     % (len(after_roster), config.ROSTER_SIZE,
                        len(after_roster) - config.ROSTER_SIZE))
    if after["unfilled"] > before["unfilled"]:
        notes.append("This leaves %d starting slot(s) you cannot fill."
                     % after["unfilled"])

    return {
        "transaction_id": str((txn or {}).get("transaction_id") or ""),
        "created": (txn or {}).get("created"),
        "verdict": verdict, "tone": tone, "delta": delta,
        "before": before["total"], "after": after["total"],
        "giving": giving, "getting": getting,
        "starters_after": after["starters"],
        "notes": notes,
    }


def resolve_names(names, context):
    """Turn typed names into scored player records. Returns (found, missing)."""
    found, missing = [], []
    by_id = {p["player_id"]: p for p in context["owned"]}
    for name in names:
        pid = context["index"].match(name)
        if not pid:
            missing.append(name)
            continue
        if pid in by_id:
            found.append(by_id[pid])
        else:
            player = context["players"].get(pid)
            if not player:
                missing.append(name)
                continue
            found.append(_score(player, context["projections"], context["scoring"]))
    return found, missing
