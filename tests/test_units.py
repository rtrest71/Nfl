"""Unit tests for the parts that decide who gets drafted."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import draftstate
import projections as paste
import scoring
import sleeper
import valuation


class TestScoring(unittest.TestCase):
    def test_exact_league_table(self):
        # 4000 pass yds, 30 pass TD, 10 INT, 300 rush yds, 3 rush TD, 2 fumbles
        stats = {"pass_yd": 4000, "pass_td": 30, "pass_int": 10,
                 "rush_yd": 300, "rush_td": 3, "fum_lost": 2}
        expected = (4000 * 0.04 + 30 * 4 + 10 * -1
                    + 300 * 0.1 + 3 * 6 + 2 * -2)
        self.assertAlmostEqual(scoring.fantasy_points(stats, "QB"), expected, places=2)

    def test_passing_td_is_four_not_six(self):
        """The whole point of computing from raw stats rather than a PPR column."""
        four = scoring.fantasy_points({"pass_td": 30}, "QB")
        self.assertAlmostEqual(four, 120.0)
        self.assertNotAlmostEqual(four, 180.0)

    def test_full_ppr_receptions(self):
        stats = {"rec": 100, "rec_yd": 1200, "rec_td": 8}
        self.assertAlmostEqual(scoring.fantasy_points(stats, "WR"),
                               100 * 1.0 + 1200 * 0.1 + 8 * 6, places=2)

    def test_alias_keys_from_other_sources(self):
        a = scoring.fantasy_points({"receptions": 80, "receiving_yards": 1000,
                                    "receiving_tds": 7}, "WR")
        b = scoring.fantasy_points({"rec": 80, "rec_yd": 1000, "rec_td": 7}, "WR")
        self.assertAlmostEqual(a, b)

    def test_defense_points_allowed_bucket(self):
        # 17 games allowing 10 points a game -> the 7-13 bucket, 4 pts a game.
        stats = {"sack": 40, "int": 15, "pts_allow": 10, "gp": 17}
        points = scoring.fantasy_points(stats, "DEF")
        self.assertAlmostEqual(points, 40 * 1 + 15 * 2 + 4 * 17, places=2)

    def test_defense_bucket_counts_take_precedence(self):
        stats = {"pts_allow_0": 1, "pts_allow_1_6": 2, "pts_allow": 14, "gp": 17}
        self.assertAlmostEqual(scoring.fantasy_points(stats, "DEF"),
                               1 * 10 + 2 * 7, places=2)

    def test_kicker_distance_tiers(self):
        stats = {"fgm_30_39": 10, "fgm_40_49": 6, "fgm_50_59": 3, "xpm": 30,
                 "fg_miss": 4}
        self.assertAlmostEqual(scoring.fantasy_points(stats, "K"),
                               10 * 3 + 6 * 4 + 3 * 5 + 30 * 1 + 4 * -1, places=2)

    def test_non_scoring_stats_are_ignored(self):
        self.assertAlmostEqual(
            scoring.fantasy_points({"rec": 10, "targets": 150, "snaps": 900}, "WR"),
            10.0)


class TestSnakeOrder(unittest.TestCase):
    def test_first_and_last_slot(self):
        self.assertEqual(draftstate.pick_number(1, 1), 1)
        self.assertEqual(draftstate.pick_number(1, 12), 12)
        self.assertEqual(draftstate.pick_number(2, 12), 13)   # turn
        self.assertEqual(draftstate.pick_number(2, 1), 24)
        self.assertEqual(draftstate.pick_number(3, 1), 25)

    def test_my_picks_all_rounds(self):
        picks = draftstate.my_picks(5)
        self.assertEqual(len(picks), 15)
        self.assertEqual(picks[0], 5)
        self.assertEqual(picks[1], 20)     # 12 - 5 + 1 = 8 -> 12 + 8
        self.assertEqual(picks[2], 29)
        self.assertEqual(sorted(picks), picks)

    def test_every_pick_number_used_exactly_once(self):
        seen = []
        for slot in range(1, 13):
            seen.extend(draftstate.my_picks(slot))
        self.assertEqual(sorted(seen), list(range(1, 12 * 15 + 1)))

    def test_slot_of_pick_round_trips(self):
        for slot in range(1, 13):
            for rnd in range(1, 16):
                pick = draftstate.pick_number(rnd, slot)
                self.assertEqual(draftstate.slot_of_pick(pick), (rnd, slot))

    def test_third_round_reversal(self):
        # With reversal at round 3, round 3 repeats round 2's order.
        self.assertEqual(draftstate.pick_number(2, 12, reversal_round=3), 13)
        self.assertEqual(draftstate.pick_number(3, 12, reversal_round=3), 25)

    def test_linear_draft(self):
        self.assertEqual(draftstate.pick_number(2, 1, draft_type="linear"), 13)

    def test_find_my_slot_before_order_is_set(self):
        self.assertIsNone(draftstate.find_my_slot({"draft_order": None}, "u1"))
        self.assertEqual(draftstate.find_my_slot({"draft_order": {"u1": 7}}, "u1"), 7)


class TestSurvival(unittest.TestCase):
    def test_monotonic_in_pick_number(self):
        early = valuation.survival_probability(20, 6, 15)
        late = valuation.survival_probability(20, 6, 30)
        self.assertGreater(early, late)

    def test_at_adp_is_a_coin_flip(self):
        self.assertAlmostEqual(valuation.survival_probability(24, 8, 24), 0.5, places=6)

    def test_bounded(self):
        self.assertLess(valuation.survival_probability(5, 4, 90), 0.001)
        self.assertGreater(valuation.survival_probability(150, 20, 10), 0.99)

    def test_stdev_widens_late(self):
        self.assertLess(valuation.default_stdev(10), valuation.default_stdev(120))

    def test_expected_best_prefers_likely_survivors(self):
        certain = [{"adj_vor": 50, "adp": 200, "adp_stdev": 10}]
        doomed = [{"adj_vor": 50, "adp": 1, "adp_stdev": 2}]
        self.assertGreater(valuation.expected_best(certain, 30),
                           valuation.expected_best(doomed, 30))


class TestBaselines(unittest.TestCase):
    def test_matches_the_spec(self):
        base = config.baselines()
        self.assertEqual(base["QB"], 12)
        self.assertEqual(base["RB"], 34)
        self.assertEqual(base["WR"], 36)
        self.assertEqual(base["TE"], 14)
        self.assertEqual(base["K"], 12)
        self.assertEqual(base["DEF"], 12)

    def test_flex_split_is_tunable(self):
        original = dict(config.FLEX_SPLIT)
        try:
            config.FLEX_SPLIT.update({"RB": 0.5, "WR": 0.5, "TE": 0.0})
            self.assertEqual(config.baselines()["RB"], 36)
            self.assertEqual(config.baselines()["TE"], 12)
        finally:
            config.FLEX_SPLIT.clear()
            config.FLEX_SPLIT.update(original)


class TestRosterNeeds(unittest.TestCase):
    def test_empty_roster_needs_everything(self):
        needs = valuation.roster_needs([])
        self.assertEqual(needs["RB"], 2)
        self.assertEqual(needs["WR"], 2)
        self.assertEqual(needs["QB"], 1)
        self.assertEqual(needs["FLEX"], 2)

    def test_extra_backs_fill_flex(self):
        needs = valuation.roster_needs(["RB", "RB", "RB", "WR", "WR", "WR"])
        self.assertEqual(needs["RB"], 0)
        self.assertEqual(needs["WR"], 0)
        self.assertEqual(needs["FLEX"], 0)

    def test_required_slots_remaining(self):
        needs = valuation.roster_needs(["QB", "RB", "RB", "WR", "WR", "TE"])
        # Only the two flex slots plus K and DEF are left.
        self.assertEqual(valuation.required_slots_remaining(needs), 4)


class TestEligibility(unittest.TestCase):
    def _state(self, **kwargs):
        state = {
            "taken": set(), "round": 1, "current_pick": 1, "my_next_pick": 20,
            "picks_until_next": 19, "my_roster": [], "my_remaining_picks": [1, 20],
            "needs": valuation.roster_needs([]), "picks_left": 15,
            "opponent_needs": {},
        }
        state.update(kwargs)
        if "my_roster" in kwargs:
            state["needs"] = valuation.roster_needs(kwargs["my_roster"])
        return state

    def _player(self, **kwargs):
        player = {"player_id": "x", "position": "RB", "name": "Test", "vor": 10.0,
                  "adp": 30.0, "adp_stdev": 8.0, "depth_chart_order": 1}
        player.update(kwargs)
        return player

    def test_qb_blocked_early(self):
        ok, reason = valuation.eligible(self._player(position="QB", vor=20),
                                        self._state(round=3))
        self.assertFalse(ok)
        self.assertIn("one QB", reason)

    def test_qb_allowed_from_round_eight(self):
        ok, _ = valuation.eligible(self._player(position="QB"), self._state(round=8))
        self.assertTrue(ok)

    def test_elite_qb_falling_absurdly_far_is_allowed(self):
        player = self._player(position="QB", vor=config.QB_ELITE_STEAL_VOR + 5, adp=10)
        ok, _ = valuation.eligible(player, self._state(round=4, current_pick=40))
        self.assertTrue(ok)

    def test_qb_block_lifts_when_the_room_drains_the_position(self):
        """Waiting on QB is right only while supply outruns demand. A league-mate
        who drafts QBs early breaks that, so the block has to notice."""
        player = self._player(position="QB", vor=40.0, adp=30.0)

        plenty = self._state(round=4, startable_qbs_left=99)
        ok, reason = valuation.eligible(player, plenty)
        self.assertFalse(ok)
        self.assertIn("wait until round", reason)

        scarce = self._state(round=4,
                             startable_qbs_left=config.QB_SCARCITY_UNLOCK)
        ok, _ = valuation.eligible(player, scarce)
        self.assertTrue(ok, "QB still blocked with the position running out")

    def test_scarcity_never_justifies_a_second_quarterback(self):
        player = self._player(position="QB", vor=40.0, adp=30.0)
        state = self._state(round=4, my_roster=["QB"], startable_qbs_left=1)
        ok, reason = valuation.eligible(player, state)
        self.assertFalse(ok)
        self.assertIn("already have", reason)

    def test_kicker_and_defense_blocked_until_round_fourteen(self):
        for pos in ("K", "DEF"):
            ok, _ = valuation.eligible(self._player(position=pos), self._state(round=10))
            self.assertFalse(ok, "%s should be blocked in round 10" % pos)
            ok, _ = valuation.eligible(self._player(position=pos), self._state(round=14))
            self.assertTrue(ok, "%s should be allowed in round 14" % pos)

    def test_backup_running_back_blocked_early(self):
        ok, reason = valuation.eligible(
            self._player(depth_chart_order=2), self._state(round=5))
        self.assertFalse(ok)
        self.assertIn("bench room", reason)

    def test_second_qb_never_recommended(self):
        ok, _ = valuation.eligible(self._player(position="QB"),
                                   self._state(round=10, my_roster=["QB"]))
        self.assertFalse(ok)

    def test_last_picks_must_fill_starting_slots(self):
        # One pick left, no kicker yet: a sixth receiver is not allowed.
        state = self._state(round=15, picks_left=1,
                            my_roster=["QB", "RB", "RB", "WR", "WR", "TE", "RB",
                                       "WR", "DEF"])
        ok, reason = valuation.eligible(self._player(position="WR"), state)
        self.assertFalse(ok)
        self.assertIn("starting slots", reason)

    def test_drafted_players_are_ineligible(self):
        ok, _ = valuation.eligible(self._player(player_id="taken1"),
                                   self._state(taken={"taken1"}))
        self.assertFalse(ok)


class TestLineupValue(unittest.TestCase):
    """VOR assumes a player starts. A bench player must be discounted."""

    def test_empty_roster_starts_everyone(self):
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            self.assertEqual(valuation.lineup_multiplier(pos, []), 1.0)

    def test_third_back_still_starts_in_flex(self):
        self.assertEqual(valuation.lineup_multiplier("RB", ["RB", "RB"]), 1.0)

    def test_flex_slots_absorb_two_extras_then_stop(self):
        # RB RB + one extra RB and one extra TE fills both flex slots.
        roster = ["RB", "RB", "RB", "TE", "TE"]
        self.assertEqual(valuation.lineup_multiplier("RB", roster),
                         config.BENCH_VALUE_MULTIPLIER)
        # A receiver still has two empty starting slots, so full value.
        self.assertEqual(valuation.lineup_multiplier("WR", roster), 1.0)

    def test_second_quarterback_is_bench_value(self):
        self.assertEqual(valuation.lineup_multiplier("QB", ["QB"]),
                         config.BENCH_VALUE_MULTIPLIER)

    def test_recommendation_prefers_the_empty_starting_slot(self):
        """A marginally better bench player must lose to a startable one."""
        def player(pid, pos, vor):
            return {"player_id": pid, "name": pid, "position": pos, "vor": vor,
                    "adj_vor": vor, "adp": 40.0, "adp_stdev": 8.0,
                    "depth_chart_order": 1, "points": 200.0, "risk_reasons": [],
                    "value_gap": 0, "tier": 1, "tier_last": False,
                    "next_tier_size": 0}

        board = [player("rb_bench", "RB", 60.0), player("wr_starter", "WR", 55.0)]
        roster = ["RB", "RB", "RB", "TE", "TE"]   # both flex slots already used
        state = {
            "taken": set(), "round": 6, "current_pick": 60, "my_next_pick": 80,
            "picks_until_next": 20, "my_roster": roster,
            "my_remaining_picks": [60, 80], "needs": valuation.roster_needs(roster),
            "picks_left": 10, "opponent_needs": {},
        }
        result = valuation.recommend(board, state)
        self.assertEqual(result["top"]["player"]["player_id"], "wr_starter",
                         "took a bench running back over an empty receiver slot")

    def test_a_much_better_bench_player_still_wins(self):
        """The discount is a correction, not an override of VOR."""
        def player(pid, pos, vor):
            return {"player_id": pid, "name": pid, "position": pos, "vor": vor,
                    "adj_vor": vor, "adp": 40.0, "adp_stdev": 8.0,
                    "depth_chart_order": 1, "points": 200.0, "risk_reasons": [],
                    "value_gap": 0, "tier": 1, "tier_last": False,
                    "next_tier_size": 0}

        board = [player("rb_elite", "RB", 140.0), player("wr_scrub", "WR", 20.0)]
        roster = ["RB", "RB", "RB", "TE", "TE"]
        state = {
            "taken": set(), "round": 6, "current_pick": 60, "my_next_pick": 80,
            "picks_until_next": 20, "my_roster": roster,
            "my_remaining_picks": [60, 80], "needs": valuation.roster_needs(roster),
            "picks_left": 10, "opponent_needs": {},
        }
        result = valuation.recommend(board, state)
        self.assertEqual(result["top"]["player"]["player_id"], "rb_elite")


class TestTiers(unittest.TestCase):
    def test_tier_break_detected_at_a_cliff(self):
        players = [{"vor": v} for v in (100, 98, 96, 94, 60, 58, 56)]
        valuation.assign_tiers(players)
        tiers = [p["tier"] for p in sorted(players, key=lambda p: -p["vor"])]
        self.assertEqual(tiers[3] + 1, tiers[4])
        cliff = [p for p in players if p["vor"] == 94][0]
        self.assertTrue(cliff["tier_last"])
        self.assertGreater(cliff["next_tier_drop"], 30)

    def test_smooth_distribution_stays_one_tier(self):
        players = [{"vor": 100 - i * 2} for i in range(12)]
        valuation.assign_tiers(players)
        self.assertEqual({p["tier"] for p in players}, {1})


class TestNameMatching(unittest.TestCase):
    def setUp(self):
        self.players = {
            "1": {"player_id": "1", "name": "Marvin Harrison Jr.", "team": "ARI",
                  "position": "WR", "search_rank": 10},
            "2": {"player_id": "2", "name": "Patrick Mahomes", "team": "KC",
                  "position": "QB", "search_rank": 20},
            "3": {"player_id": "3", "name": "Michael Pittman Jr.", "team": "IND",
                  "position": "WR", "search_rank": 40},
            "SF": {"player_id": "SF", "name": "San Francisco Defense", "team": "SF",
                   "position": "DEF", "search_rank": 300},
            "4": {"player_id": "4", "name": "D'Andre Swift", "team": "CHI",
                  "position": "RB", "search_rank": 50},
        }
        self.index = paste.PlayerIndex(self.players)

    def test_suffix_variations(self):
        for spelling in ("Marvin Harrison Jr.", "Marvin Harrison Jr",
                         "Marvin Harrison", "marvin harrison jr."):
            self.assertEqual(self.index.match(spelling, "WR", "ARI"), "1", spelling)

    def test_apostrophes_and_periods(self):
        self.assertEqual(self.index.match("DAndre Swift", "RB"), "4")
        self.assertEqual(self.index.match("D'Andre Swift", "RB", "CHI"), "4")

    def test_initial_form(self):
        self.assertEqual(self.index.match("P. Mahomes", "QB"), "2")

    def test_last_name_first(self):
        self.assertEqual(self.index.match("Pittman Jr., Michael", "WR"), "3")

    def test_defense_naming_variants(self):
        for spelling in ("San Francisco 49ers D/ST", "49ers DST", "SF",
                         "San Francisco Defense", "Niners D/ST"):
            self.assertEqual(self.index.match(spelling, "DEF"), "SF", spelling)

    def test_unknown_player_returns_none(self):
        self.assertIsNone(self.index.match("Nobody At All", "WR"))

    def test_team_alias_normalisation(self):
        self.assertEqual(paste.normalize_team("JAC"), "JAX")
        self.assertEqual(paste.normalize_team("WSH"), "WAS")


class TestPasteParsing(unittest.TestCase):
    def setUp(self):
        self.players = {
            "1": {"player_id": "1", "name": "Ja'Marr Chase", "team": "CIN",
                  "position": "WR", "search_rank": 1},
            "2": {"player_id": "2", "name": "Bijan Robinson", "team": "ATL",
                  "position": "RB", "search_rank": 2},
        }

    def test_csv_any_column_order(self):
        text = ("REC,Player,REC YDS,Team,POS,REC TD\n"
                "110,Ja'Marr Chase,1500,CIN,WR,12\n"
                "60,Bijan Robinson,500,ATL,RB,3\n")
        parsed, report = paste.apply_projection_paste(text, self.players)
        self.assertEqual(report["matched"], 2)
        self.assertEqual(parsed["1"]["stats"]["rec"], 110)
        self.assertEqual(parsed["1"]["stats"]["rec_yd"], 1500)

    def test_tab_separated_browser_copy(self):
        text = ("Player\tTeam\tPOS\tREC\tREC YDS\n"
                "Ja'Marr Chase\tCIN\tWR\t110\t1500\n")
        parsed, report = paste.apply_projection_paste(text, self.players)
        self.assertEqual(report["matched"], 1)

    def test_multi_space_table(self):
        text = ("Player            Team   POS   REC   REC YDS\n"
                "Ja'Marr Chase     CIN    WR    110   1500\n")
        parsed, report = paste.apply_projection_paste(text, self.players)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(parsed["1"]["stats"]["rec"], 110)

    def test_points_column_used_when_no_raw_stats(self):
        text = "Player,Team,POS,FPTS\nJa'Marr Chase,CIN,WR,310.5\n"
        parsed, _ = paste.apply_projection_paste(text, self.players)
        self.assertEqual(parsed["1"]["points_override"], 310.5)

    def test_numbered_adp_list(self):
        text = "1. Ja'Marr Chase CIN WR 1.2\n2. Bijan Robinson ATL RB 2.4\n"
        parsed, report = paste.apply_adp_paste(text, self.players)
        self.assertEqual(report["matched"], 2)
        self.assertAlmostEqual(parsed["1"]["adp"], 1.2)
        self.assertAlmostEqual(parsed["2"]["adp"], 2.4)

    def test_adp_csv_with_stdev(self):
        text = ("Rank,Player,Team,POS,ADP,Std Dev\n"
                "1,Ja'Marr Chase,CIN,WR,1.4,0.8\n")
        parsed, _ = paste.apply_adp_paste(text, self.players)
        self.assertAlmostEqual(parsed["1"]["adp"], 1.4)
        self.assertAlmostEqual(parsed["1"]["stdev"], 0.8)

    def test_bare_name_list_falls_back_to_order(self):
        text = "Ja'Marr Chase\nBijan Robinson\n"
        parsed, report = paste.apply_adp_paste(text, self.players)
        self.assertEqual(report["matched"], 2)
        self.assertLess(parsed["1"]["adp"], parsed["2"]["adp"])

    def test_unmatched_names_are_reported_not_swallowed(self):
        text = "Player,Team,POS,REC\nSomeone Fake,XXX,WR,50\n"
        _, report = paste.apply_projection_paste(text, self.players)
        self.assertEqual(report["unmatched_count"], 1)
        self.assertIn("Someone Fake", report["unmatched"])

    def test_empty_paste_is_safe(self):
        parsed, report = paste.apply_projection_paste("", self.players)
        self.assertEqual(parsed, {})
        self.assertEqual(report["parsed"], 0)


class TestLiveScoringSettings(unittest.TestCase):
    """Regression: Sleeper's own keys must not collide through paste aliases."""

    def test_fum_and_fum_lost_stay_separate(self):
        league = {"scoring_settings": {"fum": -1.0, "fum_lost": -2.0}}
        live = sleeper.live_scoring_settings(league)
        self.assertEqual(live["fum"], -1.0)
        self.assertEqual(live["fum_lost"], -2.0)

    def test_zero_valued_setting_is_kept_not_dropped(self):
        """A league that scores fumbles at 0 must override the config's -2."""
        league = {"scoring_settings": {"fum_lost": 0.0, "rec": 1.0}}
        live = sleeper.live_scoring_settings(league)
        self.assertIn("fum_lost", live)
        self.assertEqual(live["fum_lost"], 0.0)
        merged = dict(config.SCORING)
        merged.update(live)
        self.assertEqual(merged["fum_lost"], 0.0)
        self.assertEqual(scoring.fantasy_points({"fum_lost": 3}, "RB", merged), 0.0)

    def test_sleeper_spellings_are_translated(self):
        league = {"scoring_settings": {"safe": 2.0, "ff": 1.0, "blk_kick": 2.0,
                                       "fgmiss": -1.0, "pts_allow_35p": -4.0}}
        live = sleeper.live_scoring_settings(league)
        self.assertEqual(live["safety"], 2.0)
        self.assertEqual(live["forced_fumble"], 1.0)
        self.assertEqual(live["blocked_kick"], 2.0)
        self.assertEqual(live["fg_miss"], -1.0)
        self.assertEqual(live["pts_allow_35_plus"], -4.0)


