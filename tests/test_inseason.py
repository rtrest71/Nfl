"""Tests for the in-season tools: weekly lineup and trade evaluation."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import fake_sleeper
import scoring
import simulation
import sleeper
import team


class InSeasonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="inseason-")
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
        cls.server = fake_sleeper.FakeSleeper(raw)
        players = sleeper.slim_players(raw)
        cls.players = players
        cls.adp = adp

        by_adp = sorted(players.values(),
                        key=lambda p: adp.get(p["player_id"], 9999))
        cls.by_adp = by_adp

        def take(position, count):
            return [p for p in by_adp if p["position"] == position][:count]

        cls.take = staticmethod(take)
        roster = (take("QB", 1) + take("RB", 4) + take("WR", 4)
                  + take("TE", 2) + take("K", 1) + take("DEF", 1))
        cls.roster = roster
        cls.server.my_players = [p["player_id"] for p in roster]
        cls.server.my_starters = [p["player_id"] for p in roster[:9]]
        cls.server.week = 5
        # Serve real weekly projections, so the tests exercise the path the
        # tools actually prefer rather than only the season-average fallback.
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

    def context(self):
        return team.load_context(quiet=True)

    # -- roster and scoring ------------------------------------------------

    def test_reads_my_live_roster(self):
        ctx = self.context()
        self.assertEqual(ctx["week"], 5)
        self.assertEqual(len(ctx["owned"]), len(self.roster))
        self.assertTrue(all(p["points"] >= 0 for p in ctx["owned"]))

    def test_defence_is_scored_per_game_not_per_season(self):
        """Regression: dividing points-allowed by 17 put every defence in the
        best bucket and then scored it as a full season - 127 points a week."""
        ctx = self.context()
        defences = [p for p in ctx["owned"] if p["position"] == "DEF"]
        self.assertTrue(defences)
        for defence in defences:
            self.assertLess(defence["points"], 30,
                            "%s projected %.1f for one week"
                            % (defence["name"], defence["points"]))
            self.assertGreater(defence["points"], 0)

    def test_a_weekly_total_is_plausible(self):
        best = team.best_lineup(self.context()["owned"])
        self.assertGreater(best["total"], 60)
        self.assertLess(best["total"], 300)

    # -- lineup ------------------------------------------------------------

    def test_lineup_fills_every_slot_legally(self):
        best = team.best_lineup(self.context()["owned"])
        self.assertEqual(best["unfilled"], 0)
        slots = [p["slot"] for p in best["starters"]]
        for position, count in config.STARTERS.items():
            self.assertEqual(slots.count(position), count,
                             "wrong number of %s slots: %s" % (position, slots))
        self.assertEqual(slots.count("FLEX"), config.FLEX_SLOTS)

    def test_flex_is_only_ever_rb_wr_or_te(self):
        best = team.best_lineup(self.context()["owned"])
        for player in best["starters"]:
            if player["slot"] == "FLEX":
                self.assertIn(player["position"], config.FLEX_ELIGIBLE)

    def test_no_player_starts_twice(self):
        best = team.best_lineup(self.context()["owned"])
        ids = [p["player_id"] for p in best["starters"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_lineup_is_actually_optimal(self):
        """No bench player may outscore a starter he could legally replace."""
        best = team.best_lineup(self.context()["owned"])
        starters = {p["slot"]: p for p in best["starters"]}
        for benched in best["bench"]:
            ok, _ = team.playable(benched)
            if not ok:
                continue
            same = starters.get(benched["position"])
            if same:
                self.assertGreaterEqual(
                    same["points"], benched["points"],
                    "%s (%.1f) is benched behind %s (%.1f)"
                    % (benched["name"], benched["points"],
                       same["name"], same["points"]))

    def test_players_who_cannot_play_are_never_started(self):
        ctx = self.context()
        hurt = dict(ctx["owned"][0], player_id="hurt", name="Hurt Star",
                    injury_status="IR", points=999.0)
        best = team.best_lineup(ctx["owned"] + [hurt])
        self.assertNotIn("hurt", [p["player_id"] for p in best["starters"]])
        self.assertIn("hurt", [p["player_id"] for p, _ in best["unavailable"]])

    # -- trade -------------------------------------------------------------

    def _swap(self, give, get):
        ctx = self.context()
        before = team.best_lineup(ctx["owned"])
        give_ids = {p["player_id"] for p in give}
        after_roster = [p for p in ctx["owned"]
                        if p["player_id"] not in give_ids] + get
        after = team.best_lineup(after_roster)
        return after["total"] - before["total"], before, after

    def test_bench_for_bench_does_not_move_the_lineup(self):
        ctx = self.context()
        before = team.best_lineup(ctx["owned"])
        benched = [p for p in before["bench"] if team.playable(p)[0]]
        self.assertTrue(benched)
        weaker = dict(benched[0], player_id="weakling", name="Weak Sub",
                      points=max(0.0, benched[0]["points"] - 1.0))
        delta, _, _ = self._swap([benched[0]], [weaker])
        self.assertAlmostEqual(delta, 0.0, places=1,
                               msg="a bench-for-bench swap changed the lineup")

    def test_upgrading_a_starter_is_scored_positive(self):
        ctx = self.context()
        before = team.best_lineup(ctx["owned"])
        starter = next(p for p in before["starters"] if p["position"] == "WR")
        better = dict(starter, player_id="better_wr", name="Better WR",
                      points=starter["points"] + 12.0)
        delta, _, _ = self._swap([starter], [better])
        self.assertAlmostEqual(delta, 12.0, places=1)

    def test_downgrading_a_starter_is_scored_negative(self):
        ctx = self.context()
        before = team.best_lineup(ctx["owned"])
        starter = next(p for p in before["starters"] if p["position"] == "RB")
        worse = dict(starter, player_id="worse_rb", name="Worse RB",
                     points=1.0)
        delta, _, _ = self._swap([starter], [worse])
        self.assertLess(delta, 0)

    def test_receiving_an_injured_star_gains_nothing(self):
        """A trade for someone who cannot play is worth zero, not a windfall."""
        ctx = self.context()
        before = team.best_lineup(ctx["owned"])
        benched = [p for p in before["bench"] if team.playable(p)[0]][0]
        star = dict(benched, player_id="ir_star", name="IR Star",
                    points=400.0, injury_status="IR")
        delta, _, _ = self._swap([benched], [star])
        self.assertLessEqual(delta, 0.0,
                             "an injured player made the lineup better")

    def test_name_lookup_finds_players_on_and_off_my_roster(self):
        ctx = self.context()
        mine = ctx["owned"][0]["name"]
        owned_ids = {p["player_id"] for p in ctx["owned"]}
        other = next(p for p in self.by_adp
                     if p["player_id"] not in owned_ids)["name"]
        found, missing = team.resolve_names([mine, other], ctx)
        self.assertEqual(len(found), 2)
        self.assertEqual(missing, [])

    def test_unknown_names_are_reported_not_guessed(self):
        ctx = self.context()
        _, missing = team.resolve_names(["Zzzz Notaplayer"], ctx)
        self.assertEqual(missing, ["Zzzz Notaplayer"])

    # -- the app in season --------------------------------------------------

    def _assistant(self, draft_status="complete"):
        import app
        self.server.status = draft_status
        assistant = app.Assistant()
        assistant.load_cached_data()
        assistant.resolve_league()
        assistant.poll_once()
        assistant.recompute()
        return assistant

    def test_season_mode_switches_on_only_when_the_draft_is_done(self):
        try:
            self.assertFalse(self._assistant("drafting").season_mode())
            self.assertTrue(self._assistant("complete").season_mode())
        finally:
            self.server.status = "complete"

    def test_the_app_surfaces_the_weekly_lineup(self):
        assistant = self._assistant()
        season = assistant.refresh_season(force=True)
        self.assertEqual(season["status"], "ok", season)
        self.assertEqual(season["week"], 5)
        self.assertGreater(season["total"], 0)
        self.assertEqual(
            len(season["starters"]),
            sum(config.STARTERS.values()) + config.FLEX_SLOTS)

        assistant.recompute()
        snap = assistant.snapshot
        self.assertTrue(snap["season_mode"])
        self.assertEqual(snap["season"]["status"], "ok")

    def test_it_names_the_swaps_when_the_sleeper_lineup_is_wrong(self):
        assistant = self._assistant()
        season = assistant.refresh_season(force=True)
        # The fixture deliberately starts the first nine by ADP, which is not
        # the best legal lineup, so there must be something to change.
        self.assertTrue(season["changes"])
        actions = {c["action"] for c in season["changes"]}
        self.assertTrue(actions <= {"START", "BENCH"})
        self.assertGreater(season["gain"], 0)

    def test_trade_endpoint_scores_by_lineup_not_by_names(self):
        assistant = self._assistant()
        ctx = self.context()
        owned_ids = {p["player_id"] for p in ctx["owned"]}
        mine = ctx["owned"][0]["name"]
        theirs = next(p for p in self.by_adp
                      if p["player_id"] not in owned_ids)["name"]

        result = assistant.evaluate_trade([mine], [theirs])
        self.assertTrue(result["ok"], result)
        self.assertIn(result["verdict"],
                      ["ACCEPT", "LEAN ACCEPT", "TOO CLOSE TO CALL",
                       "LEAN REJECT", "REJECT"])
        self.assertAlmostEqual(result["after"] - result["before"],
                               result["delta"], places=1)

    def test_trade_endpoint_refuses_a_one_sided_request(self):
        assistant = self._assistant()
        result = assistant.evaluate_trade(["Zzzz Nobody"], ["Zzzz Nobody Two"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["notes"])

    # -- pending offers ----------------------------------------------------

    def _offer(self, give, get, **kwargs):
        """Queue an offer on the fake server; clear it again afterwards."""
        self.server.transactions = {}
        self.server.offer_trade(self.server.week,
                                [p["player_id"] for p in give],
                                [p["player_id"] for p in get], **kwargs)

    def _unowned(self, position, count=1):
        owned = {p["player_id"] for p in self.context()["owned"]}
        return [p for p in self.by_adp
                if p["position"] == position and p["player_id"] not in owned][:count]

    def test_a_pending_offer_is_found_and_scored(self):
        ctx = self.context()
        mine = [p for p in ctx["owned"] if p["position"] == "WR"][-1]
        theirs = self._unowned("WR")
        self._offer([mine], theirs)
        try:
            offers = team.pending_trades(self.context())
            self.assertEqual(len(offers), 1)
            offer = offers[0]
            self.assertEqual([p["player_id"] for p in offer["giving"]],
                             [mine["player_id"]])
            self.assertEqual([p["player_id"] for p in offer["getting"]],
                             [p["player_id"] for p in theirs])
            self.assertAlmostEqual(offer["after"] - offer["before"],
                                   offer["delta"], places=1)
        finally:
            self.server.transactions = {}

    def test_an_offer_that_upgrades_a_starter_says_accept(self):
        ctx = self.context()
        before = team.best_lineup(ctx["owned"])
        weakest = min((p for p in before["starters"] if p["position"] == "WR"),
                      key=lambda p: p["points"])
        best_wr = self._unowned("WR")[0]
        self._offer([weakest], [best_wr])
        try:
            offer = team.pending_trades(self.context())[0]
            if offer["delta"] >= 8:
                self.assertEqual(offer["verdict"], "ACCEPT")
            self.assertIn(offer["verdict"],
                          ["ACCEPT", "LEAN ACCEPT", "TOO CLOSE TO CALL"])
        finally:
            self.server.transactions = {}

    def test_an_offer_that_guts_the_lineup_says_reject(self):
        # Every running back for one replaceable kicker: the lineup loses two
        # starters and a flex, so there is no reading under which this is close.
        ctx = self.context()
        backs = [p for p in ctx["owned"] if p["position"] == "RB"]
        self.assertGreater(len(backs), 2)
        self._offer(backs, self._unowned("K", 40)[-1:])
        try:
            offer = team.pending_trades(self.context())[0]
            self.assertLess(offer["delta"], 0)
            self.assertIn(offer["verdict"], ["REJECT", "LEAN REJECT"])
            self.assertEqual(offer["tone"], "bad")
        finally:
            self.server.transactions = {}

    def test_a_completed_trade_is_not_reported_as_waiting(self):
        ctx = self.context()
        mine = ctx["owned"][-1]
        self._offer([mine], self._unowned("WR"), status="complete")
        try:
            self.assertEqual(team.pending_trades(self.context()), [])
        finally:
            self.server.transactions = {}

    def test_someone_elses_trade_is_ignored(self):
        ctx = self.context()
        self.server.transactions = {}
        # Rosters 2 and 3 trading with each other has nothing to do with me.
        self.server.offer_trade(self.server.week,
                                [ctx["owned"][0]["player_id"]],
                                [self._unowned("RB")[0]["player_id"]],
                                roster_id=2, other=3)
        try:
            self.assertEqual(team.pending_trades(self.context()), [])
        finally:
            self.server.transactions = {}

    def test_the_same_offer_is_not_reported_twice_across_weeks(self):
        """An offer open in two polled weeks is one offer, not two."""
        ctx = self.context()
        mine = ctx["owned"][-1]
        theirs = self._unowned("WR")
        self.server.transactions = {}
        for week in (self.server.week - 1, self.server.week):
            self.server.offer_trade(week, [mine["player_id"]],
                                    [p["player_id"] for p in theirs],
                                    transaction_id="same-offer")
        try:
            self.assertEqual(len(team.pending_trades(self.context())), 1)
        finally:
            self.server.transactions = {}

    def test_the_app_surfaces_waiting_offers_in_state_and_brief(self):
        ctx = self.context()
        mine = [p for p in ctx["owned"] if p["position"] == "WR"][-1]
        theirs = self._unowned("WR")
        self._offer([mine], theirs)
        try:
            assistant = self._assistant()
            season = assistant.refresh_season(force=True)
            self.assertEqual(len(season["offers"]), 1)

            assistant.recompute()
            self.assertEqual(len(assistant.snapshot["season"]["offers"]), 1)

            brief = assistant.weekly_brief()
            self.assertIn("TRADE OFFERS WAITING IN SLEEPER", brief)
            self.assertIn(mine["name"], brief)
            self.assertIn(theirs[0]["name"], brief)
        finally:
            self.server.transactions = {}

    def test_no_offers_means_no_trade_section_in_the_brief(self):
        self.server.transactions = {}
        assistant = self._assistant()
        assistant.refresh_season(force=True)
        self.assertEqual(assistant.season["offers"], [])
        self.assertNotIn("TRADE OFFERS WAITING", assistant.weekly_brief())

    def test_a_transactions_outage_still_leaves_a_lineup(self):
        """Losing the trade feed must not cost you the thing you came for."""
        original = sleeper.get_transactions
        sleeper.get_transactions = lambda *a, **k: (_ for _ in ()).throw(
            sleeper.SleeperError("down"))
        try:
            assistant = self._assistant()
            season = assistant.refresh_season(force=True)
            self.assertEqual(season["status"], "ok")
            self.assertEqual(season["offers"], [])
            self.assertGreater(season["total"], 0)
        finally:
            sleeper.get_transactions = original

    def test_trade_endpoint_ignores_a_player_i_already_own(self):
        assistant = self._assistant()
        ctx = self.context()
        mine = ctx["owned"][0]["name"]
        other_mine = ctx["owned"][1]["name"]
        result = assistant.evaluate_trade([mine], [other_mine])
        self.assertFalse(result["ok"])
        self.assertTrue(any("already own" in n for n in result["notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
