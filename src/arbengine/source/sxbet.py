"""
Read-only SX Bet market-data client.

Same hard boundary as the other sources: GET requests only, no wallet, no
signing key. SX Bet's read API needs no authentication at all.

Why it is worth integrating: SX Bet charges 0% on single bets. Fees are the
binding constraint on everything this engine does — the Kalshi ladders sit
under a cent from inverting while a round-trip costs about two — so a zero-fee
venue changes the arithmetic more than any latency improvement would.

The critical difference from Kalshi and Polymarket: SX Bet is a peer-to-peer
exchange with no house. Orders are posted by MAKERS stating the side THEY are
taking, so a taker buys the opposite side at the complementary price. Reading
an order as though it were an offer to sell you that outcome inverts every
price and every size in the book.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.sx.bet"

# SX Bet encodes implied probability as an integer scaled by 1e20, and token
# amounts in the base token's units (USDC, 6 decimals).
ODDS_SCALE = 10**20
USDC_SCALE = 10**6

# SX Bet charges no fee on single bets. Recorded explicitly rather than left
# implicit, because "no fee" is an assumption worth being able to find and
# change if their schedule ever does.
TAKER_FEE_RATE = 0.0


def _int(v: object) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


@dataclass
class SxQuote:
    """Best takeable prices on both outcomes of one SX Bet market."""

    market_hash: str
    outcome_one_name: str
    outcome_two_name: str
    league: str
    team_one: str
    team_two: str
    game_time: datetime | None
    # Cost for a TAKER to buy each outcome, in dollars, and how much stake the
    # resting orders can absorb at that price.
    one_ask: float | None = None
    two_ask: float | None = None
    one_ask_size: float = 0.0
    two_ask_size: float = 0.0
    fetched_at: datetime | None = None

    @property
    def overround(self) -> float | None:
        """
        Sum of both takeable prices. Above 1 is the usual spread; BELOW 1 is a
        within-market arbitrage on SX Bet alone, since one of the two outcomes
        must occur.
        """
        if self.one_ask is None or self.two_ask is None:
            return None
        return self.one_ask + self.two_ask


def taker_price(maker_percentage_odds: int) -> float:
    """
    Convert a maker's posted odds into what a taker pays for the OTHER side.

    A maker posting `percentageOdds` is stating the implied probability of the
    outcome they are backing. The taker takes the complement, so the taker's
    price is 1 - p. Treating the maker's number as the taker's price would
    quote every market at its mirror image.
    """
    return 1.0 - (maker_percentage_odds / ODDS_SCALE)


def taker_capacity(total_bet_size: int, maker_percentage_odds: int) -> float:
    """
    How much stake a taker can place against a maker order, in dollars.

    The maker risks `totalBetSize` to win the taker's stake. For implied
    probability p on the maker's side, the stakes are in ratio p : (1-p), so
    the taker's maximum stake is size * (1-p)/p — NOT the maker's size. Using
    the maker's size directly overstates fillable volume whenever the maker is
    on the favourite, which is most of the time.
    """
    p = maker_percentage_odds / ODDS_SCALE
    if p <= 0 or p >= 1:
        return 0.0
    return (total_bet_size / USDC_SCALE) * (1.0 - p) / p


def quote_from_orders(market: dict, orders: list[dict], now: datetime) -> SxQuote:
    """
    Collapse a market's resting orders into the best takeable price per side.

    To buy outcome ONE you need a maker backing outcome TWO, and vice versa —
    hence the crossed lookups below. The best price is the one from the maker
    with the HIGHEST odds on their own side, since the taker pays 1 - p.
    """
    game_time = market.get("gameTime")
    quote = SxQuote(
        market_hash=market.get("marketHash", ""),
        outcome_one_name=market.get("outcomeOneName", ""),
        outcome_two_name=market.get("outcomeTwoName", ""),
        league=market.get("leagueLabel", ""),
        team_one=market.get("teamOneName", ""),
        team_two=market.get("teamTwoName", ""),
        game_time=(
            datetime.fromtimestamp(game_time, tz=timezone.utc)
            if isinstance(game_time, (int, float)) and game_time
            else None
        ),
        fetched_at=now,
    )

    best_for_one: tuple[float, float] | None = None  # (price, capacity)
    best_for_two: tuple[float, float] | None = None

    for o in orders:
        if str(o.get("orderStatus", "ACTIVE")).upper() not in ("ACTIVE", ""):
            continue
        odds = _int(o.get("percentageOdds"))
        if odds <= 0 or odds >= ODDS_SCALE:
            continue

        size = _int(o.get("totalBetSize")) - _int(o.get("fillAmount"))
        if size <= 0:
            continue

        price = taker_price(odds)
        capacity = taker_capacity(size, odds)

        if o.get("isMakerBettingOutcomeOne"):
            # Maker backs outcome one, so the taker can buy outcome TWO.
            if best_for_two is None or price < best_for_two[0]:
                best_for_two = (price, capacity)
            elif abs(price - best_for_two[0]) < 1e-12:
                best_for_two = (price, best_for_two[1] + capacity)
        else:
            if best_for_one is None or price < best_for_one[0]:
                best_for_one = (price, capacity)
            elif abs(price - best_for_one[0]) < 1e-12:
                best_for_one = (price, best_for_one[1] + capacity)

    if best_for_one:
        quote.one_ask, quote.one_ask_size = best_for_one
    if best_for_two:
        quote.two_ask, quote.two_ask_size = best_for_two
    return quote


class SxBetClient:
    def __init__(self, timeout: float = 25.0, request_interval: float = 0.08) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)
        self._interval = request_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "SxBetClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _throttle(self) -> None:
        import time as _t

        async with self._lock:
            elapsed = _t.monotonic() - self._last
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last = _t.monotonic()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._throttle()
        resp = await self._http.get(f"{BASE}{path}", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("status") == "success":
            return payload.get("data")
        return payload

    async def sports(self) -> list[dict]:
        return await self._get("/sports") or []

    async def active_markets(self, sport_ids: list[int] | None = None) -> list[dict]:
        """Active markets, optionally restricted to given sports."""
        out: list[dict] = []
        for sid in sport_ids or [None]:
            params = {"sportIds": sid} if sid is not None else None
            try:
                data = await self._get("/markets/active", params=params)
            except httpx.HTTPStatusError as exc:
                log.warning("SX Bet markets/active failed: %s", exc)
                continue
            if isinstance(data, dict):
                out.extend(data.get("markets") or [])
            elif isinstance(data, list):
                out.extend(data)
        return out

    async def orders(self, market_hash: str) -> list[dict]:
        try:
            return await self._get("/orders", params={"marketHashes": market_hash}) or []
        except httpx.HTTPStatusError as exc:
            log.debug("SX Bet orders failed for %s: %s", market_hash, exc)
            return []

    async def quotes(
        self, markets: list[dict], concurrency: int = 6
    ) -> dict[str, SxQuote]:
        """Fetch and normalize books for many markets concurrently."""
        sem = asyncio.Semaphore(concurrency)
        now = datetime.now(timezone.utc)
        out: dict[str, SxQuote] = {}

        async def one(m: dict) -> None:
            async with sem:
                orders = await self.orders(m.get("marketHash", ""))
                if not orders:
                    return
                q = quote_from_orders(m, orders, now)
                if q.one_ask is not None or q.two_ask is not None:
                    out[q.market_hash] = q

        await asyncio.gather(*(one(m) for m in markets))
        return out


def detect_within_market(quote: SxQuote, min_profit: float = 0.0) -> dict | None:
    """
    Check one SX Bet market for an internal lock.

    Exactly one of the two outcomes occurs, so buying both for under $1 is a
    guaranteed dollar — and with no taker fee, the gross gap is the net gap.
    This is the same complement check the Kalshi detector runs, and it is
    strictly intra-venue, so unlike a cross-venue pair it carries no resolution
    basis risk.
    """
    total = quote.overround
    if total is None:
        return None
    profit = 1.0 - total
    if profit <= min_profit:
        return None
    sets = min(quote.one_ask_size, quote.two_ask_size)
    if sets <= 0:
        return None
    return {
        "market_hash": quote.market_hash,
        "league": quote.league,
        "outcomes": (quote.outcome_one_name, quote.outcome_two_name),
        "one_ask": quote.one_ask,
        "two_ask": quote.two_ask,
        "total_cost": total,
        "profit_per_set": profit,
        "fillable": sets,
        "game_time": quote.game_time,
    }