class TestDraftSelection(unittest.TestCase):
    """Regression: taking drafts[0] blindly picked a 3-round practice draft."""

    def _draft(self, did, status, rounds, teams=12, created=0):
        return {"draft_id": did, "status": status, "created": created,
                "settings": {"rounds": rounds, "teams": teams}}

    def test_prefers_the_draft_matching_the_league_shape(self):
        drafts = [self._draft("practice", "pre_draft", 3),
                  self._draft("real", "pre_draft", 15)]
        chosen, others = sleeper.pick_draft(drafts)
        self.assertEqual(chosen["draft_id"], "real")
        self.assertEqual(len(others), 1)

    def test_prefers_a_live_draft_over_a_completed_one(self):
        drafts = [self._draft("old", "complete", 15),
                  self._draft("live", "drafting", 15)]
        chosen, _ = sleeper.pick_draft(drafts)
        self.assertEqual(chosen["draft_id"], "live")

    def test_explicit_id_wins(self):
        drafts = [self._draft("a", "drafting", 15), self._draft("b", "pre_draft", 3)]
        chosen, others = sleeper.pick_draft(drafts, draft_id="b")
        self.assertEqual(chosen["draft_id"], "b")
        self.assertEqual(len(others), 1)

    def test_empty_list_is_safe(self):
        chosen, others = sleeper.pick_draft([])
        self.assertIsNone(chosen)
        self.assertEqual(others, [])


