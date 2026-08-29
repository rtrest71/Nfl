"""Paste-box parsing and player-name matching.

This is the mandatory fallback from spec section 3: if the Sleeper projections
endpoint shape surprises us, the user copies a table out of FantasyPros, ESPN
or Yahoo, pastes it in, and the app keeps working. It parses CSV, TSV and
copied-from-browser whitespace tables in any column order.

Name matching is the hard part. "Marvin Harrison Jr.", "Marvin Harrison Jr",
"M. Harrison", "Harrison, Marvin" and "MarvinHarrisonJr" all have to land on
the same Sleeper player_id, and "San Francisco 49ers D/ST" has to land on "SF".
"""

import csv
import difflib
import io
import re

import scoring

# ---------------------------------------------------------------------------
# Team names -> Sleeper abbreviations (defense player_ids are the abbreviation)
# ---------------------------------------------------------------------------
TEAM_ABBR = {
    "arizona": "ARI", "cardinals": "ARI", "arizona cardinals": "ARI",
    "atlanta": "ATL", "falcons": "ATL", "atlanta falcons": "ATL",
    "baltimore": "BAL", "ravens": "BAL", "baltimore ravens": "BAL",
    "buffalo": "BUF", "bills": "BUF", "buffalo bills": "BUF",
    "carolina": "CAR", "panthers": "CAR", "carolina panthers": "CAR",
    "chicago": "CHI", "bears": "CHI", "chicago bears": "CHI",
    "cincinnati": "CIN", "bengals": "CIN", "cincinnati bengals": "CIN",
    "cleveland": "CLE", "browns": "CLE", "cleveland browns": "CLE",
    "dallas": "DAL", "cowboys": "DAL", "dallas cowboys": "DAL",
    "denver": "DEN", "broncos": "DEN", "denver broncos": "DEN",
    "detroit": "DET", "lions": "DET", "detroit lions": "DET",
    "green bay": "GB", "packers": "GB", "green bay packers": "GB", "greenbay": "GB",
    "houston": "HOU", "texans": "HOU", "houston texans": "HOU",
    "indianapolis": "IND", "colts": "IND", "indianapolis colts": "IND",
    "jacksonville": "JAX", "jaguars": "JAX", "jacksonville jaguars": "JAX",
    "kansas city": "KC", "chiefs": "KC", "kansas city chiefs": "KC", "kansascity": "KC",
    "las vegas": "LV", "raiders": "LV", "las vegas raiders": "LV", "oakland": "LV",
    "los angeles chargers": "LAC", "chargers": "LAC", "la chargers": "LAC",
    "los angeles rams": "LAR", "rams": "LAR", "la rams": "LAR",
    "miami": "MIA", "dolphins": "MIA", "miami dolphins": "MIA",
    "minnesota": "MIN", "vikings": "MIN", "minnesota vikings": "MIN",
    "new england": "NE", "patriots": "NE", "new england patriots": "NE",
    "new orleans": "NO", "saints": "NO", "new orleans saints": "NO",
    "new york giants": "NYG", "giants": "NYG", "ny giants": "NYG",
    "new york jets": "NYJ", "jets": "NYJ", "ny jets": "NYJ",
    "philadelphia": "PHI", "eagles": "PHI", "philadelphia eagles": "PHI",
    "pittsburgh": "PIT", "steelers": "PIT", "pittsburgh steelers": "PIT",
    "san francisco": "SF", "49ers": "SF", "san francisco 49ers": "SF", "niners": "SF",
    "seattle": "SEA", "seahawks": "SEA", "seattle seahawks": "SEA",
    "tampa bay": "TB", "buccaneers": "TB", "tampa bay buccaneers": "TB", "bucs": "TB",
    "tennessee": "TEN", "titans": "TEN", "tennessee titans": "TEN",
    "washington": "WAS", "commanders": "WAS", "washington commanders": "WAS",
}

# Alternate abbreviations other sites use.
ABBR_ALIASES = {
    "JAC": "JAX", "WSH": "WAS", "LA": "LAR", "OAK": "LV", "SD": "LAC",
    "STL": "LAR", "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO",
    "SFO": "SF", "TAM": "TB", "LVR": "LV", "ARZ": "ARI", "BLT": "BAL",
    "CLV": "CLE", "HST": "HOU",
}

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

DEF_MARKERS = ("d/st", "dst", "def", "defense", "d st")

