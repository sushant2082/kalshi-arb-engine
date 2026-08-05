"""
Match Kalshi tennis contracts to sharp lines by player and date.

Tennis avoids the trap that broke baseball matching. A baseball series plays
the same two teams on consecutive days, so a date join maps one sharp line onto
several different games. A tennis player plays at most one match per day in a
tournament, so (player, date) is genuinely unique and a date join is safe.

The difficulty moves to names instead. Both venues publish full names, but
spelling diverges on accents and on multi-part surnames — "Iva Jović" against
"Iva Jovic", "Botic van de Zandschulp" against "Van de Zandschulp". So names
are normalized to unaccented lowercase and compared on the surname plus first
initial, which is stable across both sources without being so loose that two
different players collide.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Particles that are part of a surname but are often dropped or reordered.
_PARTICLES = {"van", "de", "der", "den", "del", "della", "di", "da", "dos",
              "du", "la", "le", "el", "al", "bin", "ibn", "st", "mc", "mac"}

_TICKER = re.compile(
    r"^(?P<series>KX(?:ATP|WTA)MATCH)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<pair>[A-Z]+)-(?P<side>[A-Z]+)$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def normalize_name(name: str) -> str:
    """Lowercase, unaccented, punctuation-free form of a player name."""
    text = strip_accents(name or "").lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    return " ".join(text.split())


def name_key(name: str) -> str | None:
    """
    A comparison key that survives the spelling differences between venues.

    Surname plus first initial. Using the full string fails on accents and on
    dropped particles; using the surname alone would collide two players from
    the same family or with a common surname, which on this data is a real
    risk rather than a theoretical one.
    """
    parts = normalize_name(name).split()
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]

    first = parts[0]
    # Walk backwards over particles so "van de zandschulp" keeps its head word.
    idx = len(parts) - 1
    while idx > 1 and parts[idx - 1] in _PARTICLES:
        idx -= 1
    surname = "".join(parts[idx:])
    return f"{surname}|{first[0]}"


@dataclass
class TennisContract:
    """A parsed Kalshi tennis contract."""

    ticker: str
    series: str
    match_date: date
    player: str
    key: str | None = None

    def __post_init__(self) -> None:
        if self.key is None:
            self.key = name_key(self.player)


def parse_contract(market: dict) -> TennisContract | None:
    """
    Build a contract from Kalshi tennis market metadata.

    The player comes from `yes_sub_title`, which carries the full name. The
    ticker's three-letter code is not used for identity: it is truncated and
    collides readily across a draw.
    """
    ticker = market.get("ticker", "")
    m = _TICKER.match(ticker)
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon"))
    if mon is None:
        return None
    try:
        match_date = date(2000 + int(m.group("yy")), mon, int(m.group("dd")))
    except ValueError:
        return None

    player = (market.get("yes_sub_title") or "").strip()
    if not player:
        return None

    contract = TennisContract(
        ticker=ticker, series=m.group("series"),
        match_date=match_date, player=player,
    )
    return contract if contract.key else None


@dataclass
class TennisMatch:
    """A Kalshi tennis contract paired with the sharp line for that match."""

    contract: TennisContract
    sharp: dict
    side_is_home: bool


# Tennis start times slide with order of play, so the date is the reliable
# join and the time is not. A one-day window absorbs matches that cross
# midnight UTC without risking a collision, since a player appears once a day.
_DATE_TOLERANCE = timedelta(days=1)


def match_players(
    kalshi_markets: list[dict], sharp_quotes: list[dict]
) -> tuple[list[TennisMatch], dict[str, int]]:
    """Pair Kalshi tennis contracts with sharp lines on player identity."""
    rejects: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejects[reason] = rejects.get(reason, 0) + 1

    index: dict[str, list[tuple[dict, bool]]] = {}
    for q in sharp_quotes:
        for name, is_home in ((q["home_team"], True), (q["away_team"], False)):
            key = name_key(name)
            if key:
                index.setdefault(key, []).append((q, is_home))

    matches: list[TennisMatch] = []
    for market in kalshi_markets:
        contract = parse_contract(market)
        if contract is None:
            reject("unparseable tennis ticker or missing player name")
            continue

        candidates = index.get(contract.key)
        if not candidates:
            reject("no sharp line for this player")
            continue

        chosen: tuple[dict, bool] | None = None
        for q, is_home in candidates:
            ct = q.get("commence_time")
            if ct is None:
                continue
            if abs(ct.date() - contract.match_date) <= _DATE_TOLERANCE:
                if chosen is not None:
                    # Two sharp matches for one player on one date should not
                    # happen; refuse rather than pick arbitrarily.
                    chosen = None
                    reject("ambiguous: player appears in two sharp matches")
                    break
                chosen = (q, is_home)
        if chosen is None:
            reject("player found but no match on that date")
            continue

        matches.append(
            TennisMatch(contract=contract, sharp=chosen[0], side_is_home=chosen[1])
        )

    return matches, rejects
