"""A fake Sleeper API, so the whole app can be exercised without the network.

Serves the same endpoint shapes the real API does, backed by a synthetic but
realistically shaped player universe. Used by the mock-draft test to drive the
real Assistant through all 15 rounds.
"""

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

USER_ID = "100000000000000001"
LEAGUE_ID = "900000000000000001"
DRAFT_ID = "800000000000000001"
MOCK_DRAFT_ID = "700000000000000009"

TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
         "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
         "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
         "TEN", "WAS"]

FIRST = ["Jalen", "Marcus", "Trey", "Deion", "Kyren", "Bijan", "Puka", "Malik",
         "Rome", "Drake", "Garrett", "Zay", "Jayden", "Brock", "Tank", "Xavier",
         "Cooper", "Jaxon", "Ladd", "Jordan", "Chase", "Trevor", "Rashee",
         "Quentin", "Devon", "Isaiah", "Caleb", "Bryce", "Anthony", "Blake"]
LAST = ["Harrison", "Nabers", "Odunze", "Bowers", "Brooks", "Wright", "Coleman",
        "Thomas", "Franklin", "McConkey", "Legette", "Mitchell", "Corley",
        "Polk", "Worthy", "Pearsall", "Wilson", "Burton", "Sanders", "Downs",
        "Flowers", "Hyatt", "Johnston", "Robinson", "Gibbs", "Achane", "Allen",
        "Nix", "Penix", "Maye"]


def _stat_line(position, quality):
    """A plausible raw stat line. `quality` runs 1.0 (elite) down to 0.0."""
    q = max(0.02, quality)
    if position == "QB":
        return {
            "pass_yd": round(2600 + 2100 * q),
            "pass_td": round(11 + 24 * q),
            "pass_int": round(16 - 7 * q),
            "rush_yd": round(60 + 520 * q ** 2),
            "rush_td": round(1 + 6 * q ** 2),
            "fum_lost": round(4 - 2 * q),
        }
    if position == "RB":
        return {
            "rush_yd": round(220 + 1330 * q),
            "rush_td": round(1 + 12 * q),
            "rec": round(9 + 76 * q),
            "rec_yd": round(60 + 640 * q),
            "rec_td": round(0 + 4 * q),
            "fum_lost": round(3 - 2 * q),
        }
    if position == "WR":
        return {
            "rec": round(16 + 100 * q),
            "rec_yd": round(190 + 1400 * q),
            "rec_td": round(1 + 11 * q),
            "rush_yd": round(4 + 60 * q ** 3),
            "fum_lost": round(2 - q),
        }
    if position == "TE":
        return {
            "rec": round(11 + 80 * q),
            "rec_yd": round(105 + 1000 * q),
            "rec_td": round(0 + 9 * q),
            "fum_lost": 1,
        }
    if position == "K":
        return {
            "fgm_20_29": round(4 + 6 * q), "fgm_30_39": round(4 + 6 * q),
            "fgm_40_49": round(3 + 6 * q), "fgm_50_59": round(1 + 4 * q),
            "xpm": round(19 + 22 * q), "fg_miss": round(7 - 4 * q),
        }
    return {  # DEF
        "sack": round(24 + 26 * q), "int": round(6 + 12 * q),
        "fum_rec": round(4 + 9 * q), "def_td": round(0 + 4 * q),
        "safety": 0 if q < 0.6 else 1, "blocked_kick": round(0 + 2 * q),
        "pts_allow": round(27 - 10 * q),
    }


def build_universe(seed=7):
    """Return (raw_players, projection_rows, adp_by_id) for a fake NFL."""
    rng = random.Random(seed)
    counts = {"QB": 40, "RB": 78, "WR": 108, "TE": 44, "K": 32, "DEF": 32}
    players, projections = {}, []
    pid = 1000

    for position, total in counts.items():
        for i in range(total):
            quality = max(0.0, 1.0 - (i / float(total)) ** 0.8)
            pid += 1
            key = str(pid)
            team = TEAMS[i % len(TEAMS)] if position != "DEF" else TEAMS[i]

            if position == "DEF":
                key = team
                players[key] = {
                    "player_id": key, "first_name": team, "last_name": "Defense",
                    "full_name": "%s Defense" % team, "position": "DEF",
                    "fantasy_positions": ["DEF"], "team": team, "age": None,
                    "years_exp": None, "injury_status": None, "status": "Active",
                    "depth_chart_order": None, "search_rank": 2000 + i * 3,
                }
            else:
                name = "%s %s%s" % (
                    FIRST[(i * 7 + pid) % len(FIRST)],
                    LAST[(i * 3 + pid) % len(LAST)],
                    " Jr." if i % 17 == 0 else ("" if i % 23 else " II"))
                players[key] = {
                    "player_id": key,
                    "first_name": name.split()[0], "last_name": name.split()[1],
                    "full_name": name, "position": position,
                    "fantasy_positions": [position], "team": team,
                    "age": rng.randint(21, 33),
                    "years_exp": rng.randint(0, 11),
                    # A handful of injuries, so the risk layer has something real.
                    "injury_status": "Questionable" if i % 19 == 5 else (
                        "IR" if i % 41 == 7 else None),
                    "status": "Active",
                    "depth_chart_order": 1 if i < total // 2 else rng.randint(1, 3),
                    "search_rank": None,
                }

            projections.append({"player_id": key, "stats": _stat_line(position, quality)})

    # Sleeper's search_rank roughly tracks its own ADP; approximate it by
    # ordering on a positional value curve so the fallback path is exercised.
    weights = {"RB": 1.0, "WR": 1.0, "TE": 0.72, "QB": 0.55, "K": 0.05, "DEF": 0.06}
    ranked = []
    for key, player in players.items():
        position = player["position"]
        index = [p["player_id"] for p in projections].index(key)
        stats = projections[index]["stats"]
        volume = (stats.get("rec", 0) * 2 + stats.get("rec_yd", 0) * 0.1
                  + stats.get("rush_yd", 0) * 0.1 + stats.get("pass_yd", 0) * 0.02
                  + stats.get("sack", 0) + stats.get("xpm", 0))
        ranked.append((volume * weights.get(position, 0.5), key))
    ranked.sort(reverse=True)
    adp = {}
    for order, (_, key) in enumerate(ranked, start=1):
        players[key]["search_rank"] = order
        adp[key] = float(order)
    return players, projections, adp