class TestValueGapScoping(unittest.TestCase):
    """Regression: value gap ranked across all 3300 players, so an undrafted
    kicker scored +2978 and saturated the value boost."""

    def _board(self):
        players, projections, adp = {}, {}, {}
        # 60 real draftable players plus deep players nobody drafts.
        for i in range(60):
            pid = "p%d" % i
            players[pid] = {"player_id": pid, "name": "Player %d" % i,
                            "position": ["RB", "WR"][i % 2], "team": "XXX",
                            "age": 25, "depth_chart_order": 1, "search_rank": i}
            projections[pid] = {"stats": {"rec": 80 - i, "rec_yd": 1200 - i * 12,
                                          "rec_td": 8}}
            adp[pid] = {"adp": float(i + 1)}
        for i in range(20):
            pid = "deep%d" % i
            players[pid] = {"player_id": pid, "name": "Deep %d" % i,
                            "position": "K", "team": "XXX", "age": 28,
                            "depth_chart_order": 1, "search_rank": 3000 + i}
            projections[pid] = {"stats": {"xpm": 20, "fgm_30_39": 5}}
            adp[pid] = {"adp": float(2500 + i)}
        return valuation.build_board(players, projections, adp, current_round=1)

    def test_players_beyond_the_draft_get_no_value_gap(self):
        board = self._board()
        deep = [p for p in board if p["player_id"].startswith("deep")]
        self.assertTrue(deep)
        for player in deep:
            self.assertEqual(player["value_gap"], 0,
                             "%s scored a value gap despite being undraftable"
                             % player["name"])
            self.assertFalse(player["draftable"])

    def test_value_gap_stays_within_a_sane_range(self):
        board = self._board()
        for player in board:
            self.assertLess(abs(player["value_gap"]), 200,
                            "%s has an absurd value gap of %d"
                            % (player["name"], player["value_gap"]))

    def test_value_board_excludes_kickers_and_undraftables(self):
        board = self._board()
        state = {"taken": set()}
        for player in valuation.value_board(board, state):
            self.assertNotIn(player["position"], ("K", "DEF"))
            self.assertTrue(player["draftable"])
            self.assertGreater(player["vor"], 0)


