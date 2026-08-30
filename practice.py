"""Built-in practice draft, so rehearsal never depends on an outside service.

A Sleeper mock draft lives in Sleeper's mock lobby and is not always reachable
through the public API. Rehearsal is too important to leave to that, so this
runs a complete 12-team snake draft locally: eleven simulated managers pick on
a timer off real ADP, and you pick by clicking in the app.

It produces picks in exactly the shape Sleeper's API returns, so the rest of
the application cannot tell the difference. Rehearsing here exercises the same
code path that runs tonight.
"""

import random
import time

import config
import draftstate

# How long a simulated manager takes to pick, in seconds. Quick enough that a
# full draft is rehearsable in minutes, slow enough to feel like a real room.
DEFAULT_PICK_SECONDS = 3.0

OPPONENT_CAPS = {"QB": 2, "RB": 7, "WR": 8, "TE": 3, "K": 1, "DEF": 1}
OPPONENT_KDEF_ROUND = 13
OPPONENT_SECOND_QB_ROUND = 10


class PracticeDraft:
    """A local stand-in for a live Sleeper draft."""

    def __init__(self, my_slot=None, teams=None, rounds=None,
                 seconds_per_pick=DEFAULT_PICK_SECONDS, seed=None):
        self.teams = teams or config.TEAMS
        self.rounds = rounds or config.ROUNDS
        self.total_picks = self.teams * self.rounds
        self.seconds_per_pick = seconds_per_pick
        self.rng = random.Random(seed)
        self.my_slot = my_slot or self.rng.randint(1, self.teams)

        self.picks = []
        self.next_pick_at = time.time() + 1.5
        self.started_at = time.time()
        self.waiting_for_me = False

    # -- Sleeper-shaped objects -------------------------------------------

    def draft_object(self, user_id):
        """The same shape the real draft endpoint returns."""
        order = {str(user_id): self.my_slot}
        for slot in range(1, self.teams + 1):
            if slot != self.my_slot:
                order["practice-manager-%d" % slot] = slot
        return {
            "draft_id": "practice",
            "status": "complete" if self.is_over() else "drafting",
            "type": "snake",
            "settings": {"teams": self.teams, "rounds": self.rounds,
                         "reversal_round": 0, "pick_timer": 120},
            "draft_order": order,
            "slot_to_roster_id": {str(s): s for s in range(1, self.teams + 1)},
            "league_id": None,
            "season": config.SEASON,
        }

    def manager_names(self, user_id, username):
        names = {str(user_id): username}
        for slot in range(1, self.teams + 1):
            if slot != self.my_slot:
                names["practice-manager-%d" % slot] = "Manager %d" % slot
        return names

    # -- state -------------------------------------------------------------

    def is_over(self):
        return len(self.picks) >= self.total_picks

    def current_pick(self):
        return len(self.picks) + 1

    def on_the_clock(self):
        if self.is_over():
            return None, None
        return draftstate.slot_of_pick(self.current_pick(), self.teams)

    def my_turn(self):
        _, slot = self.on_the_clock()
        return slot == self.my_slot

    def seconds_until_next(self):
        if self.my_turn() or self.is_over():
            return None
        return max(0.0, self.next_pick_at - time.time())

    # -- driving the draft --------------------------------------------------

    def tick(self, board, taken=None):
        """Let the simulated managers make any picks that are due.

        Stops as soon as it is my turn and waits there indefinitely - a
        rehearsal you cannot lose by thinking too long.
        """
        taken = set(taken or ())
        taken.update(p["player_id"] for p in self.picks)
        made = 0

        while not self.is_over():
            round_no, slot = self.on_the_clock()
            if slot == self.my_slot:
                self.waiting_for_me = True
                break
            self.waiting_for_me = False
            if time.time() < self.next_pick_at:
                break

            choice = self._opponent_choice(board, taken, slot, round_no)
            if not choice:
                break
            self._append(choice, slot, round_no,
                         "practice-manager-%d" % slot)
            taken.add(choice)
            made += 1
            self.next_pick_at = time.time() + self.seconds_per_pick
        return made

    def draft(self, player_id, board):
        """Make my pick. Returns (ok, detail)."""
        if self.is_over():
            return False, "the practice draft is finished"
        round_no, slot = self.on_the_clock()
        if slot != self.my_slot:
            return False, "it is not your turn yet"
        player_id = str(player_id)
        if any(p["player_id"] == player_id for p in self.picks):
            return False, "already drafted"
        if not any(p["player_id"] == player_id for p in board):
            return False, "unknown player"

        self._append(player_id, slot, round_no, "me")
        self.waiting_for_me = False
        self.next_pick_at = time.time() + self.seconds_per_pick
        return True, "drafted"

    def _append(self, player_id, slot, round_no, picked_by):
        self.picks.append({
            "pick_no": len(self.picks) + 1,
            "round": round_no,
            "draft_slot": slot,
            "player_id": str(player_id),
            "picked_by": picked_by,
            "metadata": {},
        })

    def _opponent_choice(self, board, taken, slot, round_no):
        """A plausible rival: near ADP, one QB, kickers and defenses last."""
        roster = [p["position"] for p in self._roster_of(slot, board)]
        counts = {}
        for position in roster:
            counts[position] = counts.get(position, 0) + 1

        pool = []
        for player in board:
            pid = player["player_id"]
            if pid in taken:
                continue
            position = player["position"]
            if counts.get(position, 0) >= OPPONENT_CAPS.get(position, 6):
                continue
            if position in ("K", "DEF") and round_no < OPPONENT_KDEF_ROUND:
                continue
            if (position == "QB" and counts.get("QB", 0) >= 1
                    and round_no < OPPONENT_SECOND_QB_ROUND):
                continue
            adp = player.get("adp")
            if adp is None:
                continue
            pool.append((adp + self.rng.gauss(0.0, max(2.0, adp * 0.12)), pid))

        if not pool:
            for player in board:
                if player["player_id"] not in taken:
                    return player["player_id"]
            return None
        pool.sort()
        return pool[0][1]

    def _roster_of(self, slot, board):
        by_id = {p["player_id"]: p for p in board}
        return [by_id[p["player_id"]] for p in self.picks
                if p["draft_slot"] == slot and p["player_id"] in by_id]

    def status(self):
        return {
            "active": True,
            "my_slot": self.my_slot,
            "current_pick": self.current_pick(),
            "total_picks": self.total_picks,
            "my_turn": self.my_turn() and not self.is_over(),
            "over": self.is_over(),
            "seconds_until_next": self.seconds_until_next(),
            "seconds_per_pick": self.seconds_per_pick,
        }
