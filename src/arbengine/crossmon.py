"""
Live Kalshi <-> Polymarket MLB price monitor.

Prints both venues' prices for every shared game on a fixed cadence and marks
whether they cross. Written to be read by a human watching for a window, so
every line shows the raw inputs rather than only a verdict — if it says no arb,
the numbers that produced that are on the same line.
"""

import asyncio
import logging
from datetime import datetime, timezone

from arbengine.crossmlb import CrossQuote, pair_games
from arbengine.source.kalshi import quote_from_market
from arbengine.source.polymarket import PolymarketClient
from arbengine.value.games import KALSHI_SLUG_TO_TEAM

log = logging.getLogger(__name__)


async def collect_pairs(kalshi_client, pm_client: PolymarketClient):
    """Discover MLB games carried by both venues."""
    kalshi_markets = await kalshi_client.list_markets(
        series_ticker="KXMLBGAME", max_pages=2
    )
    # Tag-filtered events, not the volume-ordered market sweep: the latter
    # surfaced 2 of 96 MLB games.
    pm_markets = await pm_client.markets_by_tag("mlb")
    pairs, rejects = pair_games(kalshi_markets, pm_markets)
    return pairs, rejects, {m["ticker"]: m for m in kalshi_markets}


async def live_snapshot(
    kalshi_client,
    pm_source,
    pairs: list,
    fee_multiplier: float = 0.07,
) -> list[CrossQuote]:
    """
    Fetch prices from the UNCACHED endpoints on both venues.

    This is the only path that can tell a real divergence from a stale one.
    Measured cache behaviour:

        Kalshi /markets                    Cache-Control max-age=15  (Age 1->13)
        Kalshi /markets/{t}/orderbook      no Age, X-Cache Miss  <- live
        Polymarket gamma /markets          Cache-Control max-age=300 (!)
        Polymarket CLOB /book              no Age, no Cache-Control  <- live

    The poll monitor compared a 15-second-stale Kalshi price against a current
    Polymarket one, which on a fast-moving live game manufactures exactly the
    kind of double-digit cross that then vanishes on the next tick. Gamma is
    worse still at five minutes, so it is used only for discovery, never for
    prices.
    """
    now = datetime.now(timezone.utc)

    tickers: list[str] = []
    tokens: list[str] = []
    for p in pairs:
        for slug in (p.away_slug, p.home_slug):
            tk = p.kalshi_tickers.get(slug)
            if tk:
                tickers.append(tk)
            tid = p.pm.token_for_slug(slug)
            if tid:
                tokens.append(tid)

    # pm_source is either PolymarketClient (REST) or PolymarketStream (WS).
    # Both expose get_books with the same shape, so the transport is invisible
    # here — which is also what makes the WS path testable against the REST one.
    books_k, books_p = await asyncio.gather(
        kalshi_client.get_books(tickers),
        pm_source.get_books(tokens),
    )

    out: list[CrossQuote] = []
    for p in pairs:
        cq = CrossQuote(pair=p, at=now)
        for slug in (p.away_slug, p.home_slug):
            tk = p.kalshi_tickers.get(slug)
            kb = books_k.get(tk) if tk else None
            if kb:
                cq.kalshi[slug] = kb["ask"]
                cq.kalshi_size[slug] = kb["ask_size"]
            tid = p.pm.token_for_slug(slug)
            pb = books_p.get(tid) if tid else None
            if pb:
                cq.poly[slug] = pb["ask"]
                cq.poly_size[slug] = pb["ask_size"]
        out.append(cq)
    return out


async def snapshot(
    kalshi_client,
    pm_client: PolymarketClient,
    pairs: list,
    fee_multiplier: float = 0.07,
) -> list[CrossQuote]:
    """Fetch current prices on both venues for every paired game."""
    now = datetime.now(timezone.utc)

    tickers = [t for p in pairs for t in p.kalshi_tickers.values()]
    fresh = await kalshi_client.list_markets(series_ticker="KXMLBGAME", max_pages=2)
    by_ticker = {m["ticker"]: m for m in fresh}

    token_list: list[str] = []
    for p in pairs:
        for slug in (p.away_slug, p.home_slug):
            tid = p.pm.token_for_slug(slug)
            if tid:
                token_list.append(tid)
    books = await pm_client.get_books(token_list)

    out: list[CrossQuote] = []
    for p in pairs:
        cq = CrossQuote(pair=p, at=now)
        for slug in (p.away_slug, p.home_slug):
            tk = p.kalshi_tickers.get(slug)
            if tk and tk in by_ticker:
                q = quote_from_market(by_ticker[tk])
                cq.kalshi[slug] = q["ask"]
                cq.kalshi_size[slug] = q["ask_size"]
            tid = p.pm.token_for_slug(slug)
            book = books.get(tid) if tid else None
            if book:
                cq.poly[slug] = book["ask"]
                cq.poly_size[slug] = book["ask_size"]
        out.append(cq)
    return out


