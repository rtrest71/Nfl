"""A stable, documented surface for an outside assistant.

Everything an AI assistant needs to answer "who do I start?", "is this trade
good?" and "what happened to my team?" - as plain JSON dictionaries with no
HTML, no colour codes and no draft-day machinery.

Three things use this module:

  * ``mcp_server.py``  - the Model Context Protocol server, for an assistant
    built on Claude, which can then call these as tools.
  * ``app.py``         - the ``/api/v1/*`` endpoints, for anything that speaks
    plain HTTP instead.
  * ``weekly_nudge.py`` - the Sunday-morning push.

It deliberately does not depend on the web app running. Every call resolves
the league and roster itself, so an assistant can ask a question whether or
not the browser page is open.

Every number here is computed from raw projected stats under this league's
exact scoring - full PPR, four-point passing touchdowns - and never taken
from a generic "PPR points" column.
"""

import time

import config
import team

# One shared context, reused briefly. Each build makes several Sleeper calls
# and nothing about a lineup changes second to second, so an assistant asking
# three questions in a row should not cost three round trips.
_CACHE = {"context": None, "at": 0.0, "week": None}
CONTEXT_TTL = 120.0


class ApiError(Exception):
    """Something the caller can act on: no data, or Sleeper unreachable."""


def context(week=None, fresh=False):
    """The scored roster, cached for a couple of minutes."""
    now = time.time()
    if (not fresh and _CACHE["context"] is not None
            and _CACHE["week"] == week and now - _CACHE["at"] < CONTEXT_TTL):
        return _CACHE["context"]
    try:
        ctx = team.load_context(week=week, quiet=True)
    except team.TeamError as exc:
        raise ApiError(str(exc))
    _CACHE.update({"context": ctx, "at": now, "week": week})
    return ctx


def invalidate():
    """Forget the cached roster. Call after anything that changes it."""
    _CACHE.update({"context": None, "at": 0.0, "week": None})


def _player(player, slot=None):
    """One player, in the shape every response uses."""
    out = {
        "player_id": player.get("player_id"),
        "name": player.get("name"),
        "position": player.get("position"),
        "team": player.get("team"),
        "projected_points": player.get("points"),
        "injury_status": player.get("injury_status"),
    }
    if slot is not None:
        out["slot"] = slot
    return out


def league_rules():
    """The rules that change the advice.

    Included in every response on purpose. An assistant that does not know
    this is a one-flex, four-point-passing-touchdown league will confidently
    give advice written for a different league.
    """
    return {
        "teams": config.TEAMS,
        "scoring": "full PPR (1 point per reception)",
        "passing_touchdown_points": config.SCORING.get("pass_td"),
        "starters": dict(config.STARTERS),
        "flex_slots": config.FLEX_SLOTS,
        "flex_eligible": list(config.FLEX_ELIGIBLE),
        "bench_spots": config.BENCH,
        "roster_size": config.ROSTER_SIZE,
        "injured_reserve_slots": 0,
        "note": ("No IR slot, so an injured player occupies a real roster "
                 "spot. Passing touchdowns are worth %s, not 6."
                 % config.SCORING.get("pass_td")),
    }


def get_lineup(week=None, fresh=False):
    """The best legal lineup this roster can field, and what to change.

    ``changes`` is the answer to "what do I do right now": each entry is a
    player to START or BENCH in the Sleeper app. An empty list means the
    lineup already set in Sleeper is the best one available.
    """
    ctx = context(week=week, fresh=fresh)
    best = team.best_lineup(ctx["owned"])
    current = set(ctx["starters_ids"])
    recommended = {p["player_id"] for p in best["starters"]}
    by_id = {p["player_id"]: p for p in ctx["owned"]}

    changes = []
    for pid in sorted(recommended - current):
        if pid in by_id:
            changes.append(dict(_player(by_id[pid]), action="START"))
    for pid in sorted(current - recommended):
        if pid in by_id:
            ok, reason = team.playable(by_id[pid])
            changes.append(dict(_player(by_id[pid]), action="BENCH",
                                reason=reason or "outscored by a bench player"))

    gain = 0.0
    if current:
        import simulation
        held = [by_id[p] for p in current if p in by_id]
        gain = round(best["total"] - simulation.optimal_lineup(held)[0], 1)

    return {
        "ok": True,
        "week": ctx["week"],
        "league": (ctx["league"] or {}).get("name"),
        "projection_source": ctx["projection_source"],
        "projected_total": best["total"],
        "starters": [_player(p, p.get("slot")) for p in best["starters"]],
        "bench": [_player(p) for p in best["bench"]],
        "cannot_play": [dict(_player(p), reason=r)
                        for p, r in best["unavailable"]],
        "unfilled_slots": best["unfilled"],
        "changes": changes,
        "points_gained_by_changes": gain,
        "rules": league_rules(),
        "caveat": ("Projections cannot see this week's weather, a Friday "
                   "practice report or a late snap-count change. Check those "
                   "before locking a close call."
                   if not str(ctx["projection_source"]).startswith("season")
                   else "These are season averages, not this week's matchup - "
                        "weaker for a single week."),
    }


def get_roster(week=None, fresh=False):
    """Every player owned, scored, best first. No lineup decisions made."""
    ctx = context(week=week, fresh=fresh)
    return {
        "ok": True,
        "week": ctx["week"],
        "league": (ctx["league"] or {}).get("name"),
        "projection_source": ctx["projection_source"],
        "players": [_player(p) for p in ctx["owned"]],
        "roster_size": len(ctx["owned"]),
        "rules": league_rules(),
    }


