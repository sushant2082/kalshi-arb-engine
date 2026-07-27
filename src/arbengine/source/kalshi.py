"""
Read-only Kalshi market-data client.

Hard boundary: this module issues GET requests and WebSocket subscriptions
only. There is no order placement, cancellation, or fund movement anywhere in
this file, and nothing here should ever grow one — execution is a separate
project. The signing helper is deliberately not exposed for arbitrary methods.
"""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets
import websockets.exceptions
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

log = logging.getLogger(__name__)

# Depth needed for arbitrage sizing. Top-of-book alone understates fillable
# size; a handful of levels is enough for the depth-bounded LP without
# ballooning the payload.
ORDERBOOK_DEPTH = 10

_READ_ONLY_METHODS = {"GET"}


def load_private_key(path: Path) -> RSAPrivateKey:
    try:
        pem = Path(path).read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"Kalshi private key not found at {path}")
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError(f"Expected an RSA private key, got {type(key)}")
    return key


def _sign(private_key: RSAPrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    """RSA-PSS over timestamp + METHOD + /path (no query string, per Kalshi v2)."""
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def parse_book(data: dict) -> dict:
    """
    Normalize a Kalshi order book into YES-side {bid, ask, bid_size, ask_size}.

    Kalshi publishes two bid ladders and no ask ladder: `yes` holds bids to buy
    YES, `no` holds bids to buy NO, both in cents descending. A resting NO bid
    at price p is an offer to sell YES at (100 - p), so:

        best YES bid  = yes[0].price            (where you can sell YES)
        best YES ask  = 100 - no[0].price       (where you can buy YES)

    and the size available at the YES ask is the size resting on that NO bid.
    Getting this inversion wrong silently turns every arbitrage check into
    nonsense, so it is asserted in the tests against a captured fixture.
    """
    ob = data.get("orderbook") or {}
    yes_bids: list[list[int]] = ob.get("yes") or []
    no_bids: list[list[int]] = ob.get("no") or []

    best_yes_bid = yes_bids[0] if yes_bids else None
    best_no_bid = no_bids[0] if no_bids else None

    bid = best_yes_bid[0] / 100.0 if best_yes_bid else None
    bid_size = best_yes_bid[1] if best_yes_bid else 0
    ask = (100 - best_no_bid[0]) / 100.0 if best_no_bid else None
    ask_size = best_no_bid[1] if best_no_bid else 0

    return {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        ws_url: str,
        key_id: str,
        private_key: RSAPrivateKey,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url
        self._key_id = key_id
        self._key = private_key
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self, path: str) -> dict[str, str]:
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": _sign(self._key, ts, "GET", path),
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = self._base_url + path
        try:
            resp = await self._http.get(url, headers=self._headers(path), params=params)
            if resp.status_code == 401:
                raise PermissionError(
                    "Kalshi 401 Unauthorized — check KALSHI_API_KEY_ID and the private key"
                )
            if resp.status_code == 429:
                log.warning("Kalshi rate limited on %s; backing off", path)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning("Kalshi HTTP %s on %s", exc.response.status_code, path)
            raise
        except httpx.RequestError as exc:
            log.warning("Kalshi request error on %s: %s", path, exc)
            raise

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def list_series(self, category: str | None = None) -> list[dict]:
        params = {"category": category} if category else None
        data = await self._get("/series", params=params)
        return data.get("series", [])

    async def list_events(
        self, series_ticker: str, status: str = "open", limit: int = 200
    ) -> list[dict]:
        """List events for a series, following cursor pagination."""
        events: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": status,
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/events", params=params)
            events.extend(data.get("events", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return events

    async def list_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str = "open",
        limit: int = 200,
    ) -> list[dict]:
        """
        List market metadata. This is where strike_type, floor_strike and
        cap_strike come from, which groups.py turns into outcome intervals.
        """
        markets: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"status": status, "limit": limit}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return markets

    # ── Quotes ────────────────────────────────────────────────────────────────

    async def get_orderbook(self, ticker: str) -> dict:
        """Return normalized {bid, ask, bid_size, ask_size} for one market."""
        data = await self._get(
            f"/markets/{ticker}/orderbook", params={"depth": ORDERBOOK_DEPTH}
        )
        return parse_book(data)

    async def get_books(
        self, tickers: list[str], concurrency: int = 8
    ) -> dict[str, dict]:
        """
        Fetch books for many markets concurrently, bounded so we do not trip
        Kalshi's rate limit. Failed fetches are omitted rather than raising —
        a group missing a leg is skipped downstream, which is the safe outcome.
        """
        sem = asyncio.Semaphore(concurrency)
        out: dict[str, dict] = {}

        async def one(ticker: str) -> None:
            async with sem:
                try:
                    out[ticker] = await self.get_orderbook(ticker)
                except (httpx.HTTPStatusError, httpx.RequestError, PermissionError) as exc:
                    log.debug("Book fetch failed for %s: %s", ticker, exc)

        await asyncio.gather(*(one(t) for t in tickers))
        return out

    async def get_market(self, ticker: str) -> dict:
        """Single market detail, including `result` once the market settles."""
        data = await self._get(f"/markets/{ticker}")
        return data.get("market", {})

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def stream_books(
        self, tickers: list[str], queue: asyncio.Queue
    ) -> None:
        """
        Maintain local book state from orderbook_delta and push
        (ticker, normalized_book, timestamp) on every update.

        Arbitrage windows are short, so REST polling measures persistence but
        understates how many opportunities existed. The WS path is what makes
        the persistence statistics honest.
        """
        books: dict[str, dict[str, list]] = {t: {"yes": [], "no": []} for t in tickers}
        subscribe = json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        })

        backoff = 1.0
        while True:
            try:
                path = "/trade-api/ws/v2"
                headers = {
                    "KALSHI-ACCESS-KEY": self._key_id,
                    "KALSHI-ACCESS-TIMESTAMP": str(int(time.time() * 1000)),
                    "KALSHI-ACCESS-SIGNATURE": _sign(
                        self._key, int(time.time() * 1000), "GET", path
                    ),
                }
                async with websockets.connect(
                    self._ws_url, additional_headers=headers
                ) as ws:
                    await ws.send(subscribe)
                    log.info("Subscribed to orderbook_delta for %d markets", len(tickers))
                    backoff = 1.0

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type")
                        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
                            continue

                        payload = msg.get("msg") or {}
                        ticker = payload.get("market_ticker", "")
                        if ticker not in books:
                            continue

                        if msg_type == "orderbook_snapshot":
                            books[ticker] = {
                                "yes": payload.get("yes") or [],
                                "no": payload.get("no") or [],
                            }
                        else:
                            for side in ("yes", "no"):
                                deltas = payload.get(side) or []
                                levels = {e[0]: e[1] for e in books[ticker][side]}
                                for price, qty in deltas:
                                    if qty == 0:
                                        levels.pop(price, None)
                                    else:
                                        levels[price] = qty
                                books[ticker][side] = sorted(
                                    ([p, q] for p, q in levels.items()),
                                    key=lambda x: -x[0],
                                )

                        await queue.put((
                            ticker,
                            parse_book({"orderbook": books[ticker]}),
                            datetime.now(timezone.utc),
                        ))

            except websockets.exceptions.ConnectionClosed as exc:
                log.warning("Kalshi WS closed: %s — reconnecting in %.0fs", exc, backoff)
            except Exception as exc:
                log.warning("Kalshi WS error: %s — reconnecting in %.0fs", exc, backoff)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
