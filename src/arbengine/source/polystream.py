"""
Polymarket CLOB WebSocket client.

VERIFIED AGAINST THE LIVE FEED 2026-08-05. The protocol is easy to get subtly
wrong in a way that fails silently, so the observed shapes are recorded here:

    url          wss://ws-subscriptions-clob.polymarket.com/ws/market
    subscribe    {"assets_ids": [...], "type": "market"}
    events       book | price_change | last_trade_price

The dangerous part: subscribing with the wrong payload still CONNECTS and then
delivers nothing. A commonly-suggested shape —

    {"type": "subscribe", "topic": "market", "token_ids": [...]}   # WRONG

— opens the socket cleanly and sits silent forever. There is no error and no
close frame, so a scanner built on it reports "no opportunities" indefinitely
while looking perfectly healthy. The field is `assets_ids` (note the plural on
both words), and `type` names the channel rather than the verb.

Messages arrive either as a single object or as a JSON array of them, so both
have to be handled.

Why bother, given the REST CLOB book is already uncached: latency and cost. The
poll path spends one request per token per read and still only sees the book at
poll boundaries; the stream pushes every change. That matters for live in-game
divergence, which is the open question these were built to answer.
"""

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone

import certifi
import websockets

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _num(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class TokenBook:
    """Maintained top-of-book and depth for one Polymarket token."""

    asset_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated_at: datetime | None = None

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def bid_size(self) -> float:
        b = self.best_bid
        return self.bids.get(b, 0.0) if b is not None else 0.0

    @property
    def ask_size(self) -> float:
        a = self.best_ask
        return self.asks.get(a, 0.0) if a is not None else 0.0

    def as_quote(self) -> dict:
        """
        Same shape the REST path produces, so detectors cannot tell them apart.

        Sizes stay FLOAT. Polymarket books carry fractional shares, and
        int()-ing them silently turned a real 0.75-share quote into zero depth
        — which then read as a tradeable cross with nothing behind it. Rounding
        to whole contracts is the consumer's job, because only Kalshi requires
        it.
        """
        return {
            "bid": self.best_bid,
            "ask": self.best_ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
        }

    def apply_snapshot(self, msg: dict) -> None:
        """
        Replace the book from a `book` event.

        Levels are objects: {"price": "0.001", "size": "2210518"}. Rebuilt from
        scratch rather than merged — a snapshot supersedes whatever was held,
        and merging would leave stale levels that were removed while
        disconnected.
        """
        self.bids = {}
        self.asks = {}
        for side, target in (("bids", self.bids), ("asks", self.asks)):
            for lvl in msg.get(side) or []:
                price, size = _num(lvl.get("price")), _num(lvl.get("size"))
                if price is None or size is None or size <= 0:
                    continue
                target[price] = size
        self.updated_at = datetime.now(timezone.utc)

    def apply_change(self, change: dict) -> None:
        """
        Apply one entry from a `price_change` event.

        `side` is BUY/SELL from the maker's perspective: BUY updates the bid
        ladder, SELL the ask ladder. A size of 0 removes the level — that is
        how Polymarket signals a level clearing, and treating it as a real
        zero-size level would leave a phantom best price on the book.
        """
        price, size = _num(change.get("price")), _num(change.get("size"))
        if price is None or size is None:
            return
        side = str(change.get("side", "")).upper()
        book = self.bids if side == "BUY" else self.asks if side == "SELL" else None
        if book is None:
            return
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.updated_at = datetime.now(timezone.utc)


class PolymarketStream:
    """
    Live top-of-book for a set of Polymarket tokens.

    Holds state in memory and reconnects with backoff. Callers read
    `quotes()` whenever they want a consistent view; there is no callback
    per message, because the consumers here scan a whole game at once rather
    than reacting to individual ticks.
    """

    def __init__(self, asset_ids: list[str]) -> None:
        self.asset_ids = list(dict.fromkeys(asset_ids))
        self.books: dict[str, TokenBook] = {
            a: TokenBook(asset_id=a) for a in self.asset_ids
        }
        self.connected = False
        self.messages = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "PolymarketStream":
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def wait_ready(self, timeout: float = 20.0) -> bool:
        """Block until at least one book has arrived, or give up."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if any(b.updated_at for b in self.books.values()):
                return True
            await asyncio.sleep(0.2)
        return False

    # ── Feed ──────────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        ctx = ssl.create_default_context(cafile=certifi.where())
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    WS_URL, ssl=ctx, ping_interval=20, ping_timeout=20
                ) as ws:
                    await ws.send(
                        json.dumps({"assets_ids": self.asset_ids, "type": "market"})
                    )
                    self.connected = True
                    backoff = 1.0
                    log.info(
                        "Polymarket stream subscribed to %d tokens",
                        len(self.asset_ids),
                    )
                    async for raw in ws:
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Polymarket stream error: %s — reconnecting", exc)
            finally:
                self.connected = False

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        for msg in data if isinstance(data, list) else [data]:
            if not isinstance(msg, dict):
                continue
            self.messages += 1
            kind = msg.get("event_type")

            if kind == "book":
                book = self.books.get(msg.get("asset_id", ""))
                if book is not None:
                    book.apply_snapshot(msg)

            elif kind == "price_change":
                for change in msg.get("price_changes") or []:
                    book = self.books.get(change.get("asset_id", ""))
                    if book is not None:
                        book.apply_change(change)

            # last_trade_price carries no book information; ignored deliberately
            # rather than silently mis-applied as a quote.

    # ── Reading ───────────────────────────────────────────────────────────────

    async def get_books(
        self, token_ids: list[str], concurrency: int = 0
    ) -> dict[str, dict]:
        """
        Drop-in replacement for PolymarketClient.get_books.

        Same signature and same return shape, so the monitors can take either
        transport without branching. Reads maintained state rather than making
        requests, so `concurrency` is accepted and ignored.

        Tokens that have not yet received a book are omitted rather than
        returned empty — an absent quote is skipped downstream, whereas a
        None-priced one would be treated as a real missing side.
        """
        live = self.quotes()
        return {t: live[t] for t in token_ids if t in live}

    def quotes(self) -> dict[str, dict]:
        """Current top-of-book per token, in the REST path's shape."""
        return {
            a: b.as_quote()
            for a, b in self.books.items()
            if b.updated_at is not None
        }

    def staleness(self) -> dict[str, float]:
        """Seconds since each token last updated — a live feed can still go quiet."""
        now = datetime.now(timezone.utc)
        return {
            a: (now - b.updated_at).total_seconds()
            for a, b in self.books.items()
            if b.updated_at is not None
        }
