"""Tests for the forward mock-draft simulation.

The simulation is only useful if it (a) obeys the same league rules the live
engine does, (b) actually discriminates between good and bad picks, and
(c) finishes fast enough to run while you are on the clock.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import simulation
import valuation


def player(pid, position, points, adp, vor=None, depth=1):
    return {
        "player_id": pid, "name": "P%s" % pid, "position": position,
        "team": "XXX", "points": points, "adj_vor": points if vor is None else vor,
        "vor": points if vor is None else vor, "adp": adp,
        "adp_stdev": max(4.0, adp * 0.2), "depth_chart_order": depth,
    }


def synthetic_board():
    """A small but complete universe: enough of every position to draft 12 teams."""
    board = []
    pid = 0
    shape = {"QB": (30, 330, 6.0), "RB": (60, 300, 4.5), "WR": (80, 290, 3.5),
             "TE": (30, 250, 6.5), "K": (20, 150, 2.0), "DEF": (20, 160, 2.5)}
    for position, (count, top, decay) in shape.items():
        for i in range(count):
            pid += 1
            points = top - i * decay
            # ADP roughly tracks value, with kickers and defenses pushed late.
            base = i * 2.2 + (150 if position in ("K", "DEF") else 0)
            base += {"QB": 40, "TE": 25}.get(position, 0)
            board.append(player(str(pid), position, points, max(1.0, base)))
    board.sort(key=lambda p: p["adj_vor"], reverse=True)
    return board


SHAPE = {"teams": 12, "rounds": 15, "type": "snake", "reversal_round": 0}


class TestOptimalLineup(unittest.TestCase):
    def test_fills_every_slot_and_flexes_the_best_leftovers(self):
        roster = [
            player("q", "QB", 300, 40), player("r1", "RB", 250, 5),
            player("r2", "RB", 200, 15), player("r3", "RB", 180, 25),
            player("w1", "WR", 240, 8), player("w2", "WR", 220, 18),
            player("w3", "WR", 210, 28), player("t", "TE", 190, 45),
            player("k", "K", 140, 160), player("d", "DEF", 150, 165),
        ]
        total, chosen, unfilled = simulation.optimal_lineup(roster)
        self.assertEqual(unfilled, 0)
        self.assertEqual(len(chosen), 10)
        # Flex takes the two best leftovers: WR3 (210) and RB3 (180).
        expected = 300 + 250 + 200 + 240 + 220 + 190 + 140 + 150 + 210 + 180
        self.assertAlmostEqual(total, expected, places=1)

    def test_reports_unfilled_slots(self):
        roster = [player("r1", "RB", 250, 5), player("r2", "RB", 200, 15)]
        total, _, unfilled = simulation.optimal_lineup(roster)
        # QB, 2 WR, TE, K, DEF and 2 flex are all empty.
        self.assertEqual(unfilled, 8)
        self.assertAlmostEqual(total, 450.0, places=1)

    def test_bench_players_contribute_nothing(self):
        base = [player("q", "QB", 300, 40), player("r1", "RB", 250, 5),
                player("r2", "RB", 200, 15), player("w1", "WR", 240, 8),
                player("w2", "WR", 220, 18), player("t", "TE", 190, 45),
                player("k", "K", 140, 160), player("d", "DEF", 150, 165),
                player("f1", "WR", 210, 28), player("f2", "RB", 180, 25)]
        before, _, _ = simulation.optimal_lineup(base)
        after, _, _ = simulation.optimal_lineup(base + [player("x", "WR", 50, 200)])
        self.assertAlmostEqual(before, after, places=1,
                               msg="a bench player changed the lineup total")


class TestSimulationRules(unittest.TestCase):
    """The simulated you must obey the same rules as the live engine."""

    def setUp(self):
        self.board = synthetic_board()
        self.state = {"taken": set(), "current_pick": 1}

    def _one_roster(self, slot=7, seed=1):
        ctx = simulation.Context(self.board, self.state, SHAPE, slot, {}, [])
        captured = {}
        original = simulation.optimal_lineup

        def spy(players):
            captured["roster"] = list(players)
            return original(players)

        simulation.optimal_lineup = spy
        try:
            simulation._run_once(ctx, None, seed)
        finally:
            simulation.optimal_lineup = original
        return captured["roster"]

    def test_drafts_a_full_fifteen_and_never_forfeits_a_pick(self):
        for slot in (1, 5, 12):
            roster = self._one_roster(slot=slot)
            self.assertEqual(len(roster), 15,
                             "slot %d drafted %d players" % (slot, len(roster)))

    def test_always_fields_a_legal_lineup(self):
        for seed in range(1, 12):
            roster = self._one_roster(seed=seed)
            _, _, unfilled = simulation.optimal_lineup(roster)
            self.assertEqual(unfilled, 0,
                             "seed %d could not field a lineup: %s"
                             % (seed, [p["position"] for p in roster]))

    def test_respects_one_quarterback_one_kicker_one_defense(self):
        roster = self._one_roster()
        counts = {}
        for p in roster:
            counts[p["position"]] = counts.get(p["position"], 0) + 1
        self.assertEqual(counts.get("QB"), 1)
        self.assertEqual(counts.get("K"), 1)
        self.assertEqual(counts.get("DEF"), 1)

    def test_considers_every_position_not_just_the_top_of_the_board(self):
        """Regression: scanning one value-ordered list hid whole positions."""
        roster = self._one_roster()
        positions = {p["position"] for p in roster}
        self.assertIn("WR", positions, "simulation drafted no receivers at all")
        self.assertIn("RB", positions)

    def test_simulated_rules_agree_with_the_live_engine(self):
        """The sim's hard blocks must match valuation.eligible, not drift."""
        ctx = simulation.Context(self.board, self.state, SHAPE, 7, {}, [])
        checks = [
            ("QB", 3, {}, False), ("QB", 8, {}, True),
            ("QB", 10, {"QB": 1}, False),
            ("K", 10, {}, False), ("K", 14, {}, True),
            ("DEF", 13, {}, False), ("DEF", 14, {}, True),
            ("RB", 5, {}, True),
        ]
        for position, round_no, counts, expected in checks:
            index = next(i for i in range(ctx.size) if ctx.position[i] == position)
            sim_says = simulation._my_allowed(
                ctx, index, round_no, counts, picks_left=15,
                needs_left=simulation._needs_left(counts))

            roster = []
            for pos, n in counts.items():
                roster.extend([pos] * n)
            state = {
                "taken": set(), "round": round_no, "current_pick": 1,
                "my_roster": roster, "needs": valuation.roster_needs(roster),
                "picks_left": 15, "opponent_needs": {},
                "my_next_pick": None, "my_remaining_picks": [],
            }
            candidate = dict(self.board[0])
            candidate.update({
                "player_id": ctx.pid[index], "position": position,
                "adp": ctx.adp[index], "vor": ctx.adj_vor[index],
                "depth_chart_order": 1,
            })
            engine_says, _ = valuation.eligible(candidate, state)

            self.assertEqual(sim_says, expected,
                             "sim disagreed on %s in round %d" % (position, round_no))
            self.assertEqual(sim_says, engine_says,
                             "sim and live engine disagree on %s in round %d"
                             % (position, round_no))

    def test_bench_discount_matches_the_live_engine(self):
        rosters = [[], ["RB"], ["RB", "RB"], ["RB", "RB", "RB"],
                   ["RB", "RB", "RB", "TE", "TE"], ["QB"], ["WR", "WR"]]
        for roster in rosters:
            counts = {}
            for pos in roster:
                counts[pos] = counts.get(pos, 0) + 1
            for position in ("QB", "RB", "WR", "TE"):
                self.assertEqual(
                    simulation._lineup_multiplier(position, counts),
                    valuation.lineup_multiplier(position, roster),
                    "bench discount drifted for %s on %s" % (position, roster))


