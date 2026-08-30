"""Sleeper API client. Standard library only - no pip install required.

The Sleeper API is read-only, free for non-commercial use, and needs no token.
The published rate limit is 1000 calls/minute; polling picks every 3 seconds
puts us at 20/minute, three orders of magnitude under the ceiling.

Everything here degrades gracefully: any fetch can fall back to the on-disk
cache, so the app still boots if Sleeper is unreachable at kickoff.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config

API = "https://api.sleeper.app/v1"
USER_AGENT = "SleeperDraftAssistant/1.0 (local personal use)"

# Endpoint shapes to try for projections, best guess first. The spec is right
# that this is the one uncertain piece of the data layer, so instead of betting
# on a single shape we probe several and report which one actually answered.
# Add your own here if you find a better one - the app will pick it up.
PROJECTION_CANDIDATES = [
    ("api.sleeper.com season",
     "https://api.sleeper.com/projections/nfl/{season}"
     "?season_type=regular&position[]=QB&position[]=RB&position[]=WR"
     "&position[]=TE&position[]=K&position[]=DEF&order_by=adp_ppr"),
    ("api.sleeper.app season",
     "https://api.sleeper.app/projections/nfl/{season}"
     "?season_type=regular&position[]=QB&position[]=RB&position[]=WR"
     "&position[]=TE&position[]=K&position[]=DEF&order_by=adp_ppr"),
    ("api.sleeper.com regular path",
     "https://api.sleeper.com/projections/nfl/regular/{season}"),
    ("api.sleeper.app regular path",
     "https://api.sleeper.app/projections/nfl/regular/{season}"),
    ("v1 projections",
     "https://api.sleeper.app/v1/projections/nfl/regular/{season}"),
]

# Prior-season actual stats, used only to derive ESTIMATED projections when no
# real projection source can be reached.
STATS_CANDIDATES = [
    ("api.sleeper.com stats", "https://api.sleeper.com/stats/nfl/regular/{season}"),
    ("v1 stats", "https://api.sleeper.app/v1/stats/nfl/regular/{season}"),
]


class SleeperError(Exception):
    pass


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def get_json(url, timeout=25, retries=3, backoff=2.0):
    """GET a URL and parse JSON, retrying on transient failures.

    Raises SleeperError on a definitive failure so callers can fall back to
    cache rather than crashing.
    """
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            if not body:
                raise SleeperError("empty response from %s" % url)
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx other than 429 will not improve by retrying.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise SleeperError("HTTP %s from %s" % (exc.code, url))
            last = exc
        except Exception as exc:  # noqa: BLE001 - network, JSON, timeouts alike
            last = exc
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    raise SleeperError("failed to fetch %s: %s" % (url, last))


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def cache_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # atomic: never leave a half-written cache behind
    return path


def cache_read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def cache_age_hours(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 3600.0
    except OSError:
        return None


# ---------------------------------------------------------------------------
# League / draft resolution
# ---------------------------------------------------------------------------

def get_user(username=None):
    username = username or config.USERNAME
    return get_json("%s/user/%s" % (API, urllib.parse.quote(str(username))))


def get_leagues(user_id, season=None):
    season = season or config.SEASON
    return get_json("%s/user/%s/leagues/%s/%s" % (API, user_id, config.SPORT, season))


def get_league(league_id):
    return get_json("%s/league/%s" % (API, league_id))


def get_league_users(league_id):
    return get_json("%s/league/%s/users" % (API, league_id))


def get_drafts(league_id):
    return get_json("%s/league/%s/drafts" % (API, league_id))


def get_draft(draft_id):
    return get_json("%s/draft/%s" % (API, draft_id))


def get_state():
    """Current NFL week and season, straight from Sleeper."""
    return get_json("%s/state/%s" % (API, config.SPORT))


def get_rosters(league_id):
    """Every roster in the league, with owner_id and player_ids."""
    return get_json("%s/league/%s/rosters" % (API, league_id))


def get_matchups(league_id, week):
    """Who plays whom this week, and each roster's starters."""
    return get_json("%s/league/%s/matchups/%s" % (API, league_id, week))


def get_transactions(league_id, week):
    """Trades, waivers and free-agent moves for a week."""
    return get_json("%s/league/%s/transactions/%s" % (API, league_id, week))


def my_roster(rosters, user_id):
    """Find my roster among the league's. Returns the roster dict or None."""
    for roster in rosters or []:
        if str(roster.get("owner_id")) == str(user_id):
            return roster
    return None