class TestOwnHandcuff(unittest.TestCase):
    """Someone else's backup is a wasted spot. Your own RB1's backup is
    insurance against the worst thing that can happen in a no-IR league."""

    def _player(self, pid, position, team, vor, depth=1):
        return {"player_id": pid, "name": pid, "position": position, "team": team,
                "vor": vor, "adj_vor": vor, "adp": 120.0, "adp_stdev": 25.0,
                "depth_chart_order": depth, "points": 150.0, "risk_reasons": [],
                "value_gap": 0, "tier": 1, "tier_last": False, "next_tier_size": 0}

    def _state(self, round_no=11):
        roster = ["RB", "RB", "WR", "WR", "TE", "RB", "WR", "QB", "WR", "RB"]
        return {
            "taken": set(), "round": round_no, "current_pick": 125,
            "my_next_pick": 140, "picks_until_next": 15, "my_roster": roster,
            "my_players": [{"player_id": "bijan", "position": "RB", "team": "ATL"}],
            "my_remaining_picks": [125, 140, 155, 170],
            "needs": valuation.roster_needs(roster), "picks_left": 4,
            "opponent_needs": {},
        }

    def test_identifies_a_backup_to_a_back_i_own(self):
        state = self._state()
        mine = self._player("mine", "RB", "ATL", 20.0, depth=2)
        other = self._player("other", "RB", "KC", 26.0, depth=2)
        self.assertTrue(valuation.is_own_handcuff(mine, state))
        self.assertFalse(valuation.is_own_handcuff(other, state))

    def test_a_receiver_is_never_an_rb_handcuff(self):
        state = self._state()
        wr = self._player("wr", "WR", "ATL", 20.0, depth=2)
        self.assertFalse(valuation.is_own_handcuff(wr, state))

    def test_my_own_handcuff_is_allowed_late_but_not_early(self):
        mine = self._player("mine", "RB", "ATL", 20.0, depth=2)
        late, _ = valuation.eligible(mine, self._state(round_no=11))
        early, _ = valuation.eligible(mine, self._state(round_no=6))
        self.assertTrue(late)
        self.assertFalse(early, "insurance should not come before your starters")

    def test_someone_elses_backup_stays_blocked(self):
        other = self._player("other", "RB", "KC", 26.0, depth=2)
        ok, reason = valuation.eligible(other, self._state(round_no=11))
        self.assertFalse(ok)
        self.assertIn("bench room", reason)

    def test_insurance_beats_a_better_backup_on_another_team(self):
        state = self._state()
        board = [self._player("mine", "RB", "ATL", 20.0, depth=2),
                 self._player("other", "RB", "KC", 26.0, depth=2),
                 self._player("wr", "WR", "BUF", 22.0)]
        result = valuation.recommend(board, state)
        self.assertEqual(result["top"]["player"]["player_id"], "mine")
        self.assertTrue(result["top"]["player"]["own_handcuff"])
        self.assertIn("backup to a running back you already own",
                      result["top"]["reason"])

    def test_no_handcuff_bonus_without_the_starter(self):
        state = self._state()
        state["my_players"] = [{"player_id": "x", "position": "RB", "team": "SF"}]
        board = [self._player("mine", "RB", "ATL", 20.0, depth=2),
                 self._player("wr", "WR", "BUF", 22.0)]
        result = valuation.recommend(board, state)
        self.assertEqual(result["top"]["player"]["player_id"], "wr")


