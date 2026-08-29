"""Fantasy point computation from raw projected stats.

The whole point of this module is that we never trust a data source's own
"fantasy points" or "PPR" column. Sources disagree about passing-TD value and
fumble penalties, and in this league (4-point passing TDs, -2 fumbles lost,
full PPR) those disagreements are worth several draft slots at QB and RB.
"""

import config

# Different sources spell the same stat differently. Map every spelling we have
# seen onto the canonical key used in config.SCORING.
STAT_ALIASES = {
    # passing
    "passing_yards": "pass_yd", "pass_yds": "pass_yd", "pass_yards": "pass_yd",
    "py": "pass_yd", "passyds": "pass_yd", "yds_pass": "pass_yd",
    "passing_tds": "pass_td", "pass_tds": "pass_td", "ptd": "pass_td",
    "passing_td": "pass_td", "tds_pass": "pass_td",
    "interceptions": "pass_int", "ints": "pass_int", "int_thrown": "pass_int",
    "pass_ints": "pass_int", "pass_interceptions": "pass_int",
    "pass_2pt_conversions": "pass_2pt", "pass_2pc": "pass_2pt",
    # rushing
    "rushing_yards": "rush_yd", "rush_yds": "rush_yd", "rush_yards": "rush_yd",
    "ry": "rush_yd", "rushyds": "rush_yd",
    "rushing_tds": "rush_td", "rush_tds": "rush_td", "rtd": "rush_td",
    "rushing_td": "rush_td",
    "rush_2pt_conversions": "rush_2pt", "rush_2pc": "rush_2pt",
    # receiving
    "receptions": "rec", "catches": "rec", "rec_": "rec", "recs": "rec",
    "receiving_yards": "rec_yd", "rec_yds": "rec_yd", "rec_yards": "rec_yd",
    "recyds": "rec_yd", "receiving_yds": "rec_yd",
    "receiving_tds": "rec_td", "rec_tds": "rec_td", "receiving_td": "rec_td",
    "rec_2pt_conversions": "rec_2pt", "rec_2pc": "rec_2pt",
    # fumbles
    "fumbles_lost": "fum_lost", "fuml": "fum_lost", "fum": "fum_lost",
    "fumbles": "fum_lost", "lost_fumbles": "fum_lost",
    "fum_td": "fum_rec_td",
    # kicking - Sleeper's own keys plus common CSV spellings
    "fgm_50p": "fgm_50_59", "fgm_50_plus": "fgm_50_59", "fgm50": "fgm_50_59",
    "fgm_60p": "fgm_60_plus", "fgm60": "fgm_60_plus",
    "fgmiss": "fg_miss", "fgm_miss": "fg_miss", "fg_missed": "fg_miss",
    "xpmiss": "xp_miss", "xp_missed": "xp_miss", "xpm_miss": "xp_miss",
    "xp_made": "xpm", "xpmade": "xpm", "pat": "xpm", "patm": "xpm",
    # defense - Sleeper abbreviations
    "safe": "safety", "sfty": "safety",
    "ff": "forced_fumble", "def_ff": "forced_fumble",
    "blk_kick": "blocked_kick", "blkkick": "blocked_kick",
    "def_st_td": "st_td", "st_ff": "st_forced_fumble",
    "def_sack": "sack", "sacks": "sack",
    "def_int": "int", "def_interceptions": "int",
    "def_fum_rec": "fum_rec", "fumble_recovery": "fum_rec",
    "def_pts_allow": "pts_allow", "pa": "pts_allow", "points_allowed": "pts_allow",
    "pts_allow_35p": "pts_allow_35_plus",
    "idp_ff": "st_player_ff",
}

# Points-allowed buckets, high to low, as (inclusive_min, inclusive_max, key).
PTS_ALLOW_BUCKETS = [
    (0, 0, "pts_allow_0"),
    (1, 6, "pts_allow_1_6"),
    (7, 13, "pts_allow_7_13"),
    (14, 20, "pts_allow_14_20"),
    (21, 27, "pts_allow_21_27"),
    (28, 34, "pts_allow_28_34"),
    (35, 10_000, "pts_allow_35_plus"),
]

# Stat keys that are scoring inputs. Anything else in a projection payload
# (snaps, attempts, targets, games played) is carried along but not scored.
SCORABLE = set(config.SCORING)


def canonical_stat(key):
    """Normalise a stat key from any source onto our canonical vocabulary."""
    k = str(key).strip().lower().replace(" ", "_").replace("-", "_")
    k = k.replace("__", "_")
    if k in SCORABLE:
        return k
    return STAT_ALIASES.get(k, k)


def normalize_stats(raw):
    """Rewrite a raw stat dict onto canonical keys, dropping non-numerics."""
    out = {}
    for key, value in (raw or {}).items():
        canon = canonical_stat(key)
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        # If two source keys collapse onto the same canonical key, keep the
        # larger magnitude rather than silently letting dict order decide.
        if canon in out and abs(out[canon]) >= abs(num):
            continue
        out[canon] = num
    return out


def _points_allowed_component(stats, scoring):
    """Score a defense's points-allowed, from either buckets or a raw average.

    Sleeper projections sometimes give per-bucket game counts and sometimes a
    single projected points-allowed figure. Handle both, and never double count.
    """
    bucket_keys = [k for (_, _, k) in PTS_ALLOW_BUCKETS]
    present = [k for k in bucket_keys if k in stats]
    if present:
        return sum(stats[k] * scoring.get(k, 0.0) for k in present), True

    if "pts_allow" not in stats:
        return 0.0, False

    # A season-long projection is points allowed per game; anything larger is a
    # full-season total that we convert back to a per-game average.
    per_game = stats["pts_allow"]
    if per_game > 60:
        games = stats.get("gp") or stats.get("games") or 17.0
        if games:
            per_game = per_game / games
    games = stats.get("gp") or stats.get("games") or 17.0
    for low, high, key in PTS_ALLOW_BUCKETS:
        if low <= per_game <= high:
            return scoring.get(key, 0.0) * games, True
    return 0.0, True


def fantasy_points(raw_stats, position=None, scoring=None):
    """Compute projected fantasy points from raw projected stats.

    `raw_stats` may use any source's spelling; it is normalised first. Returns
    a float. Stats with no scoring value contribute nothing.
    """
    scoring = scoring or config.SCORING
    stats = normalize_stats(raw_stats)

    total = 0.0
    for key, value in stats.items():
        if key.startswith("pts_allow"):
            continue
        weight = scoring.get(key)
        if weight:
            total += value * weight

    if (position or "").upper() in ("DEF", "DST", "D/ST"):
        pa_points, _ = _points_allowed_component(stats, scoring)
        total += pa_points

    return round(total, 2)


def score_breakdown(raw_stats, position=None, scoring=None):
    """Same as fantasy_points but returns the per-stat contributions.

    Used by the UI so a recommendation can show where the points come from
    rather than being a black box.
    """
    scoring = scoring or config.SCORING
    stats = normalize_stats(raw_stats)
    parts = {}
    for key, value in stats.items():
        if key.startswith("pts_allow"):
            continue
        weight = scoring.get(key)
        if weight:
            parts[key] = round(value * weight, 2)
    if (position or "").upper() in ("DEF", "DST", "D/ST"):
        pa_points, had = _points_allowed_component(stats, scoring)
        if had:
            parts["points_allowed"] = round(pa_points, 2)
    return parts
