#!/usr/bin/env python3
"""Sleeper Draft Assistant - local server.

    python3 app.py     ->   http://localhost:8000

Standard library only: no pip install, no API keys, no accounts, nothing
deployed anywhere. The browser talks to this server, and this server talks to
Sleeper, which sidesteps the CORS problem a file:// page would hit.

Everything degrades to cache. If Sleeper is unreachable at kickoff the app
still boots off cache/players.json and you can drive it by hand.
"""

import argparse
import json
import os
import re
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import draftstate
import practice as practice_mode
import projections as paste
import simulation
import sleeper
import valuation


class Assistant:
    """All mutable draft state, behind one lock."""

    def __init__(self, offline=False, slot_override=None, draft_id_override=None,
                 rounds_override=None, practice=None):
        self.lock = threading.RLock()
        self.offline = offline
        self.practice = practice          # a PracticeDraft, or None for the real thing
        self.slot_override = slot_override
        self.draft_id_override = draft_id_override or config.DRAFT_ID_OVERRIDE
        self.rounds_override = rounds_override or config.ROUNDS_OVERRIDE

        self.players = {}
        self.index = None
        self.projections = {}
        self.adp = {}
        self.scoring_settings = None
        self.byes = {}

        self.user = None
        self.league = None
        self.draft = None
        self.league_users = {}
        self.picks = []

        self.board = []
        self.board_round = None
        self.snapshot = {}

        self.manual_taken = []      # undo stack of manually marked players
        self.sim = {"status": "idle"}
        self.warnings = []
        if practice:
            # Belongs here rather than in main(): however this app is started,
            # a practice draft must be impossible to mistake for the real one.
            self.warnings.append(
                "PRACTICE DRAFT — you are drafting from slot %d against 11 "
                "simulated managers. None of this is real. Restart without "
                "--practice for the live draft." % practice.my_slot)
        self.sim_thread = None
        self.errors = []
        self.notes = []
        self.last_poll = None
        self.last_poll_ok = None
        self.poll_failures = 0
        self.running = True

    # -- loading ----------------------------------------------------------

    def log(self, message):
        stamp = time.strftime("%H:%M:%S")
        self.notes.append("%s  %s" % (stamp, message))
        del self.notes[:-60]
        print("[assistant] %s" % message)

    def load_cached_data(self):
        """Load whatever build_data.py left on disk. Never raises."""
        with self.lock:
            self.players = sleeper.cache_read(config.PLAYERS_CACHE, {}) or {}
            cached_proj = sleeper.cache_read(config.PROJECTIONS_CACHE, {}) or {}
            self.projections = cached_proj.get("players", cached_proj) or {}
            cached_adp = sleeper.cache_read(config.ADP_CACHE, {}) or {}
            self.adp = cached_adp.get("players", cached_adp) or {}
            self.byes = cached_adp.get("byes", {}) or {}
            league_cache = sleeper.cache_read(config.LEAGUE_CACHE, {}) or {}
            self.league = league_cache.get("league")
            self.draft = league_cache.get("draft")
            self.user = league_cache.get("user")
            self.league_users = league_cache.get("users") or {}
            if self.league:
                live = sleeper.live_scoring_settings(self.league)
                if live:
                    merged = dict(config.SCORING)
                    merged.update(live)
                    self.scoring_settings = merged

            if self.players:
                self.index = paste.PlayerIndex(self.players)
                self.log("loaded %d players from cache" % len(self.players))
            else:
                self.errors.append(
                    "No player database on disk. Run: python3 build_data.py")

            if not self.projections:
                self.warnings.append(
                    "No projections loaded. Paste a projections table in the "
                    "Data panel, or every player scores zero.")
            if not self.adp:
                self.warnings.append(
                    "No pasted ADP. Falling back to Sleeper's own player ranking "
                    "as an ADP estimate - paste real Sleeper ADP for sharper "
                    "survival odds.")

    def resolve_league(self):
        """Resolve user -> league -> draft from the Sleeper API."""
        if self.offline:
            self.log("offline mode: skipping league resolution")
            return
        try:
            user = sleeper.get_user()
            leagues = sleeper.get_leagues(user["user_id"])
            league = sleeper.pick_league(leagues)
            if not league:
                names = ", ".join(str(l.get("name")) for l in (leagues or []))
                raise sleeper.SleeperError(
                    "No league named %r for %s. Leagues found: %s"
                    % (config.LEAGUE_NAME, config.USERNAME, names or "none"))

            drafts = sleeper.get_drafts(league["league_id"])
            draft, others = sleeper.pick_draft(drafts, draft_id=self.draft_id_override)

            # A requested draft that is not one of the league's own - a mock
            # draft, say - has to be fetched directly. Practising against a
            # mock is the whole reason this path exists.
            if self.draft_id_override and (
                    not draft
                    or str(draft.get("draft_id")) != str(self.draft_id_override)):
                draft = sleeper.get_draft(self.draft_id_override)
                others = []
                if draft:
                    self.log("following draft %s directly (not a league draft)"
                             % self.draft_id_override)
                    self.warnings.append(
                        "PRACTICE MODE: following draft %s, which is not your "
                        "league's draft. Restart without --draft-id for the "
                        "real thing." % self.draft_id_override)
                else:
                    self.errors.append(
                        "Could not find draft %s." % self.draft_id_override)

            for other in others:
                self.log("ignoring other draft: %s" % sleeper.describe_draft(other))
            if others:
                self.warnings.append(
                    "This league has %d drafts. Using %s. If that is the wrong "
                    "one, restart with --draft-id <id>."
                    % (len(others) + 1, sleeper.describe_draft(draft)))
            users = {u["user_id"]: (u.get("display_name") or u.get("username"))
                     for u in (sleeper.get_league_users(league["league_id"]) or [])}

            with self.lock:
                self.user, self.league, self.draft = user, league, draft
                self.league_users = users
                live = sleeper.live_scoring_settings(league)
                if live:
                    merged = dict(config.SCORING)
                    merged.update(live)
                    self.scoring_settings = merged
                mismatches = sleeper.verify_league_settings(league)
                for warning in mismatches:
                    if warning not in self.warnings:
                        self.warnings.append(warning)

            sleeper.cache_write(config.LEAGUE_CACHE, {
                "user": user, "league": league, "draft": draft, "users": users,
                "fetched_at": time.time(),
            })
            self.log("league resolved: %s (draft %s, status %s)"
                     % (league.get("name"),
                        (draft or {}).get("draft_id"),
                        (draft or {}).get("status")))
        except sleeper.SleeperError as exc:
            self.errors.append("Could not resolve league: %s" % exc)
            self.log("league resolution failed: %s" % exc)

    # -- polling ----------------------------------------------------------

    def poll_once(self):
        """One poll cycle: refresh the draft object and the picks list."""
        if self.practice:
            # Practice mode replaces Sleeper entirely: the simulated managers
            # pick on a timer and the picks look exactly like real ones.
            with self.lock:
                board = self.board or []
            self.practice.tick(board)
            with self.lock:
                self.draft = self.practice.draft_object(
                    (self.user or {}).get("user_id") or "me")
                self.league_users = self.practice.manager_names(
                    (self.user or {}).get("user_id") or "me", config.USERNAME)
                self.picks = list(self.practice.picks)
                self.last_poll = time.time()
                self.last_poll_ok = True
                self.poll_failures = 0
            return

        if self.offline:
            return
        draft_id = (self.draft or {}).get("draft_id")
        if not draft_id:
            return

        try:
            # Re-read the draft object until the order is known; it is
            # randomised at draft start, so this is how we learn our slot.
            if not self._slot_known():
                draft = sleeper.get_draft(draft_id)
                if draft:
                    with self.lock:
                        previous = (self.draft or {}).get("status")
                        self.draft = draft
                        if draft.get("status") != previous:
                            self.log("draft status: %s" % draft.get("status"))
                        if self._slot_known():
                            self.log("DRAFT SLOT DETECTED: %s" % self.my_slot())

            picks = sleeper.get_picks(draft_id)
            with self.lock:
                before = len(self.picks)
                self.picks = picks or []
                self.last_poll = time.time()
                self.last_poll_ok = True
                self.poll_failures = 0
                if len(self.picks) != before:
                    self.log("picks: %d (was %d)" % (len(self.picks), before))
        except sleeper.SleeperError as exc:
            with self.lock:
                self.last_poll = time.time()
                self.last_poll_ok = False
                self.poll_failures += 1
                if self.poll_failures in (3, 10, 30):
                    self.log("picks endpoint failing (%d in a row): %s"
                             % (self.poll_failures, exc))

    def poll_loop(self):
        while self.running:
            try:
                self.poll_once()
                self.recompute()
            except Exception:  # noqa: BLE001 - the loop must never die
                traceback.print_exc()
            time.sleep(self._poll_interval())

    def _poll_interval(self):
        """Poll harder as your turn approaches - those seconds are the ones
        you actually feel under a two-minute clock."""
        try:
            until = (self.snapshot.get("me") or {}).get("picks_until_me")
        except AttributeError:
            until = None
        if until is not None and until <= config.NEAR_TURN_PICKS:
            return config.POLL_SECONDS_NEAR_TURN
        return config.POLL_SECONDS

    # -- derived state ----------------------------------------------------

    def my_slot(self):
        if self.practice:
            return self.practice.my_slot
        if self.slot_override:
            return int(self.slot_override)
        user_id = (self.user or {}).get("user_id")
        return draftstate.find_my_slot(self.draft, user_id)

    def _slot_known(self):
        return self.my_slot() is not None

    def draft_shape(self):
        draft = self.draft or {}
        settings = draft.get("settings") or {}
        rounds = int(settings.get("rounds") or config.ROUNDS)

        # A wrong round count is silently catastrophic: it would compute only a
        # few of my pick numbers and think the draft ends early. Never accept a
        # count that cannot seat a legal roster without saying so loudly.
        override = self.rounds_override or config.ROUNDS_OVERRIDE
        if override:
            rounds = int(override)
        elif rounds < config.ROSTER_SIZE:
            message = (
                "The draft object says %d rounds, but your roster needs %d "
                "players. Using %d. If the live draft really is %d rounds, "
                "restart with --rounds %d."
                % (rounds, config.ROSTER_SIZE, config.ROUNDS, rounds, rounds))
            if message not in self.warnings:
                self.warnings.append(message)
                self.log(message)
            rounds = config.ROUNDS

        return {
            "teams": int(settings.get("teams") or config.TEAMS),
            "rounds": rounds,
            "type": draft.get("type") or config.DRAFT_TYPE,
            "reversal_round": int(settings.get("reversal_round") or 0),
        }

    def recompute(self):
        """Rebuild the board and the recommendation from current picks."""
        with self.lock:
            if not self.players:
                # No player database. Still hand the UI something so it can show
                # the reason in red rather than spinning on "waiting for data".
                self.snapshot = {
                    "ok": False,
                    "fatal": True,
                    "errors": self.errors or [
                        "No player database on disk. Run: python3 build_data.py"],
                    "notes": self.notes[-12:],
                    "generated_at": time.time(),
                }
                return

            shape = self.draft_shape()
            analysis = draftstate.analyze(
                self.picks, self.players, shape["teams"], shape["type"],
                shape["reversal_round"])

            taken = set(analysis["taken"])
            taken.update(p["player_id"] for p in self.manual_taken)

            current_pick = analysis["pick_count"] + 1
            total_picks = shape["teams"] * shape["rounds"]
            current_pick = min(current_pick, total_picks)
            current_round, on_clock_slot = draftstate.slot_of_pick(
                current_pick, shape["teams"], shape["type"], shape["reversal_round"])

            slot = self.my_slot()
            if slot:
                mine = draftstate.my_picks(slot, shape["teams"], shape["rounds"],
                                           shape["type"], shape["reversal_round"])
            else:
                mine = []

            remaining = [p for p in mine if p >= current_pick]
            my_next_pick = remaining[1] if len(remaining) > 1 else None
            on_the_clock = bool(slot and remaining and remaining[0] == current_pick)
            picks_until_me = (remaining[0] - current_pick) if remaining else None

            my_roster_players = analysis["roster_players"].get(slot, []) if slot else []
            for player in my_roster_players:
                full = self.players.get(player["player_id"]) or {}
                player["bye"] = (self.projections.get(player["player_id"], {}).get("bye")
                                 or self.byes.get(full.get("team")))
            for player in self.manual_taken:
                if player.get("mine"):
                    my_roster_players.append(player)

            # Rebuild the board when the round changes: the risk profile shifts
            # from floor-weighted to upside-weighted at the crossover round.
            if self.board_round != current_round or not self.board:
                self.board = valuation.build_board(
                    self.players, self.projections, self.adp,
                    self.scoring_settings, current_round, self.byes)
                self.board_round = current_round

            slots_before = draftstate.slots_between(
                current_pick, remaining[0] if remaining else None,
                shape["teams"], shape["type"], shape["reversal_round"])
            opp_needs = draftstate.opponent_needs(analysis["rosters"], slots_before)

            my_positions = [p["position"] for p in my_roster_players]
            needs = valuation.roster_needs(my_positions)
            picks_left = len(remaining)

            state = {
                "taken": taken,
                "round": current_round,
                "current_pick": current_pick,
                "my_next_pick": my_next_pick if my_next_pick else (
                    remaining[1] if len(remaining) > 1 else None),
                "picks_until_next": ((my_next_pick - current_pick)
                                     if my_next_pick else None),
                "my_roster": my_positions,
                # Full records, so the engine can tell whose backup a player is.
                "my_players": [{"player_id": p.get("player_id"),
                                "position": p.get("position"),
                                "team": p.get("team")} for p in my_roster_players],
                "my_remaining_picks": remaining,
                "needs": needs,
                "picks_left": max(picks_left, 1),
                "opponent_needs": opp_needs,
            }

            recommendation = valuation.recommend(self.board, state)
            queue = valuation.build_queue(self.board, state)

            pool = [self._pool_entry(p, taken) for p in self.board[:320]]
            board_grid = self._board_grid(analysis, shape)

            self.snapshot = {
                "ok": True,
                "generated_at": time.time(),
                "league": {
                    "name": (self.league or {}).get("name"),
                    "league_id": (self.league or {}).get("league_id"),
                    "draft_id": (self.draft or {}).get("draft_id"),
                    "status": (self.draft or {}).get("status") or "unknown",
                    "teams": shape["teams"],
                    "rounds": shape["rounds"],
                    "type": shape["type"],
                },
                "me": {
                    "username": config.USERNAME,
                    "slot": slot,
                    "slot_known": slot is not None,
                    "my_picks": mine,
                    "remaining_picks": remaining,
                    "next_two": remaining[:2],
                    "on_the_clock": on_the_clock,
                    "picks_until_me": picks_until_me,
                },
                "draft": {
                    "current_pick": current_pick,
                    "round": current_round,
                    "on_clock_slot": on_clock_slot,
                    "on_clock_name": self._manager_name(on_clock_slot, analysis),
                    "total_picks": total_picks,
                    "picks_made": analysis["pick_count"],
                },
                "recommendation": self._serialise_recommendation(recommendation),
                "roster": draftstate.roster_report(my_roster_players),
                "roster_players": my_roster_players,
                # Roster-completion warnings are meaningless until the draft
                # order exists: with no slot I own no picks, which is not the
                # same as having run out of them.
                "warnings": (self.warnings + (
                    draftstate.imbalance_warnings(my_roster_players, picks_left)
                    if slot else [])),
                "bye_conflicts": draftstate.bye_conflicts(my_roster_players, self.byes),
                "runs": draftstate.detect_run(analysis["history"]),
                "cliffs": draftstate.tier_cliff_alerts(self.board, taken),
                "value_board": [self._pool_entry(p, taken)
                                for p in valuation.value_board(self.board, state)],
                "queue": [{"name": p["name"], "position": p["position"],
                           "team": p.get("team"), "player_id": p["player_id"]}
                          for p in queue],
                "pool": pool,
                "board_grid": board_grid,
                "opponent_needs": opp_needs,
                "history": analysis["history"][-12:],
                "manual_taken": self.manual_taken,
                "practice": (self.practice.status() if self.practice
                             else {"active": False}),
                "simulation": dict(self.sim),
                "errors": self.errors,
                "notes": self.notes[-12:],
                "data": {
                    "players": len(self.players),
                    "projections": len(self.projections),
                    "adp": len(self.adp),
                    "adp_is_estimate": not bool(self.adp),
                    "scoring_source": ("live league settings"
                                       if self.scoring_settings else "config.py"),
                    "last_poll": self.last_poll,
                    "last_poll_ok": self.last_poll_ok,
                    "poll_failures": self.poll_failures,
                    "offline": self.offline,
                },
            }

    def _manager_name(self, slot, analysis):
        if not slot:
            return None
        for user_id, user_slot in analysis["slot_by_user"].items():
            if user_slot == slot:
                return self.league_users.get(user_id, "Team %s" % slot)
        order = (self.draft or {}).get("draft_order") or {}
        for user_id, user_slot in order.items():
            if int(user_slot) == int(slot):
                return self.league_users.get(user_id, "Team %s" % slot)
        return "Team %s" % slot

    def _pool_entry(self, player, taken):
        return {
            "player_id": player["player_id"],
            "name": player["name"],
            "position": player["position"],
            "team": player.get("team"),
            "bye": player.get("bye"),
            "points": player.get("points"),
            "vor": player.get("vor"),
            "adj_vor": player.get("adj_vor"),
            "adp": player.get("adp"),
            "adp_source": player.get("adp_source"),
            "value_gap": player.get("value_gap"),
            "tier": player.get("tier"),
            "pos_label": player.get("pos_label"),
            "injury_status": player.get("injury_status"),
            "estimated": player.get("estimated"),
            "drafted": player["player_id"] in taken,
        }

    def _serialise_recommendation(self, recommendation):
        def entry(item):
            player = item["player"]
            return {
                "player": self._pool_entry(player, set()),
                "take_now": item["take_now"],
                "wait": item["wait"],
                "edge": item["edge"],
                "p_survive": item["p_survive"],
                "reason": item.get("reason", ""),
                "tier_break": item.get("tier_break", False),
                "next_tier_size": player.get("next_tier_size", 0),
                "next_tier_drop": player.get("next_tier_drop", 0),
                "cost_vs_top": item.get("cost_vs_top", 0),
                "risk_reasons": player.get("risk_reasons", []),
                "points_source": player.get("points_source"),
            }

        if not recommendation.get("top"):
            return {"top": None, "alternatives": [], "delta_to_next": 0}
        return {
            "top": entry(recommendation["top"]),
            "alternatives": [entry(a) for a in recommendation["alternatives"]],
            "delta_to_next": recommendation.get("delta_to_next", 0),
            "close_call": recommendation.get("close_call", False),
        }

    def _board_grid(self, analysis, shape):
        """One column per manager, every pick made, plus their positional needs."""
        grid = []
        for slot in range(1, shape["teams"] + 1):
            roster = analysis["roster_players"].get(slot, [])
            needs = valuation.roster_needs([p["position"] for p in roster])
            grid.append({
                "slot": slot,
                "name": self._manager_name(slot, analysis),
                "is_me": slot == self.my_slot(),
                "picks": roster,
                "needs": [pos for pos, n in needs.items() if n > 0 and pos != "FLEX"],
                "counts": {pos: sum(1 for p in roster if p["position"] == pos)
                           for pos in ("QB", "RB", "WR", "TE", "K", "DEF")},
            })
        return grid

    # -- mutations --------------------------------------------------------

    def mark_taken(self, player_id, mine=False):
        with self.lock:
            player = self.players.get(str(player_id))
            if not player:
                return False, "unknown player"
            if any(p["player_id"] == str(player_id) for p in self.manual_taken):
                return False, "already marked"
            self.manual_taken.append({
                "player_id": str(player_id),
                "name": player.get("name"),
                "position": player.get("position"),
                "team": player.get("team"),
                "mine": bool(mine),
            })
            self.log("manually marked %s%s" % (player.get("name"),
                                               " (mine)" if mine else ""))
        self.recompute()
        return True, "ok"

    def undo_manual(self):
        with self.lock:
            if not self.manual_taken:
                return False, "nothing to undo"
            removed = self.manual_taken.pop()
            self.log("undid manual mark: %s" % removed.get("name"))
        self.recompute()
        return True, removed.get("name")

    def load_projection_paste(self, text):
        with self.lock:
            if not self.players:
                return {"error": "no player database loaded"}
            parsed, report = paste.apply_projection_paste(text, self.players, self.index)
            if not parsed:
                return {"error": "nothing matched", "report": report}
            self.projections.update(parsed)
            sleeper.cache_write(config.PROJECTIONS_CACHE, {
                "players": self.projections,
                "source": "paste",
                "updated_at": time.time(),
            })
            self.board_round = None  # force a rebuild
            self.warnings = [w for w in self.warnings
                             if not w.startswith("No projections")]
            self.log("projections pasted: %d matched, %d unmatched"
                     % (report["matched"], report["unmatched_count"]))
        self.recompute()
        return {"ok": True, "report": report}

    def load_adp_paste(self, text):
        with self.lock:
            if not self.players:
                return {"error": "no player database loaded"}
            parsed, report = paste.apply_adp_paste(text, self.players, self.index)
            if not parsed:
                return {"error": "nothing matched", "report": report}
            self.adp.update(parsed)
            byes = {}
            for pid, rec in parsed.items():
                if rec.get("bye"):
                    team = (self.players.get(pid) or {}).get("team")
                    if team:
                        byes[team] = rec["bye"]
            self.byes.update(byes)
            sleeper.cache_write(config.ADP_CACHE, {
                "players": self.adp, "byes": self.byes,
                "source": "paste", "updated_at": time.time(),
            })
            self.board_round = None
            self.warnings = [w for w in self.warnings
                             if not w.startswith("No pasted ADP")]
            self.log("adp pasted: %d matched, %d unmatched"
                     % (report["matched"], report["unmatched_count"]))
        self.recompute()
        return {"ok": True, "report": report}

    def start_simulation(self, runs=500, player_ids=None):
        """Kick off a forward mock-draft simulation in the background.

        It runs off a snapshot of the board so the 3-second poll loop keeps
        updating while it works - the draft never waits on the simulation.
        """
        with self.lock:
            if self.sim.get("status") == "running":
                return {"ok": False, "error": "A simulation is already running."}
            if not self.board:
                return {"ok": False, "error": "No player data loaded."}

            snapshot = self.snapshot or {}
            slot = self.my_slot()
            if not slot:
                return {"ok": False,
                        "error": "Your draft slot is not known yet. Set it in the "
                                 "Data panel to simulate before the draft starts."}

            recommendation = snapshot.get("recommendation") or {}
            if not player_ids:
                player_ids = []
                if recommendation.get("top"):
                    player_ids.append(recommendation["top"]["player"]["player_id"])
                for alt in recommendation.get("alternatives", [])[:3]:
                    player_ids.append(alt["player"]["player_id"])
            player_ids = [str(p) for p in player_ids][:6]
            if not player_ids:
                return {"ok": False, "error": "No candidates to compare."}

            runs = max(25, min(int(runs or 500), 2000))

            # Copy the board so a mid-run rebuild cannot shift it underneath us.
            board_copy = [dict(p) for p in self.board]
            shape = self.draft_shape()
            analysis = draftstate.analyze(
                self.picks, self.players, shape["teams"], shape["type"],
                shape["reversal_round"])
            taken = set(analysis["taken"])
            taken.update(p["player_id"] for p in self.manual_taken)
            state = {
                "taken": taken,
                "current_pick": snapshot.get("draft", {}).get("current_pick", 1),
            }
            my_players = list(snapshot.get("roster_players") or [])
            for player in my_players:
                full = next((p for p in board_copy
                             if p["player_id"] == player["player_id"]), None)
                player["points"] = (full or {}).get("points") or 0.0
            opponent_rosters = analysis["rosters"]

            self.sim = {"status": "running", "runs": runs, "done": 0,
                        "total": runs * len(player_ids),
                        "started_at": time.time()}

        def progress(done, total):
            with self.lock:
                if self.sim.get("status") == "running":
                    self.sim["done"] = done
                    self.sim["total"] = total

        def worker():
            started = time.time()
            try:
                result = simulation.run(
                    board_copy, state, shape, slot, opponent_rosters, my_players,
                    player_ids, runs=runs, progress=progress)
            except Exception as exc:  # noqa: BLE001 - report, never crash the app
                traceback.print_exc()
                with self.lock:
                    self.sim = {"status": "error", "error": str(exc)}
                return
            with self.lock:
                if result.get("error"):
                    self.sim = {"status": "error", "error": result["error"]}
                else:
                    result["status"] = "done"
                    result["duration"] = round(time.time() - started, 2)
                    self.sim = result
                self.log("simulation finished in %.1fs" % (time.time() - started))
            self.recompute()

        self.sim_thread = threading.Thread(target=worker, daemon=True)
        self.sim_thread.start()
        # Refresh the snapshot now so the UI shows "running" on the next poll
        # rather than waiting up to a full poll cycle for the status to appear.
        self.recompute()
        return {"ok": True, "started": True, "runs": runs,
                "candidates": len(player_ids)}

    def list_drafts(self):
        """Every draft on the account: league drafts and mock drafts alike.

        The Sleeper phone app has no address bar, so there is no link to copy.
        This lets the page list what you are actually in and follow one with a
        click, with nothing to type.
        """
        if self.offline:
            return {"ok": False, "error": "Running offline - cannot ask Sleeper."}
        try:
            user = self.user or sleeper.get_user()
            leagues = sleeper.get_leagues(user["user_id"]) or []
            league_names = {str(l.get("league_id")): l.get("name")
                            for l in leagues}

            found, seen = [], set()

            def add(draft, league_name=None):
                draft_id = str((draft or {}).get("draft_id") or "")
                if not draft_id or draft_id in seen:
                    return
                seen.add(draft_id)
                settings = draft.get("settings") or {}
                league_id = str(draft.get("league_id") or "")
                name = league_name or league_names.get(league_id)
                found.append({
                    "draft_id": draft_id,
                    "status": draft.get("status"),
                    "type": draft.get("type"),
                    "teams": settings.get("teams"),
                    "rounds": settings.get("rounds"),
                    "league": name,
                    "is_mock": not name,
                    "current": str(draft_id) == str(
                        (self.draft or {}).get("draft_id")),
                })

            for draft in (sleeper.get_user_drafts(user["user_id"]) or []):
                add(draft)
            for league in leagues:
                for draft in (sleeper.get_drafts(league["league_id"]) or []):
                    add(draft, league.get("name"))

            rank = {"drafting": 0, "paused": 1, "pre_draft": 2, "complete": 3}
            found.sort(key=lambda d: (rank.get(d["status"], 4),
                                      0 if d["is_mock"] else 1))
            return {"ok": True, "drafts": found}
        except sleeper.SleeperError as exc:
            return {"ok": False, "error": str(exc)}

    def follow_draft(self, draft_id):
        """Watch any Sleeper draft by id - a mock, or the real one.

        This is how you rehearse the way you will actually play: pick in the
        Sleeper app, and watch every pick appear here. Pass nothing to go back
        to your league's own draft.
        """
        raw = str(draft_id or "").strip()
        # Accept a full draft address as well as a bare id.
        match = re.search(r"(\d{6,})", raw)
        draft_id = match.group(1) if match else None

        with self.lock:
            self.practice = None
            self.draft_id_override = draft_id
            self.picks = []
            self.manual_taken = []
            self.warnings = [w for w in self.warnings
                             if not w.startswith("PRACTICE")]
            self.errors = []
            self.draft = None

        self.resolve_league()
        self.poll_once()
        self.recompute()

        with self.lock:
            current = (self.draft or {}).get("draft_id")
            if draft_id and str(current) != str(draft_id):
                return {"ok": False,
                        "error": "Sleeper does not have a draft with id %s. "
                                 "Check the number in the draft's web address."
                                 % draft_id}
            self.log("now following draft %s" % (current or "league default"))
            return {"ok": True, "draft_id": current,
                    "following_other": bool(draft_id)}

    def start_practice(self, slot=None, speed=None):
        """Begin a practice draft from the browser - no terminal needed."""
        with self.lock:
            if self.practice:
                return {"ok": False, "error": "A practice draft is already running."}
            if not self.board:
                return {"ok": False, "error": "No player data loaded."}
            # `is not None`, not truthiness: a speed of 0 is a valid request
            # for "as fast as possible" and must not fall back to the default.
            self.practice = practice_mode.PracticeDraft(
                my_slot=int(slot) if slot else None,
                seconds_per_pick=(float(speed) if speed is not None
                                  else practice_mode.DEFAULT_PICK_SECONDS))
            self.manual_taken = []
            self.picks = []
            self.warnings.append(
                "PRACTICE DRAFT — you are drafting from slot %d against 11 "
                "simulated managers. None of this is real. Press End practice "
                "to go back to your live draft." % self.practice.my_slot)
            self.log("practice draft started from slot %d" % self.practice.my_slot)
        self.poll_once()
        self.recompute()
        return {"ok": True, "slot": self.practice.my_slot}

    def stop_practice(self):
        """End practice and go back to watching the real draft."""
        with self.lock:
            if not self.practice:
                return {"ok": False, "error": "Not in practice mode."}
            self.practice = None
            self.picks = []
            self.manual_taken = []
            self.warnings = [w for w in self.warnings
                             if not w.startswith("PRACTICE DRAFT")]
            self.draft = None
            self.log("practice draft ended - back to the live draft")
        # Re-read the real league and draft from Sleeper.
        self.resolve_league()
        self.poll_once()
        self.recompute()
        return {"ok": True}

    def practice_draft(self, player_id):
        """Make my pick in the practice draft."""
        with self.lock:
            if not self.practice:
                return {"ok": False, "error": "not in practice mode"}
            board = self.board or []
            ok, detail = self.practice.draft(player_id, board)
            if ok:
                name = next((p["name"] for p in board
                             if p["player_id"] == str(player_id)), player_id)
                self.log("practice: you drafted %s" % name)
        self.poll_once()
        self.recompute()
        return {"ok": ok, "detail": detail}

    def set_slot(self, slot):
        with self.lock:
            self.slot_override = int(slot) if slot else None
            self.log("draft slot manually set to %s" % self.slot_override)
        self.recompute()
        return True


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    assistant = None
    server_version = "DraftAssistant/1.0"

    def log_message(self, fmt, *args):
        pass  # the poll loop would otherwise flood the console

    def _send(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, default=str))

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"text": raw.decode("utf-8", "replace")}

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                page = os.path.join(config.TEMPLATE_DIR, "index.html")
                with open(page, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                with self.assistant.lock:
                    snapshot = self.assistant.snapshot
                if not snapshot:
                    self.assistant.recompute()
                    with self.assistant.lock:
                        snapshot = self.assistant.snapshot
                return self._json(snapshot or {"ok": False, "error": "no data yet"})
            if path == "/api/health":
                return self._json({"ok": True, "time": time.time()})
            if path == "/api/drafts":
                return self._json(self.assistant.list_drafts())
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError:
            return self._json({"error": "templates/index.html is missing"}, 500)
        except Exception as exc:  # noqa: BLE001 - never 500 silently mid-draft
            traceback.print_exc()
            return self._json({"error": str(exc)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
            if path == "/api/paste/projections":
                return self._json(
                    self.assistant.load_projection_paste(body.get("text", "")))
            if path == "/api/paste/adp":
                return self._json(self.assistant.load_adp_paste(body.get("text", "")))
            if path == "/api/mark":
                ok, detail = self.assistant.mark_taken(
                    body.get("player_id"), body.get("mine", False))
                return self._json({"ok": ok, "detail": detail})
            if path == "/api/undo":
                ok, detail = self.assistant.undo_manual()
                return self._json({"ok": ok, "detail": detail})
            if path == "/api/follow":
                return self._json(
                    self.assistant.follow_draft(body.get("draft_id")))
            if path == "/api/practice/start":
                return self._json(self.assistant.start_practice(
                    slot=body.get("slot"), speed=body.get("speed")))
            if path == "/api/practice/stop":
                return self._json(self.assistant.stop_practice())
            if path == "/api/practice/draft":
                return self._json(
                    self.assistant.practice_draft(body.get("player_id")))
            if path == "/api/simulate":
                return self._json(self.assistant.start_simulation(
                    runs=body.get("runs", 500),
                    player_ids=body.get("player_ids")))
            if path == "/api/slot":
                self.assistant.set_slot(body.get("slot"))
                return self._json({"ok": True})
            if path == "/api/refresh":
                self.assistant.resolve_league()
                self.assistant.poll_once()
                self.assistant.recompute()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": str(exc)}, 500)


def main():
    parser = argparse.ArgumentParser(description="Sleeper Draft Assistant")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--offline", action="store_true",
                        help="Do not call Sleeper; run entirely off cache.")
    parser.add_argument("--slot", type=int, default=None,
                        help="Force your draft slot (1-12) if auto-detection fails.")
    parser.add_argument("--draft-id", default=None,
                        help="Use a specific draft if the league has several.")
    parser.add_argument("--rounds", type=int, default=None,
                        help="Force the round count if the draft object is wrong.")
    parser.add_argument("--practice", action="store_true",
                        help="Rehearse against 11 simulated managers. Needs no "
                             "Sleeper draft at all.")
    parser.add_argument("--practice-slot", type=int, default=None,
                        help="Draft slot to rehearse from (default: random).")
    parser.add_argument("--practice-speed", type=float, default=None,
                        help="Seconds each simulated manager takes (default 3).")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    practice = None
    if args.practice:
        practice = practice_mode.PracticeDraft(
            my_slot=args.practice_slot,
            seconds_per_pick=args.practice_speed
            or practice_mode.DEFAULT_PICK_SECONDS)

    assistant = Assistant(offline=args.offline, slot_override=args.slot,
                          draft_id_override=args.draft_id,
                          rounds_override=args.rounds,
                          practice=practice)
    assistant.load_cached_data()
    if practice:
        # Still resolve the league, because the live scoring settings are what
        # make the rehearsal a rehearsal rather than a different game.
        assistant.resolve_league()
    else:
        assistant.resolve_league()
    assistant.poll_once()
    assistant.recompute()

    Handler.assistant = assistant

    # Never die because a port is busy - a leftover copy of this app, or
    # anything else on 8000, must not stop you drafting. Walk up until one
    # binds and tell the user which it took.
    server = None
    port = args.port
    for candidate in range(args.port, args.port + 12):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print("\n  Could not open any port between %d and %d."
              % (args.port, args.port + 11))
        print("  Something else is using them. Try:  python3 app.py --port 9000")
        return
    if port != args.port:
        print("\n  Port %d was busy - using %d instead." % (args.port, port))

    poller = threading.Thread(target=assistant.poll_loop, daemon=True)
    poller.start()

    url = "http://localhost:%d" % port
    print("\n" + "=" * 62)
    if practice:
        print("  PRACTICE DRAFT — REHEARSAL ONLY, NOTHING IS REAL")
        print("  You are slot %d of %d. Click DRAFT HIM to make your pick."
              % (practice.my_slot, config.TEAMS))
    else:
        print("  SLEEPER DRAFT ASSISTANT IS RUNNING")
    print("  %s" % url)
    print("=" * 62)
    print("  players=%d projections=%d adp=%d"
          % (len(assistant.players), len(assistant.projections), len(assistant.adp)))
    if assistant.errors:
        print("\n  PROBLEMS:")
        for err in assistant.errors:
            print("   - %s" % err)
    print("\n  >> KEEP THIS TERMINAL WINDOW OPEN FOR THE WHOLE DRAFT. <<")
    print("     Closing it, or pressing Ctrl-C, stops the app and the page")
    print("     goes blank. Nothing is lost - just start it again.\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        assistant.running = False
        server.server_close()


if __name__ == "__main__":
    main()
