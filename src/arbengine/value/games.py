"""
Match Kalshi game markets to sharp sportsbook events.

A wrong match here is the most expensive error in the value strategy: it
produces a confident edge computed against a different game, and those look
largest precisely because the two lines are unrelated. So matching is on
structured identifiers — team slug and game date parsed out of the Kalshi
ticker — never on title similarity.

Kalshi ticker shape:

    KXMLBGAME-26AUG042140DETSEA-SEA
              |     |   |     |
              |     |   |     +-- which team this contract pays on
              |     |   +-------- away+home slugs concatenated
              |     +------------ start time, HHMM ET
              +------------------ date, YYMONDD

The concatenated slug pair is the awkward part: DETSEA could split as DET/SEA
or DETS/EA, so it is resolved against the known slug table rather than by
assuming a fixed width.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

KALSHI_SLUG_TO_TEAM: dict[str, str] = {
    "ARI": "Arizona Diamondbacks",   # legacy alias
    "AZ":  "Arizona Diamondbacks",   # active Kalshi slug (2026)
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC":  "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "OAK": "Athletics",              # legacy alias
    "ATH": "Athletics",              # active Kalshi slug (2026)
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD":  "San Diego Padres",
    "SEA": "Seattle Mariners",
    "SF":  "San Francisco Giants",
    "STL": "St. Louis Cardinals",
    "TB":  "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals",
}

# Longest slugs first so a greedy prefix match cannot mis-split a pair.
_SLUGS_BY_LENGTH = sorted(KALSHI_SLUG_TO_TEAM, key=len, reverse=True)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# KXMLBGAME-26AUG042140DETSEA-SEA
_TICKER = re.compile(
    r"^(?P<series>[A-Z0-9]+)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<hhmm>\d{4})"
    r"(?P<teams>[A-Z0-9]+)-(?P<side>[A-Z0-9]+)$"
)


@dataclass
class KalshiGame:
    """A parsed Kalshi game contract."""

    ticker: str
    series: str
    game_date: date
    away_slug: str
    home_slug: str
    side_slug: str

    @property
    def away_team(self) -> str | None:
        return KALSHI_SLUG_TO_TEAM.get(self.away_slug)

    @property
    def home_team(self) -> str | None:
        return KALSHI_SLUG_TO_TEAM.get(self.home_slug)

    @property
    def side_team(self) -> str | None:
        return KALSHI_SLUG_TO_TEAM.get(self.side_slug)

    @property
    def resolvable(self) -> bool:
        return all((self.away_team, self.home_team, self.side_team))


def split_team_pair(blob: str) -> tuple[str, str] | None:
    """
    Split a concatenated away+home slug pair.

    Ambiguity is real: "DETSEA" splits cleanly, but a naive fixed-width split
    breaks on two- and four-character slugs like SD, TB, KC and CWS. Every
    prefix that is a known slug is tried, longest first, and the split is only
    accepted when the remainder is also a known slug. If two different splits
    both validate, the pair is rejected rather than guessed.
    """
    matches: list[tuple[str, str]] = []
    for slug in _SLUGS_BY_LENGTH:
        if blob.startswith(slug):
            rest = blob[len(slug):]
            if rest in KALSHI_SLUG_TO_TEAM:
                matches.append((slug, rest))
    if len(matches) != 1:
        if len(matches) > 1:
            log.debug("Ambiguous team pair %r: %s", blob, matches)
        return None
    return matches[0]


def parse_ticker(ticker: str) -> KalshiGame | None:
    """Parse a Kalshi game ticker into teams and date, or None if it does not fit."""
    m = _TICKER.match(ticker)
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon"))
    if mon is None:
        return None
    try:
        game_date = date(2000 + int(m.group("yy")), mon, int(m.group("dd")))
    except ValueError:
        return None

    pair = split_team_pair(m.group("teams"))
    if pair is None:
        return None

    game = KalshiGame(
        ticker=ticker,
        series=m.group("series"),
        game_date=game_date,
        away_slug=pair[0],
        home_slug=pair[1],
        side_slug=m.group("side"),
    )
    return game if game.resolvable else None


@dataclass
class GameMatch:
    """A Kalshi contract paired with the sharp line for the same game."""

    game: KalshiGame
    sharp: dict
    # True when the Kalshi contract pays on the home team.
    side_is_home: bool


# Kalshi tickers carry the ET date; a late game can therefore sit on the
# previous UTC day relative to the sportsbook's commence_time.
_DATE_TOLERANCE = timedelta(days=1)


def match_games(
    kalshi_markets: list[dict], sharp_quotes: list[dict]
) -> tuple[list[GameMatch], dict[str, int]]:
    """
    Pair Kalshi game contracts with sharp lines on team identity and date.

    Returns the matches and a tally of why the rest were rejected, so coverage
    gaps stay visible rather than silently shrinking the universe.
    """
    rejects: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejects[reason] = rejects.get(reason, 0) + 1

    by_teams: dict[tuple[str, str], list[dict]] = {}
    for q in sharp_quotes:
        key = (q["away_team"], q["home_team"])
        by_teams.setdefault(key, []).append(q)

    matches: list[GameMatch] = []
    for m in kalshi_markets:
        ticker = m.get("ticker", "")
        game = parse_ticker(ticker)
        if game is None:
            reject("unparseable ticker or unknown team slug")
            continue

        candidates = by_teams.get((game.away_team, game.home_team))
        if not candidates:
            reject("no sharp line for this matchup")
            continue

        chosen = None
        for q in candidates:
            ct = q.get("commence_time")
            if ct is None:
                continue
            if abs(ct.date() - game.game_date) <= _DATE_TOLERANCE:
                chosen = q
                break
        if chosen is None:
            reject("matchup found but dates do not line up")
            continue

        if game.side_slug not in (game.away_slug, game.home_slug):
            reject("contract side is not one of the two teams")
            continue

        matches.append(
            GameMatch(
                game=game,
                sharp=chosen,
                side_is_home=(game.side_slug == game.home_slug),
            )
        )

    return matches, rejects