class TestSimulationOutput(unittest.TestCase):
    def setUp(self):
        self.board = synthetic_board()
        self.state = {"taken": set(), "current_pick": 1}

    def test_detects_a_clearly_worse_pick(self):
        best = next(p for p in self.board if p["position"] == "RB")
        defense = next(p for p in self.board if p["position"] == "DEF")
        result = simulation.run(self.board, self.state, SHAPE, 7, {}, [],
                                [best["player_id"], defense["player_id"]], runs=120)
        self.assertTrue(result["ok"])
        ranked = result["candidates"]
        self.assertEqual(ranked[0]["player_id"], best["player_id"],
                         "taking a defense at pick 1 outscored a top back")
        self.assertLess(ranked[1]["mean"], ranked[0]["mean"])
        # Paired comparison: the bad pick should almost never win.
        self.assertLess(ranked[1]["beats_best_pct"], 15.0)

    def test_distribution_fields_are_coherent(self):
        best = self.board[0]
        result = simulation.run(self.board, self.state, SHAPE, 7, {}, [],
                                [best["player_id"]], runs=80)
        entry = result["candidates"][0]
        self.assertEqual(entry["runs"], 80)
        self.assertLessEqual(entry["min"], entry["p10"])
        self.assertLessEqual(entry["p10"], entry["median"])
        self.assertLessEqual(entry["median"], entry["p90"])
        self.assertLessEqual(entry["p90"], entry["max"])
        self.assertGreater(entry["mean"], 0)
        self.assertEqual(entry["incomplete_lineup_runs"], 0)
        self.assertTrue(entry["histogram"])
        self.assertEqual(sum(b["count"] for b in entry["histogram"]), 80)

    def test_common_random_numbers_make_it_reproducible(self):
        best = self.board[0]
        args = (self.board, self.state, SHAPE, 7, {}, [], [best["player_id"]])
        first = simulation.run(*args, runs=60)
        second = simulation.run(*args, runs=60)
        self.assertEqual(first["candidates"][0]["mean"],
                         second["candidates"][0]["mean"])

    def test_verdict_is_plain_english(self):
        rb = next(p for p in self.board if p["position"] == "RB")
        defense = next(p for p in self.board if p["position"] == "DEF")
        result = simulation.run(self.board, self.state, SHAPE, 7, {}, [],
                                [rb["player_id"], defense["player_id"]], runs=60)
        summary = result["summary"]
        self.assertTrue(summary)
        for jargon in ("VOR", "stdev", "p10", "adj_vor"):
            self.assertNotIn(jargon, summary)

    def test_refuses_politely_without_a_slot(self):
        result = simulation.run(self.board, self.state, SHAPE, None, {}, [],
                                [self.board[0]["player_id"]], runs=10)
        self.assertIn("error", result)
        self.assertIn("slot", result["error"].lower())

    def test_handles_unavailable_candidates(self):
        taken_id = self.board[0]["player_id"]
        state = {"taken": {taken_id}, "current_pick": 1}
        result = simulation.run(self.board, state, SHAPE, 7, {}, [],
                                [taken_id], runs=10)
        self.assertIn("error", result)

    def test_respects_players_already_drafted(self):
        gone = {p["player_id"] for p in self.board[:30]}
        keeper = self.board[31]
        state = {"taken": set(gone), "current_pick": 31}
        result = simulation.run(self.board, state, SHAPE, 7, {}, [],
                                [keeper["player_id"]], runs=40)
        self.assertTrue(result["ok"])
        self.assertEqual(result["from_pick"], 31)

    def test_fast_enough_to_use_on_the_clock(self):
        """500 runs across four candidates must fit inside a 2-minute pick."""
        candidates = [p["player_id"] for p in self.board[:4]]
        started = time.time()
        result = simulation.run(self.board, self.state, SHAPE, 7, {}, [],
                                candidates, runs=500)
        elapsed = time.time() - started
        self.assertTrue(result["ok"])
        self.assertLess(elapsed, 20.0,
                        "500 runs x 4 candidates took %.1fs - too slow to use "
                        "under a 2-minute clock" % elapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
