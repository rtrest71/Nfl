"""League configuration for the Sleeper Draft Assistant.

Every value here is a tunable constant. The scoring table and roster shape are
transcribed from the confirmed league settings; the strategy knobs below are
the ones worth adjusting if you change your mind before the draft.
"""

# ---------------------------------------------------------------------------
# League identity
# ---------------------------------------------------------------------------
USERNAME = "rtrestini2019"
LEAGUE_NAME = "Fantasy NFL 2026"
SEASON = "2026"
SPORT = "nfl"

TEAMS = 12
ROUNDS = 15
DRAFT_TYPE = "snake"

# ---------------------------------------------------------------------------
# Roster: 15 total. Not superflex - one QB only.
# ---------------------------------------------------------------------------
STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
FLEX_SLOTS = 2
FLEX_ELIGIBLE = ("RB", "WR", "TE")
BENCH = 5

ROSTER_SIZE = sum(STARTERS.values()) + FLEX_SLOTS + BENCH  # 15

# ---------------------------------------------------------------------------
# Exact scoring, transcribed from the league settings.
#
# Do NOT substitute a generic "PPR points" column from any data source. Sources
# differ on passing-TD value and fumble penalties, and those differences move QB
# and RB rankings by several spots.
# ---------------------------------------------------------------------------
SCORING = {
    # PASSING
    "pass_yd": 0.04,          # 25 yds = 1 pt
    "pass_td": 4.0,
    "pass_2pt": 2.0,
    "pass_int": -1.0,
    # RUSHING
    "rush_yd": 0.1,           # 10 yds = 1 pt
    "rush_td": 6.0,
    "rush_2pt": 2.0,
    # RECEIVING
    "rec": 1.0,               # FULL PPR
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec_2pt": 2.0,
    # KICKING
    "fgm_0_19": 3.0,
    "fgm_20_29": 3.0,
    "fgm_30_39": 3.0,
    "fgm_40_49": 4.0,
    "fgm_50_59": 5.0,
    "fgm_60_plus": 6.0,
    "xpm": 1.0,
    "fg_miss": -1.0,
    "xp_miss": -1.0,
    # TEAM DEFENSE
    "def_td": 6.0,
    "pts_allow_0": 10.0,
    "pts_allow_1_6": 7.0,
    "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0,
    "pts_allow_21_27": 0.0,   # not listed in settings; assumed 0
    "pts_allow_28_34": -1.0,
    "pts_allow_35_plus": -4.0,
    "sack": 1.0,
    "int": 2.0,
    "fum_rec": 2.0,
    "safety": 2.0,
    "forced_fumble": 1.0,
    "blocked_kick": 2.0,
    # SPECIAL TEAMS (defense)
    "st_td": 6.0,
    "st_forced_fumble": 1.0,
    "st_fum_rec": 1.0,
    # SPECIAL TEAMS (player)
    "st_player_td": 6.0,
    "st_player_ff": 1.0,
    "st_player_fum_rec": 1.0,
    # MISC
    "fum_lost": -2.0,
    "fum": 0.0,          # all fumbles, scored separately from fumbles LOST.
                         # Sleeper treats these as two different stats; leave at
                         # 0 unless your league's live settings say otherwise.
    "fum_rec_td": 6.0,
}

# Set these only to override what Sleeper reports. Leave as None normally.
DRAFT_ID_OVERRIDE = None     # force a specific draft_id if the league has several
ROUNDS_OVERRIDE = None       # force the round count if the draft object is wrong

# ---------------------------------------------------------------------------
# VOR baselines
#
# 12 teams x starters, plus the flex slots allocated by full-PPR flex tendency.
# FLEX = 2 x 12 = 24 slots -> ~40% RB, ~50% WR, ~10% TE.
# ---------------------------------------------------------------------------
FLEX_SPLIT = {"RB": 0.40, "WR": 0.50, "TE": 0.10}

# Kickers and defenses are streamed, so their baseline is simply the last
# startable one. Positions not listed fall back to starters x teams.
BASELINE_OVERRIDES = {}

# ---------------------------------------------------------------------------
# Strategy knobs
# ---------------------------------------------------------------------------

# Round at which "balanced" flips from floor-weighted to upside-weighted.
UPSIDE_CROSSOVER_ROUND = 7

# Hard positional blocks (see spec section 5).
QB_UNLOCK_ROUND = 8          # no QB recommendation before this round...
QB_ELITE_STEAL_VOR = 45.0    # ...unless an elite one has fallen this far past
QB_ELITE_ADP_FALL = 12       # his ADP and still carries this much VOR.
K_UNLOCK_ROUND = 14
DEF_UNLOCK_ROUND = 14