# Weekly projections. Same uncertainty as the season-long endpoint, so probe
# rather than assume - a start/sit call wants THIS week's number, not a
# season total divided by seventeen.
WEEKLY_PROJECTION_CANDIDATES = [
    ("api.sleeper.com week",
     "https://api.sleeper.com/projections/nfl/{season}/{week}"
     "?season_type=regular&position[]=QB&position[]=RB&position[]=WR"
     "&position[]=TE&position[]=K&position[]=DEF&order_by=ppr"),
    ("api.sleeper.app week",
     "https://api.sleeper.app/projections/nfl/{season}/{week}"
     "?season_type=regular&position[]=QB&position[]=RB&position[]=WR"
     "&position[]=TE&position[]=K&position[]=DEF&order_by=ppr"),
    ("v1 week",
     "https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}"),
]


def probe_weekly_projections(season, week, verbose=False):
    """Try to get projections for one specific week. Returns (rows, report).

    Fast-failing on purpose: this runs while someone is setting a lineup, so a
    source that is not answering should cost seconds, not a minute. The first
    candidate is built from API so it follows the configured base rather than
    always reaching for a fixed host.
    """
    candidates = [("api base week",
                   "%s/projections/%s/%s/%s" % (API, config.SPORT, season, week))]
    candidates += [(label, template.format(season=season, week=week))
                   for label, template in WEEKLY_PROJECTION_CANDIDATES]
    return probe_projections(season, candidates=candidates, verbose=verbose,
                             timeout=6, retries=1)


def get_user_drafts(user_id, season=None):
    """Every draft this user is in, including mock drafts.

    A Sleeper mock draft is its own draft object, not attached to any league,
    so it never shows up under the league's drafts. This is how we find one to
    practise against.
    """
    season = season or config.SEASON
    return get_json("%s/user/%s/drafts/%s/%s"
                    % (API, user_id, config.SPORT, season))


def draft_is_mock(draft, league_ids=()):
    """True when a draft is not attached to one of the user's real leagues."""
    league_id = (draft or {}).get("league_id")
    if not league_id:
        return True
    return str(league_id) not in {str(x) for x in league_ids}


def get_picks(draft_id):
    return get_json("%s/draft/%s/picks" % (API, draft_id))


def pick_league(leagues, name=None):
    """Find the target league by name, case- and whitespace-insensitively."""
    name = (name or config.LEAGUE_NAME).strip().lower()
    for lg in leagues or []:
        if str(lg.get("name", "")).strip().lower() == name:
            return lg
    # Fall back to the only league if there is exactly one; better than nothing
    # 20 minutes before a draft.
    if leagues and len(leagues) == 1:
        return leagues[0]
    return None


def verify_league_settings(league):
    """Compare the live league against the confirmed spec. Returns warnings."""
    warnings = []
    if not league:
        return ["Could not load league settings to verify."]

    total = league.get("total_rosters")
    if total and int(total) != config.TEAMS:
        warnings.append(
            "Team count is %s live but %s in config - VOR baselines are wrong "
            "until you fix config.TEAMS." % (total, config.TEAMS))

    # Deliberately NOT checked here: league.settings.draft_rounds. Sleeper
    # leaves that at a default (it read 3 on a 15-round league) and the draft
    # object is what actually governs the draft, so comparing it produced a
    # scary false alarm. The real round count is validated in
    # Assistant.draft_shape() against the draft object itself.

    scoring = league.get("scoring_settings") or {}
    checks = [("rec", 1.0), ("pass_td", 4.0), ("pass_int", -1.0),
              ("fum_lost", -2.0), ("rec_td", 6.0), ("rush_td", 6.0),
              ("pass_yd", 0.04), ("rush_yd", 0.1), ("rec_yd", 0.1)]
    for key, expected in checks:
        if key in scoring and abs(float(scoring[key]) - expected) > 1e-6:
            # The app already scores off the live settings, so this is
            # information rather than a problem. Say so, or it reads as an
            # error and undermines trust in the warnings that do matter.
            warnings.append(
                "Your league scores %s at %s, not the %s written in the build "
                "spec. The app is using your league's value - no action needed."
                % (key, scoring[key], expected))

    roster_positions = league.get("roster_positions") or []
    if roster_positions:
        qbs = roster_positions.count("QB")
        if roster_positions.count("SUPER_FLEX") or qbs > 1:
            warnings.append(
                "This looks like a superflex/2QB league. The QB strategy in "
                "config.py assumes ONE QB and will be badly wrong.")
        flex = roster_positions.count("FLEX")
        if flex and flex != config.FLEX_SLOTS:
            warnings.append("FLEX slots live=%s config=%s." % (flex, config.FLEX_SLOTS))
    return warnings


