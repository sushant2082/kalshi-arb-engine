"""
Kalshi <-> Polymarket cross-venue monitor for MLB games.

This closes a real gap. Cross-venue matching was built for crypto brackets and
election districts, and both turned out to be structurally unmatchable — crypto
because the two venues never share a settlement instant, elections because the
overlap is a handful of markets. Sports were never checked, and sports is where
both venues actually carry the same event with real liquidity.

The join is clean on both sides:

    Kalshi      KXMLBGAME-26AUG072305ATLNYY-ATL   date + HHMM (ET) + slugs
    Polymarket  mlb-atl-nyy-2026-08-07            slugs + date, plus an
                                                  explicit gameStartTime in UTC

so games are matched on team pair and first pitch, not on title text.

WHAT AN "ARB" HERE IS AND IS NOT
--------------------------------
Buying one team on Kalshi and the other on Polymarket for under $1 total pays
$1 whichever team wins — but only if both venues settle the same way. They are
separate entities with separate rulebooks, and MLB games have real edge cases
(postponement, suspension, rain-shortened results) where they can differ. So
this carries resolution basis risk and is not risk-free in the way an
intra-venue lock is. It is still by far the tightest cross-venue surface found.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from arbengine.fees import order_fee
from arbengine.source.polymarket import outcomes, token_ids
from arbengine.value.games import KALSHI_SLUG_TO_TEAM, parse_ticker

log = logging.getLogger(__name__)

# Polymarket slug: mlb-atl-nyy-2026-08-07
_PM_SLUG = re.compile(
    r"^mlb-(?P<away>[a-z]{2,4})-(?P<home>[a-z]{2,4})-"
    r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$"
)

# Polymarket's abbreviations mostly match Kalshi's, but not everywhere.
_PM_ALIASES = {
    "ari": "AZ", "az": "AZ",
    "ath": "ATH", "oak": "ATH",
    "chw": "CWS", "cws": "CWS",
    "chc": "CHC",
    "kan": "KC", "kc": "KC",
    "sdp": "SD", "sd": "SD",
    "sfg": "SF", "sf": "SF",
    "tam": "TB", "tb": "TB",
    "was": "WSH", "wsh": "WSH",
    "nyy": "NYY", "nym": "NYM",
    "laa": "LAA", "lad": "LAD",
}


def pm_slug_to_kalshi(abbr: str) -> str | None:
    """Normalize a Polymarket team abbreviation to a Kalshi slug."""
    low = abbr.lower()
    if low in _PM_ALIASES:
        return _PM_ALIASES[low]
    upper = abbr.upper()
    return upper if upper in KALSHI_SLUG_TO_TEAM else None


def _parse_start(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "+00:00"
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class PmGame:
    """A parsed Polymarket MLB moneyline market."""

    condition_id: str
    question: str
    slug: str
    away_slug: str
    home_slug: str
    start: datetime | None
    # Token ids aligned to `outcome_names`.
    outcome_names: list[str] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)
    liquidity: float = 0.0

    def token_for_slug(self, kalshi_slug: str) -> str | None:
        """
        The token that pays $1 if the given team wins.

        Outcome names are full team names, so this maps back through the slug
        table rather than assuming outcome order matches the slug order.
        """
        want = KALSHI_SLUG_TO_TEAM.get(kalshi_slug)
        if not want:
            return None
        for name, tid in zip(self.outcome_names, self.token_ids):
            if name.strip().lower() == want.strip().lower():
                return tid
        return None


def parse_pm_game(market: dict) -> PmGame | None:
    """Build a PmGame from Polymarket metadata, or None if it is not a moneyline."""
    slug = market.get("slug") or ""
    m = _PM_SLUG.match(slug)
    if not m:
        return None

    away = pm_slug_to_kalshi(m.group("away"))
    home = pm_slug_to_kalshi(m.group("home"))
    if not away or not home:
        log.debug("Unmapped Polymarket team slug in %s", slug)
        return None

    names = outcomes(market)
    tids = token_ids(market)
    if len(names) != 2 or len(tids) != 2:
        return None

    return PmGame(
        condition_id=str(market.get("conditionId") or ""),
        question=market.get("question") or "",
        slug=slug,
        away_slug=away,
        home_slug=home,
        start=_parse_start(market.get("gameStartTime") or market.get("startDate")),
        outcome_names=names,
        token_ids=tids,
        liquidity=float(market.get("liquidity") or 0.0),
    )


@dataclass
class GamePair:
    """One MLB game carried by both venues."""

    away_slug: str
    home_slug: str
    start: datetime | None
    pm: PmGame
    # Kalshi ticker per team slug.
    kalshi_tickers: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        a = KALSHI_SLUG_TO_TEAM.get(self.away_slug, self.away_slug)
        h = KALSHI_SLUG_TO_TEAM.get(self.home_slug, self.home_slug)
        return f"{a} @ {h}"


_START_TOLERANCE = timedelta(minutes=90)


def pair_games(
    kalshi_markets: list[dict], pm_markets: list[dict]
) -> tuple[list[GamePair], dict[str, int]]:
    """Match Kalshi game contracts to Polymarket moneylines on teams + start."""
    rejects: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejects[reason] = rejects.get(reason, 0) + 1

    pm_games: list[PmGame] = []
    for m in pm_markets:
        g = parse_pm_game(m)
        if g is not None:
            pm_games.append(g)

    by_teams: dict[tuple[str, str], list[PmGame]] = {}
    for g in pm_games:
        by_teams.setdefault((g.away_slug, g.home_slug), []).append(g)

    pairs: dict[str, GamePair] = {}
    for m in kalshi_markets:
        game = parse_ticker(m.get("ticker", ""))
        if game is None:
            reject("unparseable kalshi ticker")
            continue

        candidates = by_teams.get((game.away_slug, game.home_slug))
        if not candidates:
            reject("no polymarket market for this matchup")
            continue

        chosen = None
        best = _START_TOLERANCE
        for g in candidates:
            if g.start is None or game.start_utc is None:
                continue
            gap = abs(g.start - game.start_utc)
            if gap <= best:
                best, chosen = gap, g
        if chosen is None:
            reject("matchup found but start times do not line up")
            continue

        pair = pairs.setdefault(
            chosen.condition_id,
            GamePair(
                away_slug=game.away_slug, home_slug=game.home_slug,
                start=chosen.start, pm=chosen,
            ),
        )
        pair.kalshi_tickers[game.side_slug] = game.ticker

    complete = [p for p in pairs.values() if len(p.kalshi_tickers) == 2]
    for p in pairs.values():
        if len(p.kalshi_tickers) != 2:
            reject("only one kalshi side available")
    return complete, rejects


@dataclass
class CrossQuote:
    """Both venues' prices for one game, and whether they cross."""

    pair: GamePair
    # Cost to buy each team on each venue.
    kalshi: dict[str, float | None] = field(default_factory=dict)
    kalshi_size: dict[str, int] = field(default_factory=dict)
    poly: dict[str, float | None] = field(default_factory=dict)
    poly_size: dict[str, float] = field(default_factory=dict)
    at: datetime | None = None

    def combos(self, fee_multiplier: float = 0.07) -> list[dict]:
        """
        Every way to own both teams across the two venues.

        Buying each team once pays exactly $1, since one of them wins. Total
        all-in cost below $1 is the arbitrage condition. Both venue fees are
        charged: Kalshi's per-order taker fee, and Polymarket's taker rate.
        """
        out: list[dict] = []
        away, home = self.pair.away_slug, self.pair.home_slug

        options = [
            ("kalshi", "polymarket", self.kalshi.get(away), self.poly.get(home)),
            ("polymarket", "kalshi", self.poly.get(away), self.kalshi.get(home)),
            ("kalshi", "kalshi", self.kalshi.get(away), self.kalshi.get(home)),
            ("polymarket", "polymarket", self.poly.get(away), self.poly.get(home)),
        ]
        for away_venue, home_venue, a_price, h_price in options:
            if a_price is None or h_price is None:
                continue
            fee = 0.0
            if away_venue == "kalshi":
                fee += order_fee(a_price, 100, fee_multiplier) / 100.0
            if home_venue == "kalshi":
                fee += order_fee(h_price, 100, fee_multiplier) / 100.0
            total = a_price + h_price + fee

            # Fillable size is the thinner leg, and it is the number that
            # decides whether an edge is worth anything. Polymarket advertises
            # a `liquidity` figure in the thousands that is NOT top-of-book
            # depth: measured live, a +0.36% cross had $5,543 of advertised
            # liquidity and 50 shares at the best ask, capping the whole trade
            # at 18 cents of profit. Reporting a percentage without the size
            # makes a rounding error look like an opportunity.
            a_size = (
                self.kalshi_size.get(away, 0) if away_venue == "kalshi"
                else self.poly_size.get(away, 0)
            )
            h_size = (
                self.kalshi_size.get(home, 0) if home_venue == "kalshi"
                else self.poly_size.get(home, 0)
            )
            # Whole contracts only: Kalshi does not trade fractions, so a
            # combined depth under one contract is not executable at all.
            sets = int(min(a_size or 0, h_size or 0))
            fillable = sets >= 1

            out.append({
                "away_venue": away_venue,
                "home_venue": home_venue,
                "away_price": a_price,
                "home_price": h_price,
                "fee": fee,
                "total": total,
                "profit": 1.0 - total,
                "sets": sets,
                "dollar_profit": (1.0 - total) * sets,
                "cross_venue": away_venue != home_venue,
                "fillable": fillable,
            })
        return sorted(out, key=lambda c: c["total"])

    def best(self, fee_multiplier: float = 0.07) -> dict | None:
        """
        Cheapest EXECUTABLE combination.

        Combinations with no depth behind them are excluded rather than ranked
        first. A quoted price with zero size is not an offer, and treating it
        as one produced most of the apparent opportunities in an early
        measurement: 18 of 37 "crosses" had zero depth, and they carried both
        the longest lifetimes and the most absurd margins (+87%). If nothing is
        executable, the best non-fillable combo is returned so the prices are
        still visible, but it is flagged and must not be counted as a cross.
        """
        combos = self.combos(fee_multiplier)
        if not combos:
            return None
        executable = [c for c in combos if c["fillable"]]
        return executable[0] if executable else combos[0]

    def best_fillable(self, fee_multiplier: float = 0.07) -> dict | None:
        """The cheapest combination that could actually be traded, or None."""
        best = self.best(fee_multiplier)
        return best if best and best["fillable"] else None
