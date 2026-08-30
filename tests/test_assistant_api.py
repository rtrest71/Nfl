"""Tests for the surface an outside assistant talks to.

Two ways in - the /api/v1 HTTP endpoints and the MCP server - and they must
agree, because the whole point is that one team is being described.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assistant_api
import config
import fake_sleeper
import sleeper

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AssistantApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="assistantapi-")
        cls.original_api = sleeper.API
        cls.paths = {n: getattr(config, n) for n in
                     ("PLAYERS_CACHE", "PROJECTIONS_CACHE", "ADP_CACHE",
                      "LEAGUE_CACHE", "DURABILITY_CACHE")}
        for name, filename in (("PLAYERS_CACHE", "players.json"),
                               ("PROJECTIONS_CACHE", "projections.json"),
                               ("ADP_CACHE", "adp.json"),
                               ("LEAGUE_CACHE", "league.json"),
                               ("DURABILITY_CACHE", "durability.json")):
            setattr(config, name, os.path.join(cls.tmp, filename))

        raw, projections, adp = fake_sleeper.build_universe()
        cls.server = fake_sleeper.FakeSleeper(raw, status="complete")
        players = sleeper.slim_players(raw)
        cls.players, cls.adp = players, adp
        cls.by_adp = sorted(players.values(),
                            key=lambda p: adp.get(p["player_id"], 9999))

        def take(position, count, skip=0):
            at = [p for p in cls.by_adp if p["position"] == position]
            return at[skip:skip + count]

        # Deliberately not the best players available at every position: a
        # roster that cannot be improved makes "is this trade good?" untestable.
        roster = (take("QB", 1, skip=1) + take("RB", 4, skip=2)
                  + take("WR", 4, skip=2) + take("TE", 2, skip=1)
                  + take("K", 1) + take("DEF", 1))
        cls.roster = roster
        cls.server.my_players = [p["player_id"] for p in roster]
        cls.server.my_starters = [p["player_id"] for p in roster[:9]]
        cls.server.week = 5
        cls.server.weekly = {
            r["player_id"]: {k: (v / 17.0) if k != "pts_allow" else v
                             for k, v in r["stats"].items()}
            for r in projections if r["player_id"] in players}
        cls.server.start()
        sleeper.API = cls.server.base

        sleeper.cache_write(config.PLAYERS_CACHE, players)
        sleeper.cache_write(config.PROJECTIONS_CACHE, {
            "players": {r["player_id"]: {"stats": r["stats"]}
                        for r in projections if r["player_id"] in players}})
        sleeper.cache_write(config.LEAGUE_CACHE,
                            {"user": {"user_id": fake_sleeper.USER_ID}})

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        sleeper.API = cls.original_api
        for name, value in cls.paths.items():
            setattr(config, name, value)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        assistant_api.invalidate()
        self.server.transactions = {}

    def _unowned(self, position, count=1):
        owned = set(self.server.my_players)
        return [p for p in self.by_adp
                if p["position"] == position and p["player_id"] not in owned][:count]

    # -- the shape of the answers ------------------------------------------

    def test_lineup_is_complete_and_legal(self):
        out = assistant_api.get_lineup()
        self.assertTrue(out["ok"])
        self.assertEqual(out["week"], 5)
        self.assertEqual(len(out["starters"]),
                         sum(config.STARTERS.values()) + config.FLEX_SLOTS)
        self.assertEqual(out["unfilled_slots"], 0)
        slots = [p["slot"] for p in out["starters"]]
        for position, count in config.STARTERS.items():
            self.assertEqual(slots.count(position), count)
        self.assertEqual(slots.count("FLEX"), config.FLEX_SLOTS)

    def test_every_answer_carries_the_rules_that_change_it(self):
        """An assistant told nothing about the league gives generic advice."""
        for out in (assistant_api.get_lineup(), assistant_api.get_roster(),
                    assistant_api.get_offers()):
            rules = out["rules"]
            self.assertEqual(rules["teams"], config.TEAMS)
            self.assertEqual(rules["flex_slots"], config.FLEX_SLOTS)
            self.assertEqual(rules["passing_touchdown_points"],
                             config.SCORING["pass_td"])
            self.assertIn("PPR", rules["scoring"])

    def test_changes_name_what_to_do_in_sleeper(self):
        out = assistant_api.get_lineup()
        self.assertTrue(out["changes"])
        for change in out["changes"]:
            self.assertIn(change["action"], ("START", "BENCH"))
            self.assertTrue(change["name"])
        self.assertGreater(out["points_gained_by_changes"], 0)

    def test_a_player_who_cannot_play_is_never_a_starter(self):
        out = assistant_api.get_lineup()
        cannot = {p["player_id"] for p in out["cannot_play"]}
        starting = {p["player_id"] for p in out["starters"]}
        self.assertFalse(cannot & starting)

    def test_roster_is_scored_and_ordered(self):
        out = assistant_api.get_roster()
        self.assertEqual(out["roster_size"], len(self.roster))
        points = [p["projected_points"] for p in out["players"]]
        self.assertEqual(points, sorted(points, reverse=True))

    def test_the_response_is_json_serialisable(self):
        """It goes over a wire in both directions - it must survive the trip."""
        for out in (assistant_api.get_lineup(), assistant_api.get_roster(),
                    assistant_api.get_offers()):
            json.loads(json.dumps(out))

    # -- trades -------------------------------------------------------------

    def _best_unowned(self, position, count=1):
        """Highest-projecting available players - ADP order is not point order."""
        import team
        ctx = assistant_api.context()
        owned = {p["player_id"] for p in ctx["owned"]}
        scored = [team._score(p, ctx["projections"], ctx["scoring"])
                  for p in self.by_adp
                  if p["position"] == position and p["player_id"] not in owned]
        scored.sort(key=lambda p: p["points"], reverse=True)
        return scored[:count]

    def test_check_trade_scores_an_upgrade_positive(self):
        best = assistant_api.get_lineup()
        weakest = min((p for p in best["starters"] if p["position"] == "WR"),
                      key=lambda p: p["projected_points"])
        incoming = self._best_unowned("WR")[0]
        self.assertGreater(incoming["points"], weakest["projected_points"],
                           "fixture has no better receiver to trade for")
        out = assistant_api.check_trade([weakest["name"]], [incoming["name"]])
        self.assertTrue(out["ok"], out)
        self.assertGreater(out["points_change"], 0)
        self.assertEqual(len(out["lineup_after_trade"]),
                         sum(config.STARTERS.values()) + config.FLEX_SLOTS)

    def test_check_trade_refuses_to_guess_at_a_name_it_does_not_know(self):
        out = assistant_api.check_trade(["Zzzz Nobody"], ["Zzzz Nobody Two"])
        self.assertFalse(out["ok"])
        self.assertTrue(any("Could not find" in w for w in out["warnings"]))

    def test_check_trade_says_so_when_you_already_own_the_player(self):
        roster = assistant_api.get_roster()["players"]
        out = assistant_api.check_trade([roster[0]["name"]], [roster[1]["name"]])
        self.assertFalse(out["ok"])
        self.assertTrue(any("already own" in w for w in out["warnings"]))

    def test_a_bench_upgrade_scores_near_zero_not_positive(self):
        """The honest answer to 'this makes my bench better' is 'so what'."""
        lineup = assistant_api.get_lineup()
        benched = [p for p in lineup["bench"]
                   if p["projected_points"] and p["projected_points"] > 0]
        self.assertTrue(benched)
        worst = min(benched, key=lambda p: p["projected_points"])
        swap = [p for p in self._unowned(worst["position"], 30)
                if p["player_id"] not in
                {b["player_id"] for b in lineup["bench"]}]
        self.assertTrue(swap)
        out = assistant_api.check_trade([worst["name"]], [swap[-1]["name"]])
        if out["ok"]:
            self.assertLessEqual(abs(out["points_change"]), 3.0)

    def test_waiting_offers_are_reported_with_a_verdict(self):
        mine = [p for p in assistant_api.get_roster()["players"]
                if p["position"] == "WR"][-1]
        theirs = self._unowned("WR")[0]
        self.server.offer_trade(5, [mine["player_id"]], [theirs["player_id"]])
        assistant_api.invalidate()
        out = assistant_api.get_offers()
        self.assertEqual(out["count"], 1)
        offer = out["offers"][0]
        self.assertIn(offer["verdict"],
                      ["ACCEPT", "LEAN ACCEPT", "TOO CLOSE TO CALL",
                       "LEAN REJECT", "REJECT"])
        self.assertEqual(offer["you_send"][0]["name"], mine["name"])
        self.assertEqual(offer["you_receive"][0]["name"], theirs["name"])

    # -- the brief ----------------------------------------------------------

    def test_the_brief_stands_on_its_own(self):
        text = assistant_api.get_brief()
        for expected in ("FANTASY BRIEF", "LEAGUE RULES", "START THIS LINEUP",
                         "Passing touchdowns are worth"):
            self.assertIn(expected, text)
        lineup = assistant_api.get_lineup()
        for player in lineup["starters"]:
            self.assertIn(player["name"], text)

    def test_the_brief_reports_waiting_offers(self):
        mine = assistant_api.get_roster()["players"][-1]
        theirs = self._unowned("RB")[0]
        self.server.offer_trade(5, [mine["player_id"]], [theirs["player_id"]])
        assistant_api.invalidate()
        text = assistant_api.get_brief()
        self.assertIn("TRADE OFFERS WAITING IN SLEEPER", text)
        self.assertIn(theirs["name"], text)

    # -- caching ------------------------------------------------------------

    def test_repeated_questions_do_not_reread_sleeper_each_time(self):
        first = assistant_api.context()
        self.assertIs(assistant_api.context(), first)
        self.assertIsNot(assistant_api.context(fresh=True), first)

    # -- the MCP server -----------------------------------------------------

    # -- the HTTP surface ---------------------------------------------------

    def _unambiguous_owned(self):
        """Owned players whose name belongs to nobody else in the universe.

        The synthetic universe reuses names; a real trade request keyed on a
        name that two players share is a different problem from the one these
        tests are about.
        """
        counts = {}
        for player in self.players.values():
            counts[player["name"]] = counts.get(player["name"], 0) + 1
        return [p for p in assistant_api.get_roster()["players"]
                if counts.get(p["name"]) == 1]

    def _http(self):
        """The app's own server, on a free port, for the /api/v1 endpoints."""
        import app
        import threading
        import urllib.request

        assistant = app.Assistant()
        assistant.load_cached_data()
        assistant.resolve_league()
        assistant.poll_once()
        assistant.recompute()

        handler = type("H", (app.Handler,), {"assistant": assistant})
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]

        def get(path):
            with urllib.request.urlopen(base + path, timeout=30) as response:
                return response.status, response.read().decode()

        def post(path, payload):
            request = urllib.request.Request(
                base + path, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode()

        return server, get, post

    def test_http_v1_endpoints_answer(self):
        server, get, post = self._http()
        try:
            for path in ("/api/v1/lineup", "/api/v1/roster", "/api/v1/offers",
                         "/api/v1/rules"):
                status, raw = get(path)
                self.assertEqual(status, 200, path)
                self.assertTrue(json.loads(raw)["ok"], path)

            status, text = get("/api/v1/brief")
            self.assertEqual(status, 200)
            self.assertIn("FANTASY BRIEF", text)

            roster = self._unambiguous_owned()
            incoming = self._unowned("WR")[0]["name"]
            status, raw = post("/api/v1/trade",
                               {"give": [roster[-1]["name"]], "get": [incoming]})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)["ok"], raw)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_v1_accepts_names_as_a_comma_separated_string(self):
        """Because half the things that will call this send a string."""
        server, get, post = self._http()
        try:
            roster = self._unambiguous_owned()
            body = {"give": roster[-1]["name"],
                    "get": self._unowned("WR")[0]["name"]}
            out = json.loads(post("/api/v1/trade", body)[1])
            self.assertTrue(out["ok"], out)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_v1_names_its_endpoints_when_you_ask_for_a_wrong_one(self):
        import urllib.error
        server, get, _ = self._http()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                get("/api/v1/nonsense")
            payload = json.loads(caught.exception.read().decode())
            self.assertIn("/api/v1/lineup", payload["endpoints"])
        finally:
            server.shutdown()
            server.server_close()

    # The protocol is exercised in-process, against the same patched config
    # the rest of these tests use; a separate subprocess test proves the file
    # really runs as a server.

    def _mcp(self, requests):
        import mcp_server
        return [r for r in (mcp_server.handle(m) for m in requests)
                if r is not None]

    def test_mcp_handshake_and_tool_list(self):
        replies = self._mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        self.assertEqual(len(replies), 2)     # the notification is answered silently
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"],
                         "fantasy-assistant")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(sorted(names),
                         ["fantasy_brief", "fantasy_check_trade",
                          "fantasy_lineup", "fantasy_offers", "fantasy_roster"])
        for tool in replies[1]["result"]["tools"]:
            self.assertTrue(tool["description"].strip())
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_mcp_lineup_matches_the_python_answer(self):
        replies = self._mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "fantasy_lineup", "arguments": {}}}])
        result = replies[0]["result"]
        self.assertFalse(result["isError"], result)
        payload = json.loads(result["content"][0]["text"])
        mine = assistant_api.get_lineup()
        self.assertEqual(payload["projected_total"], mine["projected_total"])
        self.assertEqual([p["name"] for p in payload["starters"]],
                         [p["name"] for p in mine["starters"]])

    def test_mcp_brief_is_plain_text_not_json(self):
        replies = self._mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "fantasy_brief", "arguments": {}}}])
        text = replies[0]["result"]["content"][0]["text"]
        self.assertIn("FANTASY BRIEF", text)
        self.assertRaises(ValueError, json.loads, text)

    def test_mcp_reports_a_bad_trade_request_without_crashing(self):
        replies = self._mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "fantasy_check_trade",
                        "arguments": {"give": ["Zzzz Nobody"], "get": []}}}])
        payload = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertFalse(replies[0]["result"]["isError"])   # a real answer

    def test_mcp_rejects_an_unknown_method_and_an_unknown_tool(self):
        replies = self._mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "nonsense/method"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "fantasy_nonsense", "arguments": {}}}])
        self.assertEqual(replies[0]["error"]["code"], -32601)
        self.assertTrue(replies[1]["result"]["isError"])

    # -- the weekly nudge ---------------------------------------------------

    def test_the_nudge_speaks_up_when_there_is_something_to_do(self):
        import weekly_nudge
        payload, headline, attention = weekly_nudge.gather()
        self.assertTrue(attention, headline)
        self.assertTrue(payload["lineup"]["changes"])
        self.assertIn("Week 5", headline)
        self.assertIn("lineup change", headline)

    def test_the_nudge_counts_a_waiting_offer_as_news(self):
        import weekly_nudge
        mine = self._unambiguous_owned()[0]
        theirs = self._unowned("RB")[0]
        self.server.offer_trade(5, [mine["player_id"]], [theirs["player_id"]])
        assistant_api.invalidate()
        _, headline, attention = weekly_nudge.gather()
        self.assertTrue(attention)
        self.assertIn("trade offer", headline)

    def test_the_nudge_stays_quiet_when_the_lineup_is_already_right(self):
        """Silence is the feature. A notification every week is noise."""
        import weekly_nudge
        best = assistant_api.get_lineup()
        original = list(self.server.my_starters)
        self.server.my_starters = [p["player_id"] for p in best["starters"]]
        assistant_api.invalidate()
        try:
            payload, headline, attention = weekly_nudge.gather()
            if not payload["lineup"]["cannot_play"]:
                self.assertFalse(attention, headline)
                self.assertIn("nothing to change", headline)
        finally:
            self.server.my_starters = original
            assistant_api.invalidate()

    def test_the_nudge_writes_the_brief_where_anything_can_read_it(self):
        import weekly_nudge
        original = config.CACHE_DIR
        config.CACHE_DIR = os.path.join(self.tmp, "nudge-out")
        argv = sys.argv
        sys.argv = ["weekly_nudge.py", "--quiet"]
        try:
            status = weekly_nudge.main()
            self.assertIn(status, (0, 1))
            with open(os.path.join(config.CACHE_DIR, "brief.txt"),
                      encoding="utf-8") as handle:
                self.assertIn("FANTASY BRIEF", handle.read())
            with open(os.path.join(config.CACHE_DIR, "brief.json"),
                      encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["week"], 5)
            self.assertIn("needs_attention", payload)
        finally:
            sys.argv = argv
            config.CACHE_DIR = original

    def test_the_mcp_file_runs_as_a_real_server(self):
        """Started as a client would start it: a pipe, and one bad line."""
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "mcp_server.py")],
            input=('not json at all\n'
                   '{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
                   '{"jsonrpc":"2.0","id":10,"method":"tools/list"}\n'),
            capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l]
        self.assertEqual([r["id"] for r in lines], [9, 10])
        self.assertEqual(len(lines[1]["result"]["tools"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