# Injury penalty, in projected points, by Sleeper injury_status. No IR slots and
# only 5 bench spots means an injured player burns a roster spot for nothing.
INJURY_PENALTY = {
    "IR": 60.0,
    "PUP": 45.0,
    "NA": 45.0,
    "Out": 35.0,
    "Doubtful": 25.0,
    "Suspended": 40.0,
    "Questionable": 6.0,
    "Sus": 40.0,
    "COV": 8.0,
    "DNR": 45.0,
}

# Age curves. Sweet spot gets a boost; the cliff gets a penalty.
AGE_SWEET_SPOT = {"RB": (22, 26), "WR": (23, 28), "TE": (24, 29), "QB": (24, 33)}
AGE_BOOST = 4.0
RB_AGE_CLIFF = 30
RB_AGE_CLIFF_PENALTY = 12.0
WR_AGE_CLIFF = 31
WR_AGE_CLIFF_PENALTY = 8.0

# Depth-chart penalty. depth_chart_order >= 2 means a backup: with no IR slots
# and 5 bench spots there is no room to roster handcuffs.
BACKUP_RB_PENALTY = 18.0
BACKUP_WR_PENALTY = 10.0
HANDCUFF_BLOCK_ROUND = 12    # never recommend a pure backup before this round

# VOR measures a player's value ASSUMING HE STARTS. A player who would sit on
# your bench every week does not deliver that value, so his score is discounted
# to this fraction. This is what stops the engine drafting a sixth running back
# ahead of your first receiver in a league that starts seven RB/WR/TE.
BENCH_VALUE_MULTIPLIER = 0.45

# The value gap compares where the market drafts a player against where his
# production ranks him. It is only meaningful among players who are actually
# drafted: a kicker nobody takes until pick 3000 is not "a bargain", he is
# irrelevant. Rank both sides within this ADP window so the number means
# something - roughly the draft's 180 picks plus a healthy margin.
VALUE_GAP_ADP_LIMIT = 260

# Value-gap boost: how many points per round of positive (ADP rank - VOR rank).
VALUE_GAP_POINTS_PER_PICK = 0.22
VALUE_GAP_MAX_BOOST = 14.0

# Tier detection: cut a tier when the gap between consecutive players at a
# position exceeds this multiple of the running average gap.
TIER_GAP_MULTIPLIER = 1.5
TIER_MIN_PLAYERS = 3

# ADP standard deviation when none is supplied: a fraction of the ADP value,
# growing in later rounds because late-round consensus is much looser.
ADP_STDEV_FRACTION = 0.20
ADP_STDEV_LATE_FRACTION = 0.25
ADP_STDEV_LATE_THRESHOLD = 60      # pick number past which the wider band applies
ADP_STDEV_MIN = 4.0

# Opponent-need adjustment: if managers picking before you need a position, that
# position's players are likelier to be gone. Expressed as picks of ADP shift
# per needy manager, capped so it can never dominate the real ADP signal.
OPPONENT_NEED_SHIFT_PER_MANAGER = 1.5
OPPONENT_NEED_MAX_SHIFT = 6.0

# Run detection: N players at one position within the last M picks.
RUN_THRESHOLD = 3
RUN_WINDOW = 6

# Queue export length.
QUEUE_LENGTH = 40

# Polling interval for the live picks endpoint, in seconds.
POLL_SECONDS = 3

# Bye weeks that matter for the fantasy playoffs.
PLAYOFF_WEEKS = (15, 16, 17)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

PLAYERS_CACHE = os.path.join(CACHE_DIR, "players.json")
PROJECTIONS_CACHE = os.path.join(CACHE_DIR, "projections.json")
ADP_CACHE = os.path.join(CACHE_DIR, "adp.json")
LEAGUE_CACHE = os.path.join(CACHE_DIR, "league.json")
STATE_CACHE = os.path.join(CACHE_DIR, "state.json")


def baselines():
    """Return the VOR baseline rank for each position.

    QB   1 x 12 = 12                       -> QB12
    RB   2 x 12 = 24 + 40% of 24 flex = 34 -> RB34
    WR   2 x 12 = 24 + 50% of 24 flex = 36 -> WR36
    TE   1 x 12 = 12 + 10% of 24 flex = 14 -> TE14
    K    1 x 12 = 12                       -> K12
    DEF  1 x 12 = 12                       -> DEF12
    """
    out = {}
    flex_total = FLEX_SLOTS * TEAMS
    for pos, count in STARTERS.items():
        base = count * TEAMS
        if pos in FLEX_ELIGIBLE:
            base += int(round(flex_total * FLEX_SPLIT.get(pos, 0.0)))
        out[pos] = BASELINE_OVERRIDES.get(pos, base)
    return out