def format_snapshot(quotes: list[CrossQuote], fee_multiplier: float = 0.07) -> str:
    """One block per game: both venues' prices and the arb verdict."""
    lines: list[str] = []
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    lines.append(f"\n─── {stamp} UTC " + "─" * 62)

    if not quotes:
        lines.append("  no games carried by both venues")
        return "\n".join(lines)

    header = (
        f"  {'game':<30}{'K away':>7}{'K home':>7}"
        f"{'P away':>7}{'P home':>7}{'total':>8}{'sets':>7}{'$':>8}  verdict"
    )
    lines.append(header)

    for cq in sorted(quotes, key=lambda c: (c.best(fee_multiplier) or {}).get("total", 9)):
        p = cq.pair
        ka = cq.kalshi.get(p.away_slug)
        kh = cq.kalshi.get(p.home_slug)
        pa = cq.poly.get(p.away_slug)
        ph = cq.poly.get(p.home_slug)

        def f(v):
            return f"{v:.3f}" if v is not None else "  --  "

        best = cq.best(fee_multiplier)
        if best is None:
            verdict = "incomplete quotes"
            total = "  --  "
        else:
            total = f"{best['total']:.4f}"
            if best["profit"] > 0:
                venues = (
                    f"{best['away_venue'][:1].upper()}/{best['home_venue'][:1].upper()}"
                )
                verdict = (
                    f"ARB +{best['profit'] * 100:.2f}% [{venues}] "
                    f"= ${best['dollar_profit']:.2f} max"
                )
                if best["dollar_profit"] < 1.0:
                    verdict += "  (too thin to matter)"
            else:
                verdict = f"no arb ({best['profit'] * 100:+.2f}%)"

        sets = f"{best['sets']:,}" if best else "  --  "
        dollars = f"${best['dollar_profit']:.2f}" if best else "  --  "
        lines.append(
            f"  {p.label[:29]:<30}{f(ka):>7}{f(kh):>7}{f(pa):>7}{f(ph):>7}"
            f"{total:>8}{sets:>7}{dollars:>8}  {verdict}"
        )

    return "\n".join(lines)


async def monitor_live(
    kalshi_client,
    duration_sec: float = 300.0,
    interval_sec: float = 5.0,
    fee_multiplier: float = 0.07,
    only_started: bool = True,
    min_dollar_profit: float = 0.0,
) -> dict:
    """
    Poll uncached endpoints on both venues and record how long crosses survive.

    Restricted by default to games already underway, because that is where the
    two venues plausibly diverge — and where the poll monitor's stale-cache
    artifacts appeared. A cross that survives several consecutive uncached
    reads is real; one that appears in a single read is not evidence of
    anything.
    """
    stats: dict = {
        "ticks": 0, "games": 0, "crosses": 0,
        "streaks": {}, "best": None,
    }

    async with PolymarketClient() as pm:
        pairs, rejects, _ = await collect_pairs(kalshi_client, pm)
        now = datetime.now(timezone.utc)
        if only_started:
            pairs = [
                p for p in pairs
                if p.start is not None and p.start <= now
            ]
        stats["games"] = len(pairs)

        label = "in-progress" if only_started else "all"
        print(f"\nLIVE monitor (uncached endpoints) — {len(pairs)} {label} games")
        for p in pairs:
            age = (now - p.start).total_seconds() / 60 if p.start else 0
            print(f"   {p.label:<40} started {age:.0f} min ago")
        if not pairs:
            print("   (no games in progress right now)")
            return stats

        loop = asyncio.get_event_loop()
        started = loop.time()
        streaks: dict[str, int] = {}

        while loop.time() - started < duration_sec:
            quotes = await live_snapshot(kalshi_client, pm, pairs, fee_multiplier)
            stats["ticks"] += 1
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

            for cq in quotes:
                key = cq.pair.pm.condition_id
                best = cq.best(fee_multiplier)
                if best and best["profit"] > 0 and best["dollar_profit"] >= min_dollar_profit:
                    streaks[key] = streaks.get(key, 0) + 1
                    stats["crosses"] += 1
                    run = streaks[key]
                    print(
                        f"  {stamp} {cq.pair.label[:30]:<32}"
                        f"+{best['profit'] * 100:5.2f}%  "
                        f"{best['away_venue'][:1].upper()}/{best['home_venue'][:1].upper()}  "
                        f"sets={best['sets']:>5}  ${best['dollar_profit']:>7.2f}  "
                        f"streak={run}",
                        flush=True,
                    )
                    if stats["best"] is None or best["dollar_profit"] > stats["best"]:
                        stats["best"] = best["dollar_profit"]
                else:
                    if streaks.get(key):
                        print(
                            f"  {stamp} {cq.pair.label[:30]:<32}"
                            f"cross ended after {streaks[key]} reads",
                            flush=True,
                        )
                    streaks[key] = 0

            stats["streaks"] = {k: v for k, v in streaks.items() if v}
            remaining = duration_sec - (loop.time() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval_sec, remaining))

    return stats


async def monitor(
    kalshi_client,
    duration_sec: float = 300.0,
    interval_sec: float = 30.0,
    fee_multiplier: float = 0.07,
) -> dict:
    """Poll both venues and print an annotated snapshot on each tick."""
    stats = {"ticks": 0, "arbs": 0, "games": 0}

    async with PolymarketClient() as pm:
        pairs, rejects, _ = await collect_pairs(kalshi_client, pm)
        stats["games"] = len(pairs)

        print(f"\nMLB games carried by BOTH venues: {len(pairs)}")
        if rejects:
            print(f"unmatched: {rejects}")
        for p in pairs:
            start = p.start.strftime("%m-%d %H:%M") if p.start else "?"
            print(f"   {p.label:<36} first pitch {start} UTC   pm_liq=${p.pm.liquidity:,.0f}")
        if not pairs:
            return stats

        loop = asyncio.get_event_loop()
        started = loop.time()
        while loop.time() - started < duration_sec:
            quotes = await snapshot(kalshi_client, pm, pairs, fee_multiplier)
            print(format_snapshot(quotes, fee_multiplier), flush=True)
            stats["ticks"] += 1
            for cq in quotes:
                b = cq.best(fee_multiplier)
                if b and b["profit"] > 0:
                    stats["arbs"] += 1
            remaining = duration_sec - (loop.time() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval_sec, remaining))

    return stats
