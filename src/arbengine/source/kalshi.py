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
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi
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


def _num(v: object) -> float | None:
    """Kalshi returns numerics as strings in the _fp/_dollars fields."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_book(data: dict) -> dict:
    """
    Normalize a Kalshi order book into YES-side {bid, ask, bid_size, ask_size}.

    Kalshi publishes two bid ladders and no ask ladder: one holds bids to buy
    YES, the other bids to buy NO. A resting NO bid at price p is an offer to
    sell YES at (1 - p), so:

        best YES bid  = best yes bid           (where you can sell YES)
        best YES ask  = 1 - best no bid        (where you can buy YES)

    and the size available at the YES ask is the size resting on that NO bid.
    Getting this inversion wrong silently turns every arbitrage check into
    nonsense, so it is pinned in the tests against captured fixtures.

    Two wire formats are handled:

    - `orderbook_fp` with `yes_dollars`/`no_dollars`: decimal-dollar strings,
      sorted **ascending**, so the best bid is the LAST entry.
    - legacy `orderbook` with `yes`/`no`: integer cents, sorted descending, so
      the best bid is the FIRST entry.

    The ascending-vs-descending flip is the dangerous part: reading the wrong
    end of the ladder yields a plausible-looking price that is the worst quote
    on the book rather than the best.
    """
    ob_fp = data.get("orderbook_fp")
    if ob_fp is not None:
        yes_levels = ob_fp.get("yes_dollars") or []
        no_levels = ob_fp.get("no_dollars") or []
        # Ascending: best bid is the highest price, i.e. the last entry.
        best_yes = yes_levels[-1] if yes_levels else None
        best_no = no_levels[-1] if no_levels else None

        bid = _num(best_yes[0]) if best_yes else None
        bid_size = int(_num(best_yes[1]) or 0) if best_yes else 0
        no_bid = _num(best_no[0]) if best_no else None
        ask = round(1.0 - no_bid, 4) if no_bid is not None else None
        ask_size = int(_num(best_no[1]) or 0) if best_no else 0

        return {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}

    ob = data.get("orderbook") or {}
    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []

    # Descending: best bid is the first entry, prices in integer cents.
    best_yes_bid = yes_bids[0] if yes_bids else None
    best_no_bid = no_bids[0] if no_bids else None

    bid = best_yes_bid[0] / 100.0 if best_yes_bid else None
    bid_size = int(best_yes_bid[1]) if best_yes_bid else 0
    ask = (100 - best_no_bid[0]) / 100.0 if best_no_bid else None
    ask_size = int(best_no_bid[1]) if best_no_bid else 0

    return {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}


def _levels_from_snapshot(levels: list | None) -> dict[float, float]:
    """Snapshot side -> {price: size}. Entries are ["0.0010", "26000.00"]."""
    out: dict[float, float] = {}
    for entry in levels or []:
        if len(entry) < 2:
            continue
        price, size = _num(entry[0]), _num(entry[1])
        if price is None or size is None or size <= 0:
            continue
        out[price] = size
    return out


def _book_from_levels(sides: dict[str, dict[float, float]]) -> dict:
    """
    Collapse maintained WS book state to the same YES-side top-of-book shape
    the REST paths produce, so detectors never learn which feed they came from.
    """
    yes, no = sides.get("yes") or {}, sides.get("no") or {}

    best_yes = max(yes) if yes else None
    best_no = max(no) if no else None

    return {
        "bid": best_yes,
        "bid_size": int(yes[best_yes]) if best_yes is not None else 0,
        "ask": round(1.0 - best_no, 4) if best_no is not None else None,
        "ask_size": int(no[best_no]) if best_no is not None else 0,
    }


def quote_from_market(market: dict) -> dict:
    """
    Extract the YES-side top of book straight from market metadata.

    `/markets` already carries `yes_bid_dollars`, `yes_ask_dollars` and their
    sizes, so a whole event's quotes arrive in the same paginated call as its
    strikes. Fetching one order book per market instead would mean ~188
    requests for a single BTC event, which trips the rate limit immediately and
    guarantees the legs are skewed in time. Order books are still worth pulling
    for depth beyond level one, but not for detection.
    """
    bid = _num(market.get("yes_bid_dollars"))
    ask = _num(market.get("yes_ask_dollars"))
    bid_size = int(_num(market.get("yes_bid_size_fp")) or 0)
    ask_size = int(_num(market.get("yes_ask_size_fp")) or 0)

    # A zero-size quote is not a quote. Kalshi reports 0.00/0 for an empty side
    # rather than omitting it, and treating that as a real $0.00 bid would let
    # the LP "sell" into nothing.
    if bid_size <= 0:
        bid, bid_size = None, 0
    if ask_size <= 0:
        ask, ask_size = None, 0

    return {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        ws_url: str,
        key_id: str,
        private_key: RSAPrivateKey,
        timeout: float = 15.0,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        request_interval: float = 0.15,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url
        self._key_id = key_id
        self._key = private_key
        self._http = httpx.AsyncClient(timeout=timeout)
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        # Minimum spacing between requests. Pagination fires back-to-back
        # otherwise and trips the rate limit within a few pages.
        self._request_interval = request_interval
        self._last_request = 0.0

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

    async def _throttle(self) -> None:
        """Space requests out so pagination does not trip the rate limit."""
        if self._request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._request_interval:
            await asyncio.sleep(self._request_interval - elapsed)
        self._last_request = time.monotonic()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """
        Signed GET with retry on rate limiting and transient server errors.

        Kalshi's per-key rate limit is easy to trip when paginating market
        metadata, and a 429 partway through a scan would otherwise abort the
        whole pass. 429 and 5xx are retried with exponential backoff, honouring
        Retry-After when the server sends it. 401 fails immediately — no amount
        of retrying fixes a bad key, and hammering an unauthorized key is how
        you get blocked.
        """
        url = self._base_url + path
        delay = self._retry_base_delay

        for attempt in range(self._max_retries + 1):
            try:
                await self._throttle()
                resp = await self._http.get(
                    url, headers=self._headers(path), params=params
                )

                if resp.status_code == 401:
                    raise PermissionError(
                        "Kalshi 401 Unauthorized — check KALSHI_API_KEY_ID and the private key"
                    )

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt >= self._max_retries:
                        log.warning(
                            "Kalshi %s on %s: out of retries", resp.status_code, path
                        )
                        resp.raise_for_status()

                    wait = delay
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                        except ValueError:
                            pass

                    log.warning(
                        "Kalshi %s on %s; retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, path, wait, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, self._retry_max_delay)
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError as exc:
                log.warning("Kalshi HTTP %s on %s", exc.response.status_code, path)
                raise
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    log.warning("Kalshi request error on %s: %s", path, exc)
                    raise
                log.warning(
                    "Kalshi request error on %s: %s; retrying in %.1fs", path, exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max_delay)

        raise RuntimeError(f"Kalshi GET {path} exhausted retries")

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def list_series(self, category: str | None = None) -> list[dict]:
        """
        List series metadata. Beyond discovery, this is the authoritative source
        for `fee_multiplier` and `fee_type` per series — see series_fee_scale.
        """
        params = {"category": category} if category else None
        data = await self._get("/series", params=params)
        return data.get("series", [])

    async def get_series(self, series_ticker: str) -> dict:
        data = await self._get(f"/series/{series_ticker}")
        return data.get("series", data)

    async def series_fee_scales(
        self, series_tickers: list[str] | None = None
    ) -> dict[str, float]:
        """
        Map series ticker → its fee scaling factor.

        Kalshi's quoted taker fee is quadratic in price, and each series carries
        a multiplier that scales it. Almost every series reports 1 (the standard
        rate), but a handful report 0 — genuinely fee-free. That distinction
        matters enormously here: fees are what kill most thin arbitrage, so a
        fee-free series is where marginal locks actually survive. Hardcoding
        0.07 for everything would both miss those and misprice any series Kalshi
        reprices later.

        Returns the raw scale factor; multiply FEE_MULTIPLIER by it.
        """
        series = await self.list_series()
        wanted = set(series_tickers) if series_tickers else None
        out: dict[str, float] = {}
        for s in series:
            ticker = s.get("ticker")
            if not ticker or (wanted is not None and ticker not in wanted):
                continue
            scale = s.get("fee_multiplier")
            if scale is None:
                continue
            try:
                out[ticker] = float(scale)
            except (TypeError, ValueError):
                continue
        return out

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
        max_pages: int | None = None,
    ) -> list[dict]:
        """
        List market metadata. This is where strike_type, floor_strike and
        cap_strike come from, which groups.py turns into outcome intervals.

        `max_pages` bounds an unfiltered sweep. Kalshi has tens of thousands of
        open markets, so paginating all of them just to survey what exists
        burns rate limit for no benefit — the caller says how deep to look and
        is told when the result was truncated.
        """
        markets: list[dict] = []
        cursor: str | None = None
        pages = 0
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
            pages += 1
            cursor = data.get("cursor") or None
            if not cursor:
                break
            if max_pages is not None and pages >= max_pages:
                log.info(
                    "Stopped after %d pages (%d markets); more available",
                    pages, len(markets),
                )
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
        # price (dollars) -> resting size, per side, per ticker.
        books: dict[str, dict[str, dict[float, float]]] = {
            t: {"yes": {}, "no": {}} for t in tickers
        }
        subscribe = json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        })

        # websockets validates against the OS trust store, which a python.org
        # macOS build does not populate — httpx works only because it bundles
        # certifi. Point the WS at the same CA bundle rather than disabling
        # verification, which would expose the signed key to a MITM.
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

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
                    self._ws_url, additional_headers=headers, ssl=ssl_ctx
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
                            # A side is omitted entirely when its book is empty,
                            # so rebuild both from scratch rather than merging.
                            books[ticker] = {
                                "yes": _levels_from_snapshot(
                                    payload.get("yes_dollars_fp")
                                ),
                                "no": _levels_from_snapshot(
                                    payload.get("no_dollars_fp")
                                ),
                            }
                        else:
                            # A delta is a single price level and carries a
                            # CHANGE in size, not the new size. Treating it as
                            # absolute silently corrupts depth, which then feeds
                            # the LP's size bounds and invents fillable volume.
                            side = payload.get("side")
                            if side not in ("yes", "no"):
                                continue
                            price = _num(payload.get("price_dollars"))
                            change = _num(payload.get("delta_fp"))
                            if price is None or change is None:
                                continue

                            levels = books[ticker][side]
                            new_size = levels.get(price, 0.0) + change
                            if new_size > 0:
                                levels[price] = new_size
                            else:
                                levels.pop(price, None)

                        await queue.put((
                            ticker,
                            _book_from_levels(books[ticker]),
                            datetime.now(timezone.utc),
                        ))

            except websockets.exceptions.ConnectionClosed as exc:
                log.warning("Kalshi WS closed: %s — reconnecting in %.0fs", exc, backoff)
            except Exception as exc:
                log.warning("Kalshi WS error: %s — reconnecting in %.0fs", exc, backoff)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
