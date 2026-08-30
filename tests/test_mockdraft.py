"""End-to-end test: drive the real Assistant through a full 12-team mock draft.

This is the "test it with a mock draft before the real thing" requirement, run
against a fake Sleeper API so it needs no network. It exercises the actual code
path the app uses on draft day: poll picks, diff, recompute, recommend.
"""

import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import draftstate
import fake_sleeper
import sleeper
import valuation


class MockDraftTest(unittest.TestCase):
    MY_SLOT = 7

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="draft-test-")
        cls.original_api = sleeper.API
        cls.original_paths = {
            name: getattr(config, name) for name in
            ("CACHE_DIR", "PLAYERS_CACHE", "PROJECTIONS_CACHE", "ADP_CACHE",
             "LEAGUE_CACHE", "STATE_CACHE")
        }
        # Redirect every cache path so the test never touches the real cache.
        config.CACHE_DIR = cls.tmp
        for name, filename in (("PLAYERS_CACHE", "players.json"),
                               ("PROJECTIONS_CACHE", "projections.json"),
                               ("ADP_CACHE", "adp.json"),
                               ("LEAGUE_CACHE", "league.json"),
                               ("STATE_CACHE", "state.json")):
            setattr(config, name, os.path.join(cls.tmp, filename))

        raw_players, projection_rows, adp = fake_sleeper.build_universe()
        cls.raw_players = raw_players
        cls.server = fake_sleeper.FakeSleeper(raw_players).start()
        sleeper.API = cls.server.base

        players = sleeper.slim_players(raw_players)
        cls.players = players
        sleeper.cache_write(config.PLAYERS_CACHE, players)
        sleeper.cache_write(config.PROJECTIONS_CACHE, {
            "players": {r["player_id"]: {"stats": r["stats"], "source": "test"}
                        for r in projection_rows if r["player_id"] in players},
            "source": "test"})
        sleeper.cache_write(config.ADP_CACHE, {
            "players": {pid: {"adp": value} for pid, value in adp.items()
                        if pid in players},
            "byes": {}, "source": "test"})
        cls.adp = adp

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        sleeper.API = cls.original_api
        for name, value in cls.original_paths.items():
            setattr(config, name, value)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _assistant(self):
        import app
        assistant = app.Assistant()
        assistant.load_cached_data()
        assistant.resolve_league()
        assistant.poll_once()
        assistant.recompute()
        return assistant

    # -- pre-draft --------------------------------------------------------

    def test_01_boots_before_the_draft_order_exists(self):
        """Before the draft starts draft_order is absent. That is not an error."""
        self.server.draft_order = {}
        self.server.status = "pre_draft"
        assistant = self._assistant()

        snap = assistant.snapshot
        self.assertTrue(snap["ok"])
        self.assertFalse(snap["me"]["slot_known"])
        self.assertIsNone(snap["me"]["slot"])
        self.assertEqual(snap["league"]["name"], "Fantasy NFL 2026")
        self.assertEqual(snap["league"]["teams"], 12)
        self.assertEqual(snap["draft"]["current_pick"], 1)
        # It must still produce a board and a recommendation to look at.
        self.assertGreater(len(snap["pool"]), 100)
        self.assertIsNotNone(snap["recommendation"]["top"])

    def test_02_detects_slot_the_moment_the_order_lands(self):
        self.server.draft_order = {}
        self.server.status = "pre_draft"
        assistant = self._assistant()
        self.assertFalse(assistant.snapshot["me"]["slot_known"])

        # Sleeper randomises the order and flips status to drafting.
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        for i in range(2, 13):
            self.server.draft_order["user%d" % i] = i if i != self.MY_SLOT else 1
        self.server.status = "drafting"

        assistant.poll_once()
        assistant.recompute()
        snap = assistant.snapshot

        self.assertTrue(snap["me"]["slot_known"])
        self.assertEqual(snap["me"]["slot"], self.MY_SLOT)
        # Every pick number for all 15 rounds, recomputed immediately.
        self.assertEqual(len(snap["me"]["my_picks"]), 15)
        self.assertEqual(snap["me"]["my_picks"][0], self.MY_SLOT)
        self.assertEqual(snap["me"]["my_picks"][1], 24 - self.MY_SLOT + 1)
        self.assertEqual(snap["me"]["picks_until_me"], self.MY_SLOT - 1)

    def test_03_scoring_uses_the_league_table_not_a_ppr_column(self):
        assistant = self._assistant()
        import scoring
        board = assistant.board
        by_id = {p["player_id"]: p for p in board}
        for pid, record in list(assistant.projections.items())[:40]:
            if pid not in by_id:
                continue
            expected = scoring.fantasy_points(
                record["stats"], by_id[pid]["position"], assistant.scoring_settings)
            self.assertAlmostEqual(by_id[pid]["points"], expected, places=2)

        # A passing touchdown must be worth 4, not 6.
        qbs = [p for p in board if p["position"] == "QB"]
        self.assertTrue(qbs)
        top = max(qbs, key=lambda p: p["points"])
        stats = assistant.projections[top["player_id"]]["stats"]
        six_point = scoring.fantasy_points(
            stats, "QB", dict(config.SCORING, pass_td=6.0))
        self.assertGreater(six_point, top["points"])

    # -- the full draft ---------------------------------------------------

    def _opponent_pick(self, available, roster, round_no, rng):
        """A plausible opponent: best ADP that fills a need, kickers last."""
        needs = valuation.roster_needs(roster)
        pool = []
        for player in available:
            pos = player["position"]
            if pos in ("K", "DEF") and round_no < 13:
                continue
            if pos == "QB" and needs.get("QB", 0) <= 0:
                continue
            if pos in ("K", "DEF") and needs.get(pos, 0) <= 0:
                continue
            pool.append(player)
        if not pool:
            pool = available
        pool.sort(key=lambda p: self.adp.get(p["player_id"], 9999))
        # A little noise, because real drafters reach.
        window = pool[:max(1, min(6, len(pool)))]
        return rng.choice(window)

    def test_04_full_fifteen_round_draft(self):
        rng = random.Random(11)
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        for i in range(2, 13):
            self.server.draft_order["user%d" % i] = i if i != self.MY_SLOT else 1
        self.server.status = "drafting"

        assistant = self._assistant()
        self.assertEqual(assistant.snapshot["me"]["slot"], self.MY_SLOT)

        rosters = {slot: [] for slot in range(1, 13)}
        taken = set()
        my_picks_log = []

        for pick_no in range(1, 12 * 15 + 1):
            round_no, slot = draftstate.slot_of_pick(pick_no)
            snap = assistant.snapshot

            self.assertEqual(snap["draft"]["current_pick"], pick_no,
                             "app lost track at pick %d" % pick_no)
            self.assertEqual(snap["draft"]["round"], round_no)
            self.assertEqual(snap["draft"]["on_clock_slot"], slot)

            if slot == self.MY_SLOT:
                self.assertTrue(snap["me"]["on_the_clock"],
                                "app did not flag my turn at pick %d" % pick_no)
                rec = snap["recommendation"]["top"]
                self.assertIsNotNone(rec, "no recommendation at pick %d" % pick_no)
                player = rec["player"]

                self.assertNotIn(player["player_id"], taken,
                                 "recommended an already drafted player")
                self.assertTrue(rec["reason"].strip(),
                                "recommendation had no plain-English reason")

                # The league rules from spec section 5, enforced live.
                if player["position"] == "QB":
                    self.assertGreaterEqual(
                        round_no, config.QB_UNLOCK_ROUND,
                        "recommended a QB in round %d" % round_no)
                if player["position"] in ("K", "DEF"):
                    self.assertGreaterEqual(
                        round_no, config.K_UNLOCK_ROUND,
                        "recommended a %s in round %d"
                        % (player["position"], round_no))

                chosen = player["player_id"]
                my_picks_log.append((round_no, player["position"], player["name"]))
            else:
                available = [self.players[pid] for pid in self.players
                             if pid not in taken]
                chosen = self._opponent_pick(
                    available, rosters[slot], round_no, rng)["player_id"]

            taken.add(chosen)
            rosters[slot].append(self.players[chosen]["position"])
            self.server.add_pick(pick_no, round_no, slot, chosen)

            assistant.poll_once()
            assistant.recompute()

        self.my_roster = rosters[self.MY_SLOT]
        self.my_picks_log = my_picks_log
        self._assert_legal_roster(rosters[self.MY_SLOT], my_picks_log)

        final = assistant.snapshot
        self.assertEqual(final["draft"]["picks_made"], 180)
        self.assertEqual(len(final["roster_players"]), 15)

    def _assert_legal_roster(self, roster, log):
        counts = {}
        for pos in roster:
            counts[pos] = counts.get(pos, 0) + 1

        self.assertEqual(len(roster), 15, "roster is not 15 players: %s" % counts)

        # Every starting slot must be fillable.
        needs = valuation.roster_needs(roster)
        unfilled = {pos: n for pos, n in needs.items() if n > 0}
        self.assertFalse(unfilled,
                         "could not field a full lineup, missing %s (roster %s)\n%s"
                         % (unfilled, counts,
                            "\n".join("  R%-2d %-3s %s" % row for row in log)))

        # Exactly one QB, one K, one DEF - no wasted roster spots.
        self.assertEqual(counts.get("QB"), 1, "should draft exactly one QB")
        self.assertEqual(counts.get("K"), 1, "should draft exactly one kicker")
        self.assertEqual(counts.get("DEF"), 1, "should draft exactly one defense")

        # Full PPR with two flex slots: load up on pass catchers.
        catchers = counts.get("WR", 0) + counts.get("TE", 0) + counts.get("RB", 0)
        self.assertGreaterEqual(catchers, 12,
                                "too few RB/WR/TE for a 7-starter league: %s" % counts)
        self.assertGreaterEqual(counts.get("WR", 0), 4,
                                "full PPR league needs receivers: %s" % counts)

        qb_round = [r for r, pos, _ in log if pos == "QB"]
        self.assertTrue(qb_round and qb_round[0] >= config.QB_UNLOCK_ROUND,
                        "took a QB in round %s" % qb_round)
        for pos in ("K", "DEF"):
            rounds = [r for r, p, _ in log if p == pos]
            self.assertTrue(rounds and rounds[0] >= 14,
                            "took a %s in round %s" % (pos, rounds))

    # -- resilience -------------------------------------------------------

    def test_05_survives_the_picks_endpoint_dying(self):
        """If Sleeper stalls, the app must keep its state and stay usable."""
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        self.server.status = "drafting"
        assistant = self._assistant()

        for pick_no in range(1, 9):
            round_no, slot = draftstate.slot_of_pick(pick_no)
            pid = sorted(self.players, key=lambda p: self.adp.get(p, 9999))[pick_no - 1]
            self.server.add_pick(pick_no, round_no, slot, pid)
        assistant.poll_once()
        assistant.recompute()
        before = assistant.snapshot["draft"]["picks_made"]
        self.assertEqual(before, 8)

        # Kill the API underneath it.
        original = sleeper.API
        sleeper.API = "http://127.0.0.1:1/v1"
        try:
            for _ in range(3):
                assistant.poll_once()
            assistant.recompute()
        finally:
            sleeper.API = original

        snap = assistant.snapshot
        self.assertTrue(snap["ok"], "app broke when the feed died")
        self.assertEqual(snap["draft"]["picks_made"], before,
                         "app lost the picks it already had")
        self.assertFalse(snap["data"]["last_poll_ok"])
        self.assertGreaterEqual(snap["data"]["poll_failures"], 3)
        self.assertIsNotNone(snap["recommendation"]["top"])

    def test_06_manual_override_and_undo(self):
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        assistant = self._assistant()

        target = assistant.snapshot["recommendation"]["top"]["player"]
        ok, _ = assistant.mark_taken(target["player_id"])
        self.assertTrue(ok)

        after = assistant.snapshot["recommendation"]["top"]["player"]
        self.assertNotEqual(after["player_id"], target["player_id"],
                            "manually marked player was still recommended")

        ok, name = assistant.undo_manual()
        self.assertTrue(ok)
        self.assertEqual(name, target["name"])
        restored = assistant.snapshot["recommendation"]["top"]["player"]
        self.assertEqual(restored["player_id"], target["player_id"])

    def test_07_queue_export_is_legal_and_ordered(self):
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        assistant = self._assistant()
        queue = assistant.snapshot["queue"]

        self.assertGreaterEqual(len(queue), 20)
        ids = [p["player_id"] for p in queue]
        self.assertEqual(len(ids), len(set(ids)), "queue repeats a player")
        # Nothing already drafted, and no kickers near the top of a round-1 queue.
        taken = assistant.snapshot["pool"]
        drafted = {p["player_id"] for p in taken if p["drafted"]}
        self.assertFalse(set(ids) & drafted)
        self.assertNotIn("K", [p["position"] for p in queue[:10]])
        self.assertNotIn("DEF", [p["position"] for p in queue[:10]])

    def test_08_paste_fallback_replaces_projections_live(self):
        assistant = self._assistant()
        sample = [p for p in assistant.board if p["position"] == "WR"][:3]

        rows = ["Player,Team,POS,REC,REC YDS,REC TD"]
        for i, player in enumerate(sample):
            rows.append("%s,%s,WR,%d,%d,%d"
                        % (player["name"], player["team"], 120 - i, 1800 - i * 10, 15))
        result = assistant.load_projection_paste("\n".join(rows))

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["report"]["matched"], len(sample))

        board = {p["player_id"]: p for p in assistant.board}
        boosted = board[sample[0]["player_id"]]
        expected = 120 * 1.0 + 1800 * 0.1 + 15 * 6
        self.assertAlmostEqual(boosted["points"], expected, places=1)

    def test_09_simulation_runs_in_the_background(self):
        """The 500-run simulation must not block the live poll loop."""
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        self.server.status = "drafting"
        assistant = self._assistant()

        started = assistant.start_simulation(runs=100)
        self.assertTrue(started.get("ok"), started)
        self.assertGreaterEqual(started["candidates"], 1)

        # While it runs, the app must keep serving state.
        snapshot = assistant.snapshot
        self.assertTrue(snapshot["ok"])
        self.assertIn(snapshot["simulation"]["status"], ("running", "done"))

        deadline = time.time() + 60
        while time.time() < deadline:
            with assistant.lock:
                status = assistant.sim.get("status")
            if status in ("done", "error"):
                break
            assistant.poll_once()
            assistant.recompute()
            time.sleep(0.2)

        with assistant.lock:
            sim = dict(assistant.sim)
        self.assertEqual(sim.get("status"), "done", sim)
        self.assertEqual(sim["runs"], 100)
        self.assertTrue(sim["candidates"])
        for entry in sim["candidates"]:
            self.assertGreater(entry["mean"], 0)
            self.assertEqual(entry["incomplete_lineup_runs"], 0,
                             "simulated draft could not field a lineup")
        self.assertTrue(sim["summary"])

        assistant.recompute()
        self.assertEqual(assistant.snapshot["simulation"]["status"], "done")

    def test_10_simulation_refuses_without_a_slot(self):
        self.server.picks = []
        self.server.draft_order = {}
        self.server.status = "pre_draft"
        assistant = self._assistant()
        assistant.slot_override = None

        result = assistant.start_simulation(runs=20)
        self.assertFalse(result.get("ok"))
        self.assertIn("slot", result["error"].lower())

    def test_11_can_follow_a_sleeper_mock_draft(self):
        """A Sleeper mock draft is a separate draft with no league attached.

        Practising against one is the only way to rehearse before the real
        draft, so --draft-id must be able to follow a draft that is not one of
        the league's own.
        """
        import app

        # The league draft is dormant; the mock is live with a different slot.
        self.server.picks = []
        self.server.status = "pre_draft"
        self.server.draft_order = {}
        self.server.mock_picks = []
        self.server.mock_status = "drafting"
        self.server.mock_draft_order = {fake_sleeper.USER_ID: 4}

        assistant = app.Assistant(draft_id_override=fake_sleeper.MOCK_DRAFT_ID)
        assistant.load_cached_data()
        assistant.resolve_league()
        assistant.poll_once()
        assistant.recompute()

        snap = assistant.snapshot
        self.assertEqual(snap["league"]["draft_id"], fake_sleeper.MOCK_DRAFT_ID,
                         "app did not follow the mock draft")
        self.assertEqual(snap["me"]["slot"], 4,
                         "app did not read my slot from the mock draft")
        self.assertTrue(
            any("PRACTICE MODE" in w for w in snap["warnings"]),
            "app did not warn that it is following a practice draft")

        # Picks made in the mock must flow through exactly as real ones do.
        ordered = sorted(self.players, key=lambda p: self.adp.get(p, 9999))
        for pick_no in range(1, 4):
            round_no, slot = draftstate.slot_of_pick(pick_no)
            self.server.add_mock_pick(pick_no, round_no, slot, ordered[pick_no - 1])
        assistant.poll_once()
        assistant.recompute()

        snap = assistant.snapshot
        self.assertEqual(snap["draft"]["picks_made"], 3)
        self.assertEqual(snap["draft"]["current_pick"], 4)
        drafted = {p["player_id"] for p in snap["pool"] if p["drafted"]}
        self.assertEqual(drafted, set(ordered[:3]),
                         "mock draft picks did not remove players from the pool")
        self.assertTrue(snap["me"]["on_the_clock"], "slot 4 should be on the clock")

    def test_12_finds_mock_drafts_on_the_account(self):
        drafts = sleeper.get_user_drafts(fake_sleeper.USER_ID)
        self.assertEqual(len(drafts), 2)
        league_ids = {fake_sleeper.LEAGUE_ID}
        mocks = [d for d in drafts if sleeper.draft_is_mock(d, league_ids)]
        self.assertEqual(len(mocks), 1)
        self.assertEqual(mocks[0]["draft_id"], fake_sleeper.MOCK_DRAFT_ID)
        # The league's own draft must never be mistaken for a mock.
        real = [d for d in drafts if not sleeper.draft_is_mock(d, league_ids)]
        self.assertEqual(real[0]["draft_id"], fake_sleeper.DRAFT_ID)

    def test_13_practice_mode_runs_a_whole_draft(self):
        """Rehearsal must not depend on Sleeper's mock lobby being reachable."""
        import app
        import practice as practice_mode

        prac = practice_mode.PracticeDraft(
            my_slot=5, seconds_per_pick=0.0, seed=3)
        assistant = app.Assistant(practice=prac)
        assistant.load_cached_data()
        assistant.resolve_league()
        assistant.poll_once()
        assistant.recompute()

        self.assertTrue(assistant.snapshot["practice"]["active"])
        self.assertEqual(assistant.snapshot["me"]["slot"], 5)
        self.assertTrue(any("PRACTICE" in w.upper()
                            for w in assistant.snapshot["warnings"]),
                        "practice mode must be impossible to mistake for real")

        my_rounds = []
        for _ in range(600):
            assistant.poll_once()
            assistant.recompute()
            snap = assistant.snapshot
            if snap["practice"]["over"]:
                break
            if snap["practice"]["my_turn"]:
                top = snap["recommendation"]["top"]
                self.assertIsNotNone(top, "no recommendation on my practice turn")
                result = assistant.practice_draft(top["player"]["player_id"])
                self.assertTrue(result["ok"], result)
                my_rounds.append((snap["draft"]["round"],
                                  top["player"]["position"]))

        snap = assistant.snapshot
        self.assertEqual(snap["draft"]["picks_made"], 180)
        self.assertEqual(len(snap["roster_players"]), 15)
        empty = [s["slot"] for s in snap["roster"]["slots"]
                 if not s["filled"] and s["slot"] != "BN"]
        self.assertFalse(empty, "practice draft left starting slots empty: %s" % empty)

        # The league rules must hold in rehearsal exactly as they do live.
        for round_no, position in my_rounds:
            if position == "QB":
                self.assertGreaterEqual(round_no, config.QB_UNLOCK_ROUND)
            if position in ("K", "DEF"):
                self.assertGreaterEqual(round_no, config.K_UNLOCK_ROUND)

    def test_14_practice_rejects_drafting_out_of_turn(self):
        import app
        import practice as practice_mode

        prac = practice_mode.PracticeDraft(
            my_slot=12, seconds_per_pick=999, seed=1)
        assistant = app.Assistant(practice=prac)
        assistant.load_cached_data()
        assistant.resolve_league()
        assistant.poll_once()
        assistant.recompute()

        # Slot 12 does not pick first, so this must be refused.
        target = assistant.snapshot["pool"][0]["player_id"]
        result = assistant.practice_draft(target)
        self.assertFalse(result["ok"])
        self.assertIn("turn", result["detail"])

    def test_15_practice_can_be_driven_entirely_from_the_browser(self):
        """No terminal during the draft: start and stop practice from the page."""
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        self.server.status = "drafting"
        assistant = self._assistant()

        self.assertFalse(assistant.snapshot["practice"]["active"])
        live_draft_id = assistant.snapshot["league"]["draft_id"]

        started = assistant.start_practice(slot=9, speed=0.0)
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["slot"], 9)
        self.assertTrue(assistant.snapshot["practice"]["active"])
        self.assertEqual(assistant.snapshot["me"]["slot"], 9)

        # Starting twice must not silently create a second draft.
        self.assertFalse(assistant.start_practice()["ok"])

        for _ in range(600):
            assistant.poll_once()
            assistant.recompute()
            snap = assistant.snapshot
            if snap["practice"]["over"]:
                break
            if snap["practice"]["my_turn"]:
                assistant.practice_draft(
                    snap["recommendation"]["top"]["player"]["player_id"])
        self.assertEqual(assistant.snapshot["draft"]["picks_made"], 180)
        self.assertEqual(len(assistant.snapshot["roster_players"]), 15)

        stopped = assistant.stop_practice()
        self.assertTrue(stopped["ok"], stopped)
        snap = assistant.snapshot
        self.assertFalse(snap["practice"]["active"])
        self.assertEqual(snap["league"]["draft_id"], live_draft_id,
                         "did not return to the real draft")
        self.assertEqual(snap["draft"]["picks_made"], 0,
                         "practice picks leaked into the live draft")
        self.assertFalse(any("PRACTICE" in w for w in snap["warnings"]),
                         "practice banner survived the switch back to live")
        self.assertFalse(assistant.stop_practice()["ok"])

    def test_16_follows_any_sleeper_draft_from_the_browser(self):
        """The real workflow: pick in Sleeper, watch it appear here.

        Nothing is clicked in the web app - every pick, mine included, arrives
        from Sleeper exactly as it will during the live draft.
        """
        self.server.picks = []
        self.server.draft_order = {fake_sleeper.USER_ID: self.MY_SLOT}
        self.server.mock_picks = []
        self.server.mock_status = "drafting"
        self.server.mock_draft_order = {fake_sleeper.USER_ID: 5}
        assistant = self._assistant()
        league_draft = assistant.snapshot["league"]["draft_id"]

        # A pasted draft address, not just a bare id.
        result = assistant.follow_draft(
            "https://sleeper.com/draft/nfl/%s" % fake_sleeper.MOCK_DRAFT_ID)
        self.assertTrue(result["ok"], result)
        self.assertEqual(assistant.snapshot["league"]["draft_id"],
                         fake_sleeper.MOCK_DRAFT_ID)
        self.assertEqual(assistant.snapshot["me"]["slot"], 5)

        ordered = sorted(self.players, key=lambda p: self.adp.get(p, 9999))
        for pick_no in range(1, 9):
            round_no, slot = draftstate.slot_of_pick(pick_no)
            self.server.add_mock_pick(pick_no, round_no, slot, ordered[pick_no - 1])
        assistant.poll_once()
        assistant.recompute()

        snap = assistant.snapshot
        self.assertEqual(snap["draft"]["picks_made"], 8)
        self.assertEqual(len([p for p in snap["pool"] if p["drafted"]]), 8,
                         "picks made in Sleeper did not leave the pool")
        # Slot 5 picked at 5; that player must be on my roster without any
        # clicking in the web app.
        self.assertEqual([p["player_id"] for p in snap["roster_players"]],
                         [ordered[4]])

        back = assistant.follow_draft(None)
        self.assertTrue(back["ok"])
        self.assertEqual(assistant.snapshot["league"]["draft_id"], league_draft)
        self.assertEqual(assistant.snapshot["draft"]["picks_made"], 0)

    def test_17_rejects_a_draft_id_sleeper_does_not_have(self):
        assistant = self._assistant()
        result = assistant.follow_draft("999999999999")
        self.assertFalse(result["ok"])
        self.assertIn("999999999999", result["error"])

    def test_18_http_layer_serves_state_and_page(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import app

        assistant = self._assistant()
        app.Handler.assistant = assistant
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/state")
            body = conn.getresponse().read()
            snap = json.loads(body)
            self.assertTrue(snap["ok"])
            self.assertIn("recommendation", snap)

            conn.request("GET", "/")
            page = conn.getresponse()
            self.assertEqual(page.status, 200)
            html = page.read().decode()
            self.assertIn("YOUR PICK IN", html.upper())

            conn.request("POST", "/api/slot", json.dumps({"slot": 3}),
                         {"Content-Type": "application/json"})
            self.assertTrue(json.loads(conn.getresponse().read())["ok"])
            self.assertEqual(assistant.snapshot["me"]["slot"], 3)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