class FakeSleeper:
    """A threaded HTTP server speaking the Sleeper endpoints we use."""

    def __init__(self, players, draft_order=None, status="pre_draft"):
        self.players = players
        self.picks = []
        self.status = status
        self.draft_order = draft_order or {}
        self.lock = threading.Lock()
        # A standalone mock draft, with its own picks and its own slot.
        self.mock_picks = []
        self.mock_status = "drafting"
        self.mock_draft_order = {USER_ID: 4}
        # In-season state
        self.week = 1
        self.my_players = []
        self.my_starters = []
        self.weekly = {}          # player_id -> per-week stat line

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                body = outer.route(path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def draft_object(self):
        return {
            "draft_id": DRAFT_ID, "status": self.status, "type": "snake",
            "settings": {"teams": 12, "rounds": config.ROUNDS,
                         "reversal_round": 0},
            "draft_order": self.draft_order or None,
            "slot_to_roster_id": {str(i): i for i in range(1, 13)},
            "league_id": LEAGUE_ID, "season": "2026",
        }

    def mock_object(self):
        """A Sleeper mock draft: a real draft object with no league attached."""
        return {
            "draft_id": MOCK_DRAFT_ID, "status": self.mock_status, "type": "snake",
            "settings": {"teams": 12, "rounds": config.ROUNDS,
                         "reversal_round": 0},
            "draft_order": self.mock_draft_order or None,
            "slot_to_roster_id": {}, "league_id": None, "season": "2026",
        }

    def add_mock_pick(self, pick_no, round_no, slot, player_id):
        with self.lock:
            self.mock_picks.append({
                "pick_no": pick_no, "round": round_no, "draft_slot": slot,
                "player_id": str(player_id), "picked_by": "user%d" % slot,
                "metadata": {},
            })

    def route(self, path):
        with self.lock:
            if path == "/v1/user/rtrestini2019":
                return {"user_id": USER_ID, "username": "rtrestini2019",
                        "display_name": "rtrestini2019"}
            if path == "/v1/user/%s/leagues/nfl/2026" % USER_ID:
                return [{
                    "league_id": LEAGUE_ID, "name": "Fantasy NFL 2026",
                    "total_rosters": 12, "season": "2026",
                    "settings": {"draft_rounds": config.ROUNDS},
                    "roster_positions": (["QB", "RB", "RB", "WR", "WR", "TE"]
                                         + ["FLEX"] * config.FLEX_SLOTS
                                         + ["K", "DEF"]
                                         + ["BN"] * config.BENCH),
                    "scoring_settings": {"rec": 1.0, "pass_td": 4.0, "pass_int": -1.0,
                                         "fum_lost": -2.0, "rec_td": 6.0,
                                         "rush_td": 6.0, "pass_yd": 0.04,
                                         "rush_yd": 0.1, "rec_yd": 0.1},
                }]
            if path == "/v1/league/%s/drafts" % LEAGUE_ID:
                return [self.draft_object()]
            if path == "/v1/league/%s/users" % LEAGUE_ID:
                users = [{"user_id": USER_ID, "display_name": "rtrestini2019"}]
                for i in range(2, 13):
                    users.append({"user_id": "user%d" % i, "display_name": "Manager %d" % i})
                return users
            if path.startswith("/v1/projections/nfl/"):
                # Weekly projections, in the shape the real endpoint returns.
                return [{"player_id": pid, "stats": stats}
                        for pid, stats in self.weekly.items()]
            if path == "/v1/state/nfl":
                return {"week": self.week, "season": "2026",
                        "season_type": "regular"}
            if path == "/v1/league/%s/rosters" % LEAGUE_ID:
                out = [{"roster_id": 1, "owner_id": USER_ID,
                        "players": list(self.my_players),
                        "starters": list(self.my_starters)}]
                for i in range(2, 13):
                    out.append({"roster_id": i, "owner_id": "user%d" % i,
                                "players": [], "starters": []})
                return out
            if path == "/v1/user/%s/drafts/nfl/2026" % USER_ID:
                # The league draft plus a standalone mock, exactly as Sleeper
                # reports them: a mock carries no league_id.
                return [self.draft_object(), self.mock_object()]
            if path == "/v1/draft/%s" % MOCK_DRAFT_ID:
                return self.mock_object()
            if path == "/v1/draft/%s/picks" % MOCK_DRAFT_ID:
                return list(self.mock_picks)
            if path == "/v1/draft/%s" % DRAFT_ID:
                return self.draft_object()
            if path == "/v1/draft/%s/picks" % DRAFT_ID:
                return list(self.picks)
            if path == "/v1/players/nfl":
                return self.players
        return None

    def add_pick(self, pick_no, round_no, slot, player_id, user_id=None):
        with self.lock:
            self.picks.append({
                "pick_no": pick_no, "round": round_no, "draft_slot": slot,
                "player_id": str(player_id), "picked_by": user_id or ("user%d" % slot),
                "metadata": {},
            })
