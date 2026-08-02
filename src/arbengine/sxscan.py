"""
Sustained SX Bet within-market scanner.

This is the surface with the best risk profile in the engine:

  - 0% taker fee on straight bets, so the gross gap IS the net gap. Everywhere
    else fees are the binding constraint.
  - Strictly intra-venue, so no cross-venue resolution basis risk.
  - Downside is zero rather than negative: if the market voids, both stakes are
    returned.

Its books are also the tightest measured — a median overround around 1.0175
with the best books at 1.0038 — so the question is purely whether they ever
cross below 1.00, and how long they stay there. That is what this loop
measures.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from arbengine.source.sxbet import SxBetClient, SxQuote, detect_within_market

log = logging.getLogger(__name__)

# Sports with real depth on SX Bet.
DEFAULT_SPORT_IDS = [1, 2, 3, 5, 6, 8]


@dataclass
class SxOpportunity:
    """A two-sided SX Bet position priced under $1."""

    market_hash: str
    league: str
    outcome_one: str
    outcome_two: str
    one_ask: float
    two_ask: float
    total_cost: float
    profit_per_set: float
    fillable: float
    void_outcome: str
    push_possible: bool
    first_seen: datetime
    last_seen: datetime

    @property
    def key(self) -> str:
        return f"sx|{self.market_hash}"

    @property
    def persistence_sec(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()

    @property
    def total_profit(self) -> float:
        return self.profit_per_set * self.fillable


@dataclass
class SxScanStats:
    """
    Running picture of the scan.

    The overround distribution matters as much as the hit count: with no fee to
    clear, how close the books get is the whole question, and "no locks" is
    uninformative without it.
    """

    passes: int = 0
    markets_seen: int = 0
    books_two_sided: int = 0
    opportunities: int = 0
    tightest: list[tuple[float, str, str]] = field(default_factory=list)
    overrounds: list[float] = field(default_factory=list)

    @property
    def median_overround(self) -> float | None:
        if not self.overrounds:
            return None
        s = sorted(self.overrounds)
        return s[len(s) // 2]

    @property
    def best_overround(self) -> float | None:
        return min(self.overrounds) if self.overrounds else None


class SxTracker:
    """
    Holds first_seen per market so a persisting gap extends one record.

    Persistence is the real output here exactly as it is on Kalshi: a lock that
    exists for 200ms is a statistic, not a trade.
    """

    def __init__(self) -> None:
        self._first: dict[str, datetime] = {}

    def observe(self, opp: SxOpportunity) -> SxOpportunity:
        first = self._first.setdefault(opp.key, opp.first_seen)
        opp.first_seen = first
        return opp

    def expire(self, live_keys: set[str]) -> None:
        for key in list(self._first):
            if key not in live_keys:
                del self._first[key]


async def scan_once(
    client: SxBetClient,
    sport_ids: list[int] | None = None,
    min_profit: float = 0.0,
    max_markets: int = 400,
) -> tuple[list[SxOpportunity], SxScanStats, dict[str, SxQuote]]:
    """One full pass over SX Bet's active markets."""
    stats = SxScanStats(passes=1)
    markets = await client.active_markets(sport_ids or DEFAULT_SPORT_IDS)
    stats.markets_seen = len(markets)

    by_hash = {m.get("marketHash"): m for m in markets}
    quotes = await client.quotes(markets[:max_markets])

    now = datetime.now(timezone.utc)
    found: list[SxOpportunity] = []

    for h, q in quotes.items():
        if q.overround is None:
            continue
        stats.books_two_sided += 1
        stats.overrounds.append(q.overround)

        hit = detect_within_market(q, min_profit=min_profit, market=by_hash.get(h))
        if hit:
            found.append(
                SxOpportunity(
                    market_hash=hit["market_hash"],
                    league=hit["league"],
                    outcome_one=hit["outcomes"][0],
                    outcome_two=hit["outcomes"][1],
                    one_ask=hit["one_ask"],
                    two_ask=hit["two_ask"],
                    total_cost=hit["total_cost"],
                    profit_per_set=hit["profit_per_set"],
                    fillable=hit["fillable"],
                    void_outcome=hit["void_outcome"],
                    push_possible=hit["push_possible"],
                    first_seen=now,
                    last_seen=now,
                )
            )

    stats.opportunities = len(found)
    stats.tightest = sorted(
        (
            (q.overround, q.league, q.outcome_one_name)
            for q in quotes.values()
            if q.overround is not None
        )
    )[:10]
    return found, stats, quotes


async def scan_loop(
    client: SxBetClient,
    on_opportunity,
    on_pass=None,
    sport_ids: list[int] | None = None,
    min_profit: float = 0.0,
    interval_sec: float = 5.0,
    duration_sec: float | None = None,
    max_markets: int = 400,
) -> SxScanStats:
    """
    Poll SX Bet until stopped, reporting locks and tracking how long they last.

    Polling rather than streaming: SX Bet's read API is unauthenticated and
    cheap, and a full pass over the liquid markets costs a few hundred requests.
    A websocket feed would be the next step if pass latency ever becomes the
    limiting factor, but it is not yet — the books move on human timescales.
    """
    tracker = SxTracker()
    total = SxScanStats()
    started = asyncio.get_event_loop().time()

    while True:
        found, stats, _ = await scan_once(
            client, sport_ids, min_profit, max_markets
        )
        total.passes += 1
        total.markets_seen = stats.markets_seen
        total.books_two_sided = stats.books_two_sided
        total.overrounds = stats.overrounds
        total.tightest = stats.tightest

        now = datetime.now(timezone.utc)
        live: set[str] = set()
        for opp in found:
            tracked = tracker.observe(opp)
            tracked.last_seen = now
            live.add(tracked.key)
            total.opportunities += 1
            await on_opportunity(tracked)
        tracker.expire(live)

        if on_pass is not None:
            await on_pass(total, stats)

        if duration_sec is not None:
            if asyncio.get_event_loop().time() - started >= duration_sec:
                break
        await asyncio.sleep(interval_sec)

    return total