# Column header synonyms -> what the column means.
COLUMN_ROLES = {
    "player": "name", "name": "name", "player name": "name", "playername": "name",
    "full name": "name", "athlete": "name",
    "team": "team", "tm": "team", "nfl team": "team", "pro team": "team",
    "pos": "position", "position": "position", "pos.": "position",
    "adp": "adp", "avg pick": "adp", "average pick": "adp", "avg. pick": "adp",
    "adp ppr": "adp", "sleeper adp": "adp", "avg": "adp", "average": "adp",
    "rank": "rank", "rk": "rank", "#": "rank", "overall": "rank",
    "stdev": "adp_stdev", "std dev": "adp_stdev", "sd": "adp_stdev",
    "std": "adp_stdev", "std.dev": "adp_stdev",
    "bye": "bye", "bye week": "bye", "b": "bye",
    "fpts": "points", "points": "points", "proj": "points", "proj pts": "points",
    "fantasy points": "points", "projected points": "points", "pts": "points",
    "fpts/g": "ignore", "ppg": "ignore", "owned": "ignore", "%": "ignore",
}


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

def normalize_name(name):
    """Reduce a player name to a comparable key.

    Lowercases, drops punctuation and generational suffixes, collapses spaces.
    "Marvin Harrison Jr." and "marvin harrison" both become "marvin harrison".
    """
    if not name:
        return ""
    text = str(name).strip().lower()
    # "Harrison, Marvin" -> "Marvin Harrison"
    if "," in text and not any(m in text for m in DEF_MARKERS):
        head, _, tail = text.partition(",")
        if tail.strip():
            text = "%s %s" % (tail.strip(), head.strip())
    text = text.replace("&", " and ")
    text = re.sub(r"[.’'`\-_]", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    parts = [p for p in text.split() if p]
    while len(parts) > 1 and parts[-1] in NAME_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def is_defense(name, position=None):
    if (position or "").strip().upper() in ("DEF", "DST", "D/ST", "D"):
        return True
    low = " %s " % str(name or "").strip().lower()
    return any(m in low for m in DEF_MARKERS)


def defense_abbr(name):
    """Map any spelling of a team defense onto its Sleeper abbreviation."""
    raw = str(name or "").strip()
    upper = raw.upper()
    bare = re.sub(r"[^A-Z]", "", upper)
    if bare in ABBR_ALIASES:
        return ABBR_ALIASES[bare]
    if 2 <= len(bare) <= 3 and bare in set(TEAM_ABBR.values()):
        return bare

    low = raw.lower()
    for marker in ("d/st", "dst", "defense", " def", "d st"):
        low = low.replace(marker, " ")
    low = re.sub(r"[^a-z0-9 ]+", " ", low)
    low = " ".join(low.split())
    if low in TEAM_ABBR:
        return TEAM_ABBR[low]
    # Try the longest matching team token, so "san francisco 49ers" beats "49ers".
    best = None
    for key, abbr in TEAM_ABBR.items():
        if key in low and (best is None or len(key) > len(best[0])):
            best = (key, abbr)
    if best:
        return best[1]
    token = low.split()[0].upper() if low.split() else ""
    return ABBR_ALIASES.get(token, token if token in set(TEAM_ABBR.values()) else None)


def normalize_team(team):
    if not team:
        return None
    raw = str(team).strip().upper()
    raw = re.sub(r"[^A-Z0-9 ]+", "", raw)
    if raw in ABBR_ALIASES:
        return ABBR_ALIASES[raw]
    if raw in set(TEAM_ABBR.values()):
        return raw
    low = raw.lower()
    if low in TEAM_ABBR:
        return TEAM_ABBR[low]
    return raw or None


# ---------------------------------------------------------------------------
# Matching pasted rows onto Sleeper player_ids
# ---------------------------------------------------------------------------

class PlayerIndex:
    """Lookup structure over the Sleeper player database."""

    def __init__(self, players):
        self.players = players
        self.by_name = {}
        self.by_name_pos = {}
        self.by_name_team = {}
        self.by_initial = {}
        self.defenses = {}

        for pid, p in players.items():
            pos = (p.get("position") or "").upper()
            team = normalize_team(p.get("team"))
            if pos == "DEF":
                key = normalize_team(pid) or team
                if key:
                    self.defenses[key] = pid
                continue
            key = normalize_name(p.get("name"))
            if not key:
                continue
            self.by_name.setdefault(key, []).append(pid)
            self.by_name_pos.setdefault((key, pos), []).append(pid)
            if team:
                self.by_name_team.setdefault((key, team), []).append(pid)
            parts = key.split()
            if len(parts) >= 2:
                initial = "%s %s" % (parts[0][0], parts[-1])
                self.by_initial.setdefault((initial, pos), []).append(pid)
                self.by_initial.setdefault((initial, ""), []).append(pid)

        self._all_names = list(self.by_name)

    def match(self, name, position=None, team=None):
        """Return a player_id, or None. Never guesses between equal candidates."""
        pos = (position or "").strip().upper()
        if pos in ("DST", "D/ST", "D"):
            pos = "DEF"
        team = normalize_team(team)

        if is_defense(name, pos):
            abbr = defense_abbr(name) or team
            if abbr and abbr in self.defenses:
                return self.defenses[abbr]
            return None

        key = normalize_name(name)
        if not key:
            return None

        # Most specific first: name + team, then name + position, then name.
        for candidates in (
            self.by_name_team.get((key, team)) if team else None,
            self.by_name_pos.get((key, pos)) if pos else None,
            self.by_name.get(key),
        ):
            if candidates and len(candidates) == 1:
                return candidates[0]
            if candidates and len(candidates) > 1:
                narrowed = self._narrow(candidates, pos, team)
                if narrowed:
                    return narrowed

        # "P. Mahomes" style.
        parts = key.split()
        if len(parts) >= 2:
            initial = "%s %s" % (parts[0][0], parts[-1])
            for lookup_pos in (pos, ""):
                candidates = self.by_initial.get((initial, lookup_pos))
                if candidates and len(candidates) == 1:
                    return candidates[0]
                if candidates:
                    narrowed = self._narrow(candidates, pos, team)
                    if narrowed:
                        return narrowed

        # Last resort: close string match, but only a confident one.
        close = difflib.get_close_matches(key, self._all_names, n=1, cutoff=0.90)
        if close:
            candidates = self.by_name.get(close[0]) or []
            if len(candidates) == 1:
                return candidates[0]
            narrowed = self._narrow(candidates, pos, team)
            if narrowed:
                return narrowed
        return None

    def _narrow(self, candidates, pos, team):
        """Break a tie using position and team; give up if still ambiguous."""
        pool = candidates
        if pos:
            filtered = [c for c in pool
                        if (self.players[c].get("position") or "").upper() == pos]
            pool = filtered or pool
        if team:
            filtered = [c for c in pool
                        if normalize_team(self.players[c].get("team")) == team]
            pool = filtered or pool
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:
            # Prefer the player Sleeper itself ranks highest - that is nearly
            # always the relevant one when two players share a name.
            ranked = [c for c in pool if self.players[c].get("search_rank") is not None]
            if ranked:
                return min(ranked, key=lambda c: self.players[c]["search_rank"])
        return None


# ---------------------------------------------------------------------------
# Table parsing
# ---------------------------------------------------------------------------

def _split_rows(text):
    """Split pasted text into cells, sniffing the delimiter per the whole blob."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        return []
    lines = [ln for ln in text.split("\n") if ln.strip()]
    sample = "\n".join(lines[:20])

    if "\t" in sample:
        return [ln.split("\t") for ln in lines]

    comma_lines = sum(1 for ln in lines[:20] if ln.count(",") >= 2)
    if comma_lines >= max(2, len(lines[:20]) // 2):
        try:
            return [row for row in csv.reader(io.StringIO(text)) if row]
        except csv.Error:
            pass

    # Browser-copied tables: two-or-more spaces separate columns.
    if any(re.search(r"\S {2,}\S", ln) for ln in lines[:20]):
        return [re.split(r" {2,}", ln.strip()) for ln in lines]

    if "|" in sample:
        return [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]

    return [[ln.strip()] for ln in lines]


def _header_roles(row):
    """Map a candidate header row to column roles; None if it is not a header."""
    roles = {}
    named = 0
    for idx, cell in enumerate(row):
        key = str(cell).strip().lower()
        key = re.sub(r"\s+", " ", key)
        role = COLUMN_ROLES.get(key)
        if role is None:
            canon = scoring.canonical_stat(key)
            if canon in scoring.SCORABLE:
                role = ("stat", canon)
        if role is not None and role != "ignore":
            roles[idx] = role
            named += 1
    # A header must name a player column plus at least one useful value column.
    if "name" in roles.values() and named >= 2:
        return roles
    return None


def _to_float(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    text = text.strip("()")
    if not text or text in ("-", "--", "N/A", "NA", "n/a"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


_ADP_LINE = re.compile(
    r"^\s*(?P<rank>\d+)[.)]?\s+(?P<name>[A-Za-z0-9.'\-’ ]+?)"
    r"(?:\s+\(?(?P<team>[A-Z]{2,3})\)?)?"
    r"(?:\s+(?P<pos>QB|RB|WR|TE|K|DST|D/ST|DEF)\d*)?"
    r"\s*(?:\(?(?P<adp>\d+\.?\d*)\)?)?\s*$"
)


def parse_table(text):
    """Parse a pasted projections/ADP table into normalised records.

    Returns a list of dicts with any of: name, team, position, adp, adp_stdev,
    rank, bye, points, stats{}. Column order does not matter, and the header is
    optional - unheadered "1. Ja'Marr Chase CIN (1.2)" lists are handled too.
    """
    rows = _split_rows(text)
    if not rows:
        return []

    roles = None
    start = 0
    for idx, row in enumerate(rows[:5]):
        found = _header_roles(row)
        if found:
            roles, start = found, idx + 1
            break

    records = []
    if roles:
        for row in rows[start:]:
            rec = {"stats": {}}
            for col, role in roles.items():
                if col >= len(row):
                    continue
                cell = str(row[col]).strip()
                if not cell:
                    continue
                if isinstance(role, tuple):
                    value = _to_float(cell)
                    if value is not None:
                        rec["stats"][role[1]] = value
                elif role == "name":
                    rec["name"] = cell
                elif role == "team":
                    rec["team"] = cell
                elif role == "position":
                    rec["position"] = re.sub(r"\d+$", "", cell)
                else:
                    value = _to_float(cell)
                    if value is not None:
                        rec[role] = value
            if rec.get("name"):
                records.append(rec)
        if records:
            return records

    # No usable header. Try the numbered-list form, then a bare name list.
    for row in rows:
        line = " ".join(str(c).strip() for c in row).strip()
        if not line:
            continue
        match = _ADP_LINE.match(line)
        if match and match.group("name") and len(match.group("name").strip()) > 2:
            rec = {"name": match.group("name").strip(), "stats": {}}
            if match.group("team"):
                rec["team"] = match.group("team")
            if match.group("pos"):
                rec["position"] = match.group("pos")
            adp = _to_float(match.group("adp"))
            rank = _to_float(match.group("rank"))
            rec["rank"] = rank
            rec["adp"] = adp if adp is not None else rank
            records.append(rec)
            continue
        # Bare "Name TEAM POS" or just "Name": rank by line order.
        cells = [str(c).strip() for c in row if str(c).strip()]
        if cells and re.search(r"[A-Za-z]{3,}", cells[0]):
            rec = {"name": cells[0], "stats": {}}
            for cell in cells[1:]:
                upper = cell.upper()
                if upper in ("QB", "RB", "WR", "TE", "K", "DEF", "DST", "D/ST"):
                    rec["position"] = upper
                elif normalize_team(cell) in set(TEAM_ABBR.values()):
                    rec["team"] = cell
                elif _to_float(cell) is not None and "adp" not in rec:
                    rec["adp"] = _to_float(cell)
            rec.setdefault("adp", float(len(records) + 1))
            records.append(rec)
    return records


def apply_projection_paste(text, players, index=None):
    """Parse pasted projections and return (by_player_id, report)."""
    index = index or PlayerIndex(players)
    records = parse_table(text)
    out, matched, unmatched = {}, 0, []

    for rec in records:
        pid = index.match(rec.get("name"), rec.get("position"), rec.get("team"))
        if not pid:
            unmatched.append(rec.get("name"))
            continue
        entry = {"stats": rec.get("stats") or {}, "source": "paste"}
        if rec.get("points") is not None:
            entry["points_override"] = rec["points"]
        if rec.get("bye") is not None:
            entry["bye"] = int(rec["bye"])
        out[pid] = entry
        matched += 1

    report = {
        "parsed": len(records),
        "matched": matched,
        "unmatched": [u for u in unmatched if u][:40],
        "unmatched_count": len(unmatched),
    }
    return out, report


def apply_adp_paste(text, players, index=None):
    """Parse pasted ADP and return (by_player_id, report)."""
    index = index or PlayerIndex(players)
    records = parse_table(text)
    out, matched, unmatched = {}, 0, []

    for order, rec in enumerate(records, start=1):
        pid = index.match(rec.get("name"), rec.get("position"), rec.get("team"))
        if not pid:
            unmatched.append(rec.get("name"))
            continue
        adp = rec.get("adp")
        if adp is None:
            adp = rec.get("rank")
        if adp is None:
            adp = float(order)
        entry = {"adp": float(adp)}
        if rec.get("adp_stdev") is not None:
            entry["stdev"] = float(rec["adp_stdev"])
        if rec.get("bye") is not None:
            entry["bye"] = int(rec["bye"])
        out[pid] = entry
        matched += 1

    report = {
        "parsed": len(records),
        "matched": matched,
        "unmatched": [u for u in unmatched if u][:40],
        "unmatched_count": len(unmatched),
    }
    return out, report
