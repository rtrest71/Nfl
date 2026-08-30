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


if __name__ == "__main__":
    unittest.main(verbosity=2)