class TestQueueExport(unittest.TestCase):
    """Regression: before the draft starts the slot is unknown, so we own no
    pick numbers - and the queue was built from that, yielding three players
    instead of forty. That is exactly when the queue has to be loaded."""

    def _board(self):
        players, projections, adp = {}, {}, {}
        shape = {"QB": 24, "RB": 60, "WR": 70, "TE": 24, "K": 16, "DEF": 16}
        pid = 0
        for position, count in shape.items():
            for i in range(count):
                pid += 1
                key = str(pid)
                players[key] = {"player_id": key, "name": "%s %d" % (position, i),
                                "position": position, "team": "XXX", "age": 25,
                                "depth_chart_order": 1, "search_rank": pid}
                projections[key] = {"stats": {"rec": max(0, 90 - i),
                                              "rec_yd": max(0, 1300 - i * 15),
                                              "rec_td": max(0, 10 - i // 6)}}
                base = i * 2.0 + {"K": 170, "DEF": 165, "QB": 40, "TE": 30}.get(
                    position, 0)
                adp[key] = {"adp": max(1.0, base)}
        return valuation.build_board(players, projections, adp, current_round=1)

    def _state(self, **kwargs):
        state = {"taken": set(), "round": 1, "current_pick": 1,
                 "my_roster": [], "my_remaining_picks": [],
                 "needs": valuation.roster_needs([]), "picks_left": 15,
                 "opponent_needs": {}, "my_next_pick": None}
        state.update(kwargs)
        return state

    def test_full_queue_before_the_slot_is_known(self):
        queue = valuation.build_queue(self._board(), self._state())
        self.assertEqual(len(queue), config.QUEUE_LENGTH,
                         "queue was %d players before the draft started"
                         % len(queue))

    def test_queue_has_no_repeats(self):
        queue = valuation.build_queue(self._board(), self._state())
        ids = [p["player_id"] for p in queue]
        self.assertEqual(len(ids), len(set(ids)))

    def test_queue_does_not_front_load_kickers_or_defenses(self):
        queue = valuation.build_queue(self._board(), self._state())
        early = [p["position"] for p in queue[:20]]
        self.assertNotIn("K", early)
        self.assertNotIn("DEF", early)

    def test_queue_is_mostly_pass_catchers_and_backs(self):
        queue = valuation.build_queue(self._board(), self._state())
        skill = sum(1 for p in queue if p["position"] in ("RB", "WR", "TE"))
        self.assertGreaterEqual(skill, 30,
                                "a full-PPR queue should be dominated by "
                                "RB/WR/TE, got %d of %d" % (skill, len(queue)))

    def test_queue_still_works_mid_draft(self):
        board = self._board()
        state = self._state(round=9, current_pick=100,
                            my_roster=["RB", "RB", "WR", "WR", "TE", "RB", "WR", "QB"],
                            my_remaining_picks=[100, 117, 124, 141, 148, 165, 172])
        state["needs"] = valuation.roster_needs(state["my_roster"])
        queue = valuation.build_queue(board, state)
        self.assertGreaterEqual(len(queue), 15)


class TestRunsAndWarnings(unittest.TestCase):
    def test_run_detection(self):
        history = [{"position": p} for p in
                   ["WR", "RB", "RB", "WR", "RB", "TE"]]
        runs = draftstate.detect_run(history)
        self.assertTrue(any(r["position"] == "RB" and r["count"] == 3 for r in runs))

    def test_no_run_when_spread_out(self):
        history = [{"position": p} for p in ["WR", "RB", "TE", "QB", "WR", "RB"]]
        self.assertEqual(draftstate.detect_run(history), [])

    def test_imbalance_warning_for_five_backs_one_receiver(self):
        roster = [{"position": "RB"}] * 5 + [{"position": "WR"}]
        warnings = draftstate.imbalance_warnings(roster, picks_left=9)
        self.assertTrue(any("receivers" in w for w in warnings))

    def test_roster_completion_warning(self):
        roster = [{"position": "RB"}, {"position": "RB"}]
        warnings = draftstate.imbalance_warnings(roster, picks_left=2)
        self.assertTrue(any("cannot fill" in w or "must fill" in w for w in warnings))

    def test_bye_conflict(self):
        roster = [{"position": "WR", "name": "A", "bye": 9, "team": "X"},
                  {"position": "WR", "name": "B", "bye": 9, "team": "Y"}]
        conflicts = draftstate.bye_conflicts(roster)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("week 9", conflicts[0]["message"])

    def test_opponent_needs_counts_managers(self):
        rosters = {1: ["RB", "RB"], 2: ["WR", "WR"], 3: []}
        needs = draftstate.opponent_needs(rosters, [1, 2, 3])
        self.assertEqual(needs.get("WR"), 2)   # slots 1 and 3 still need receivers
        self.assertEqual(needs.get("RB"), 2)   # slots 2 and 3 still need backs


if __name__ == "__main__":
    unittest.main(verbosity=2)