# Sleeper's own scoring keys that spell one of our stats differently. This map
# is deliberately explicit and one-directional: it must NEVER merge two distinct
# Sleeper stats onto the same key. In particular `fum` (all fumbles) and
# `fum_lost` (fumbles lost) are different stats and are both kept.
SLEEPER_SCORING_KEYMAP = {
    "fgmiss": "fg_miss",
    "fgmiss_0_19": "fg_miss", "fgmiss_20_29": "fg_miss",
    "fgmiss_30_39": "fg_miss", "fgmiss_40_49": "fg_miss",
    "fgmiss_50p": "fg_miss",
    "xpmiss": "xp_miss",
    "fgm_50p": "fgm_50_59",
    "fgm_60p": "fgm_60_plus",
    "safe": "safety",
    "ff": "forced_fumble",
    "blk_kick": "blocked_kick",
    "st_ff": "st_forced_fumble",
    "pts_allow_35p": "pts_allow_35_plus",
}


def live_scoring_settings(league):
    """Return the league's live scoring settings, keyed the way we score.

    The spec's table is a hand transcription; the live league is the truth. We
    take Sleeper's keys almost verbatim, translating only the handful it spells
    differently. We deliberately do NOT run these through the paste-table alias
    table: that one maps a column headed "FUM" onto fumbles lost, which is right
    for a third-party CSV and wrong for Sleeper, where `fum` and `fum_lost` are
    separate stats that would collide onto one key.
    """
    raw = (league or {}).get("scoring_settings") or {}
    out = {}
    for key, value in raw.items():
        name = str(key).strip().lower()
        name = SLEEPER_SCORING_KEYMAP.get(name, name)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        # Several Sleeper keys can fold onto one of ours (the fgmiss family).
        # Keep the largest magnitude rather than letting dict order decide.
        if name in out and abs(out[name]) >= abs(value):
            continue
        out[name] = value
    return out


DRAFT_STATUS_RANK = {"drafting": 0, "paused": 1, "pre_draft": 2, "complete": 3}


def pick_draft(drafts, rounds=None, draft_id=None):
    """Choose the real draft when a league has several.

    Leagues often carry leftover or test drafts, and taking drafts[0] blindly
    picks whichever the API happens to list first - which can be a 3-round
    practice draft. Prefer, in order: an explicitly requested id, a draft that
    is live or upcoming, one whose round count matches the league, and finally
    the most recently created.

    Returns (chosen, others).
    """
    drafts = [d for d in (drafts or []) if isinstance(d, dict)]
    if not drafts:
        return None, []

    draft_id = draft_id or config.DRAFT_ID_OVERRIDE
    if draft_id:
        for draft in drafts:
            if str(draft.get("draft_id")) == str(draft_id):
                return draft, [d for d in drafts if d is not draft]

    rounds = rounds or config.ROUNDS

    def sort_key(draft):
        settings = draft.get("settings") or {}
        status_rank = DRAFT_STATUS_RANK.get(draft.get("status"), 4)
        matches_rounds = 0 if int(settings.get("rounds") or 0) == rounds else 1
        matches_teams = 0 if int(settings.get("teams") or 0) == config.TEAMS else 1
        created = draft.get("start_time") or draft.get("created") or 0
        return (status_rank, matches_rounds, matches_teams, -int(created or 0))

    ordered = sorted(drafts, key=sort_key)
    return ordered[0], ordered[1:]


def describe_draft(draft):
    settings = (draft or {}).get("settings") or {}
    return "%s  status=%s type=%s teams=%s rounds=%s" % (
        (draft or {}).get("draft_id"), (draft or {}).get("status"),
        (draft or {}).get("type"), settings.get("teams"), settings.get("rounds"))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


def slim_players(raw):
    """Cut the ~5MB player dump down to the fields the app actually uses.

    Sleeper's docs ask that this endpoint be called at most once a day, so we
    fetch once, slim it, and never call it again during the draft.
    """
    out = {}
    for pid, p in (raw or {}).items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position") or ""
        fantasy_positions = p.get("fantasy_positions") or []
        if pos not in FANTASY_POSITIONS and not (
                set(fantasy_positions) & FANTASY_POSITIONS):
            continue
        if pos not in FANTASY_POSITIONS and fantasy_positions:
            pos = next(x for x in fantasy_positions if x in FANTASY_POSITIONS)

        name = p.get("full_name")
        if not name:
            first, last = p.get("first_name") or "", p.get("last_name") or ""
            name = ("%s %s" % (first, last)).strip() or pid

        out[str(pid)] = {
            "player_id": str(pid),
            "name": name,
            "team": p.get("team") or "FA",
            "position": pos,
            "age": p.get("age"),
            "years_exp": p.get("years_exp"),
            "injury_status": p.get("injury_status"),
            "status": p.get("status"),
            "depth_chart_order": p.get("depth_chart_order"),
            "depth_chart_position": p.get("depth_chart_position"),
            "number": p.get("number"),
            "search_rank": p.get("search_rank"),
        }
    return out


