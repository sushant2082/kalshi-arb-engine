"""
Read-only Polymarket market-data client.

Same hard boundary as the Kalshi client: GET requests only. No order placement,
no wallet access, no signing key. Polymarket's public Gamma and CLOB APIs need
no authentication for market data, so this module never touches a credential.

Two APIs are used:
  - Gamma (`gamma-api.polymarket.com`): market metadata, question text,
    resolution rules, fee schedule, best bid/ask.
  - CLOB (`clob.polymarket.com`): real order books with per-level sizes.

Detection needs sizes, so the CLOB book is authoritative; Gamma is for
discovery and for the resolution text that cross-venue matching depends on.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def _num(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_clob_book(data: dict) -> dict:
    """
    Normalize a CLOB book to {bid, ask, bid_size, ask_size} for the YES token.

    Polymarket sorts BOTH sides so that the best price is the LAST entry: bids
    ascend to the highest bid, asks descend to the lowest ask. Reading index 0
    yields the worst price on the book — a plausible number that is silently
    wrong, the same trap Kalshi's ascending ladders set. Pinned in tests.
    """
    bids = data.get("bids") or []
    asks = data.get("asks") or []

    best_bid = bids[-1] if bids else None
    best_ask = asks[-1] if asks else None

    bid = _num(best_bid.get("price")) if best_bid else None
    bid_size = _num(best_bid.get("size")) if best_bid else 0.0
    ask = _num(best_ask.get("price")) if best_ask else None
    ask_size = _num(best_ask.get("size")) if best_ask else 0.0

    return {
        "bid": bid,
        "ask": ask,
        "bid_size": int(bid_size or 0),
        "ask_size": int(ask_size or 0),
        "tick_size": _num(data.get("tick_size")),
        "min_order_size": _num(data.get("min_order_size")),
        "neg_risk": bool(data.get("neg_risk")),
    }


def fee_for_order(
    price: float, shares: float, schedule: dict | None
) -> float:
    """
    Polymarket taker fee for an order.

    Polymarket now charges fees on most markets (`feesEnabled` is true on
    essentially every liquid market), with a per-category schedule of the form
    `{"rate": 0.05, "takerOnly": true, "exponent": 1, "rebateRate": 0.15}`.
    The widely repeated claim that Polymarket is fee-free is out of date and
    would produce false arbitrage if assumed.

    Modelled as `rate * min(P, 1-P) * shares`, which matches Polymarket's
    published symmetric-around-0.50 form at exponent 1. VERIFY against current
    docs before trusting a marginal opportunity — an understated fee here turns
    a loss into a reported lock, which is the one error direction that costs
    real money. The rebate is deliberately ignored: it accrues to makers, and
    everything this engine models is a taker fill.
    """
    if not schedule:
        return 0.0
    rate = _num(schedule.get("rate")) or 0.0
    if rate <= 0 or shares <= 0:
        return 0.0
    exponent = _num(schedule.get("exponent"))
    if exponent is not None and exponent != 1:
        # Unknown shape; refuse to guess rather than under-charge.
        raise ValueError(
            f"Unsupported Polymarket fee exponent {exponent}; verify the schedule"
        )
    return rate * min(price, 1.0 - price) * shares


class PolymarketClient:
    def __init__(self, timeout: float = 20.0, request_interval: float = 0.1) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)
        self._interval = request_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "PolymarketClient":
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

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        await self._throttle()
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # Gamma silently caps `limit` at 100 regardless of what is requested, so
    # paginating on offset is the only way to see past the first page. Sending
    # limit=500 and breaking when fewer come back returns exactly one page and
    # looks like "that is all there is".
    PAGE_SIZE = 100

    async def list_markets(
        self,
        max_pages: int = 20,
        order: str = "volume",
        tag_id: int | None = None,
        include_closed: bool = False,
    ) -> list[dict]:
        """
        Open, order-book-enabled markets, most liquid first.

        `tag_id` narrows to a Polymarket category (crypto is 21), which is far
        cheaper than sweeping everything when the target is known.
        """
        out: list[dict] = []
        seen: set[str] = set()
        for page in range(max_pages):
            params: dict[str, Any] = {
                "limit": self.PAGE_SIZE,
                "offset": page * self.PAGE_SIZE,
                "order": order,
                "ascending": "false",
            }
            if not include_closed:
                params["closed"] = "false"
            if tag_id is not None:
                params["tag_id"] = tag_id

            try:
                batch = await self._get(f"{GAMMA_BASE}/markets", params=params)
            except httpx.HTTPStatusError as exc:
                # Gamma 422s past a maximum offset rather than returning an
                # empty page, so a deep sweep ends with an error, not a stop.
                if exc.response.status_code == 422:
                    log.debug("Gamma offset limit reached at page %d", page)
                    break
                raise
            if not batch:
                break
            fresh = [m for m in batch if str(m.get("id")) not in seen]
            seen.update(str(m.get("id")) for m in batch)
            out.extend(fresh)
            if len(batch) < self.PAGE_SIZE:
                break

        # Only markets with a live CLOB book are tradeable at a quoted price.
        return [
            m for m in out
            if m.get("enableOrderBook") and m.get("acceptingOrders")
        ]

    async def markets_by_tag(
        self, tag_slug: str, max_pages: int = 15
    ) -> list[dict]:
        """
        Every market under a league tag, via the EVENTS endpoint.

        The volume-ordered /markets sweep buries per-game moneylines beneath
        thousands of higher-volume markets — a 30-page sweep surfaced only two
        MLB games out of ninety-six. Tag filtering works on /events but not on
        /markets, so games are collected from the events payload instead.
        """
        out: dict[str, dict] = {}
        for page in range(max_pages):
            try:
                events = await self._get(
                    f"{GAMMA_BASE}/events",
                    params={
                        "limit": self.PAGE_SIZE,
                        "offset": page * self.PAGE_SIZE,
                        "closed": "false",
                        "tag_slug": tag_slug,
                    },
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    break
                raise
            if not events:
                break
            for ev in events:
                for m in ev.get("markets") or []:
                    slug = m.get("slug")
                    if slug:
                        out[slug] = m
            if len(events) < self.PAGE_SIZE:
                break
        return list(out.values())

    async def get_book(self, token_id: str) -> dict:
        data = await self._get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        return parse_clob_book(data)

    async def get_books(
        self, token_ids: list[str], concurrency: int = 8
    ) -> dict[str, dict]:
        sem = asyncio.Semaphore(concurrency)
        out: dict[str, dict] = {}

        async def one(tid: str) -> None:
            async with sem:
                try:
                    out[tid] = await self.get_book(tid)
                except Exception as exc:
                    log.debug("Polymarket book fetch failed for %s: %s", tid, exc)

        await asyncio.gather(*(one(t) for t in token_ids))
        return out


def token_ids(market: dict) -> list[str]:
    """CLOB token ids for a market, ordered to match `outcomes`."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw or [])


def outcomes(market: dict) -> list[str]:
    raw = market.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw or [])


def yes_token_id(market: dict) -> str | None:
    """
    The token whose payout is $1 on YES.

    Polymarket orders `clobTokenIds` to match `outcomes`, so this looks up the
    "Yes" label rather than assuming index 0 — some markets are labelled with
    the outcome names instead ("Chiefs"/"Bills"), where index 0 is arbitrary.
    """
    outs = [o.strip().lower() for o in outcomes(market)]
    ids = token_ids(market)
    if len(ids) != len(outs) or not ids:
        return None
    if "yes" in outs:
        return ids[outs.index("yes")]
    return None


def quote_from_market(market: dict) -> dict:
    """
    Top of book straight from Gamma metadata, for cheap screening.

    Gamma carries `bestBid`/`bestAsk` but no sizes, so this is only sufficient
    to rank candidates. Anything that looks like an opportunity must be
    re-checked against the CLOB book before it is believed — without size, the
    fillable quantity is unknown and the "lock" might be one share deep.
    """
    return {
        "bid": _num(market.get("bestBid")),
        "ask": _num(market.get("bestAsk")),
        "bid_size": 0,
        "ask_size": 0,
        "fetched_at": datetime.now(timezone.utc),
    }