def get_offers(week=None, fresh=False):
    """Trades other managers have sent, already scored.

    Reading only - accepting or rejecting still happens in the Sleeper app.
    """
    ctx = context(week=week, fresh=fresh)
    offers = team.pending_trades(ctx)
    return {
        "ok": True,
        "week": ctx["week"],
        "count": len(offers),
        "offers": [_offer(o) for o in offers],
        "rules": league_rules(),
    }


def _offer(offer):
    return {
        "transaction_id": offer.get("transaction_id"),
        "verdict": offer.get("verdict"),
        "points_change": offer.get("delta"),
        "lineup_before": offer.get("before"),
        "lineup_after": offer.get("after"),
        "you_send": [_player(p) for p in offer.get("giving") or []],
        "you_receive": [_player(p) for p in offer.get("getting") or []],
        "warnings": offer.get("notes") or [],
    }


def check_trade(give, get, week=None, fresh=False):
    """Score a proposed trade by what it does to the starting lineup.

    ``give`` and ``get`` are lists of player names - "Bijan Robinson" - or
    Sleeper player ids. A trade that improves the bench and not the lineup is
    worth nothing, and is scored as nothing.
    """
    ctx = context(week=week, fresh=fresh)
    giving, missing_give = team.resolve_names(list(give or []), ctx)
    getting, missing_get = team.resolve_names(list(get or []), ctx)
    owned = {p["player_id"] for p in ctx["owned"]}

    notes = ["Could not find a player called '%s'" % n
             for n in missing_give + missing_get]
    for player in getting:
        if player["player_id"] in owned:
            notes.append("You already own %s - ignored on the incoming side."
                         % player["name"])
    for name, player in zip(give or [], giving):
        if player["player_id"] not in owned:
            notes.append("You do not own %s - ignored on the outgoing side."
                         % player["name"])

    give_ids = [p["player_id"] for p in giving if p["player_id"] in owned]
    get_ids, seen = [], set()
    for player in getting:
        pid = player["player_id"]
        if pid not in owned and pid not in seen:
            seen.add(pid)
            get_ids.append(pid)

    if not give_ids or not get_ids:
        return {"ok": False,
                "error": "Name at least one player on each side that can "
                         "actually be traded.",
                "warnings": notes, "rules": league_rules()}

    result = team.evaluate_offer(ctx, give_ids, get_ids)
    out = _offer(result)
    out.pop("transaction_id", None)
    out.update({
        "ok": True,
        "week": ctx["week"],
        "projection_source": ctx["projection_source"],
        "lineup_after_trade": [_player(p, p.get("slot"))
                               for p in result["starters_after"]],
        "warnings": notes + (result.get("notes") or []),
        "rules": league_rules(),
        "caveat": ("Scored on projected points only. It does not price a "
                   "player's schedule, a coming bye week, or what you think "
                   "of the manager offering it."),
    })
    return out


def get_brief(week=None, fresh=False):
    """Everything at once, as plain text - lineup, changes, offers, rules.

    The one call to make if you only want to make one.
    """
    lineup = get_lineup(week=week, fresh=fresh)
    try:
        offers = get_offers(week=week)["offers"]
    except ApiError:
        offers = []

    lines = ["FANTASY BRIEF - %s" % (lineup["league"] or "your league"),
             "Week %s. Projections: %s."
             % (lineup["week"], lineup["projection_source"]),
             "",
             "LEAGUE RULES THAT CHANGE THE ADVICE:",
             "  %d teams, full PPR (1 point per catch)." % config.TEAMS,
             "  Passing touchdowns are worth %s, NOT 6."
             % config.SCORING.get("pass_td"),
             "  Starters: 1 QB, 2 RB, 2 WR, 1 TE, %d FLEX, 1 K, 1 DEF."
             % config.FLEX_SLOTS,
             "  %d bench spots and no IR slot." % config.BENCH,
             "",
             "START THIS LINEUP (projected %s):" % lineup["projected_total"]]
    for player in lineup["starters"]:
        lines.append("  %-5s %-24s %-3s %-4s %6.1f%s"
                     % (player["slot"], player["name"], player["position"],
                        player["team"] or "", player["projected_points"] or 0,
                        "  [%s]" % player["injury_status"]
                        if player["injury_status"] else ""))

    lines.append("")
    if lineup["changes"]:
        lines.append("CHANGE IN SLEEPER (worth about %s points):"
                     % lineup["points_gained_by_changes"])
        for change in lineup["changes"]:
            lines.append("  %-5s %-24s %-3s %6.1f%s"
                         % (change["action"], change["name"],
                            change["position"],
                            change["projected_points"] or 0,
                            "  (%s)" % change["reason"]
                            if change.get("reason") else ""))
    else:
        lines.append("NOTHING TO CHANGE - Sleeper already has the best lineup.")

    if lineup["cannot_play"]:
        lines.append("")
        lines.append("CANNOT PLAY:")
        for player in lineup["cannot_play"]:
            lines.append("  %-24s %-3s  %s"
                         % (player["name"], player["position"],
                            player["reason"]))

    if offers:
        lines.append("")
        lines.append("TRADE OFFERS WAITING IN SLEEPER:")
        for offer in offers:
            lines.append("  %s (%+.1f points)"
                         % (offer["verdict"], offer["points_change"] or 0))
            lines.append("    send:    %s"
                         % (", ".join(p["name"] for p in offer["you_send"])
                            or "nothing"))
            lines.append("    receive: %s"
                         % (", ".join(p["name"] for p in offer["you_receive"])
                            or "nothing"))
            for warning in offer["warnings"]:
                lines.append("    NOTE: %s" % warning)

    lines.append("")
    lines.append(lineup["caveat"])
    return "\n".join(lines)