def fetch_players(force=False):
    """Fetch and cache the player database. Returns (players, source)."""
    age = cache_age_hours(config.PLAYERS_CACHE)
    if not force and age is not None and age < 24:
        cached = cache_read(config.PLAYERS_CACHE)
        if cached:
            return cached, "cache (%.1fh old)" % age

    try:
        raw = get_json("%s/players/%s" % (API, config.SPORT), timeout=120)
        players = slim_players(raw)
        if not players:
            raise SleeperError("player dump parsed to zero fantasy players")
        cache_write(config.PLAYERS_CACHE, players)
        return players, "sleeper api (%d players)" % len(players)
    except SleeperError:
        cached = cache_read(config.PLAYERS_CACHE)
        if cached:
            return cached, "cache after fetch failure"
        raise


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------

def _rows_from_payload(payload):
    """Normalise any of the shapes Sleeper might return into flat rows.

    Handles:
      * list of {player_id, stats:{...}}
      * list of {player_id, ...flat stats}
      * dict {player_id: {stats...}}
      * dict {player_id: {stats:{...}}}
    """
    rows = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            pid = item.get("player_id") or item.get("id")
            player = item.get("player") if isinstance(item.get("player"), dict) else {}
            if not pid and player:
                pid = player.get("player_id") or player.get("id")
            stats = item.get("stats") if isinstance(item.get("stats"), dict) else {
                k: v for k, v in item.items()
                if k not in ("player_id", "id", "player", "team", "position",
                             "opponent", "week", "season", "season_type",
                             "category", "date", "game_id", "sport", "company",
                             "updated_at", "last_modified")
            }
            if pid:
                rows.append({
                    "player_id": str(pid),
                    "stats": stats or {},
                    "meta": {
                        "team": item.get("team") or player.get("team"),
                        "position": item.get("position") or player.get("position"),
                    },
                })
    elif isinstance(payload, dict):
        for pid, item in payload.items():
            if isinstance(item, dict):
                stats = item.get("stats") if isinstance(item.get("stats"), dict) else item
                rows.append({"player_id": str(pid), "stats": stats or {}, "meta": {}})
    return rows


def _looks_useful(rows):
    """A payload is only useful if it carries scoreable volume stats."""
    if len(rows) < 50:
        return False
    import scoring as scoring_mod

    volume_keys = {"pass_yd", "rush_yd", "rec_yd", "rec", "pass_td", "rush_td", "rec_td"}
    hits = 0
    for row in rows[:400]:
        canon = {scoring_mod.canonical_stat(k) for k in row["stats"]}
        if canon & volume_keys:
            hits += 1
    return hits >= 25


def probe_projections(season=None, candidates=None, verbose=True,
                      timeout=45, retries=2):
    """Try each candidate endpoint shape and return the first useful one.

    Returns (rows, report) where report lists every attempt and its outcome, so
    build_data.py can print exactly what was verified rather than assuming.
    """
    season = season or config.SEASON
    candidates = candidates or PROJECTION_CANDIDATES
    report = []
    for label, template in candidates:
        url = template.format(season=season)
        try:
            payload = get_json(url, timeout=timeout, retries=retries)
        except SleeperError as exc:
            report.append({"label": label, "url": url, "ok": False, "detail": str(exc)})
            if verbose:
                print("  [x] %-28s %s" % (label, exc))
            continue
        rows = _rows_from_payload(payload)
        useful = _looks_useful(rows)
        report.append({
            "label": label, "url": url, "ok": useful,
            "detail": "%d rows, %s" % (len(rows), "scoreable" if useful
                                       else "no scoreable volume stats"),
        })
        if verbose:
            print("  [%s] %-28s %d rows" % ("ok" if useful else "x", label, len(rows)))
        if useful:
            return rows, report
    return [], report


def extract_adp(rows):
    """Pull Sleeper ADP out of a projections payload when it carries one.

    The api.sleeper.com projections shape includes adp fields; if it is the one
    that answered, we get real Sleeper ADP for free and the paste box becomes
    optional rather than required.
    """
    adp = {}
    for row in rows:
        stats = row.get("stats") or {}
        for key in ("adp_ppr", "adp_full_ppr", "adp_std", "adp_2qb", "adp_dynasty_ppr", "adp"):
            value = stats.get(key)
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                adp[row["player_id"]] = value
                break
    return adp
