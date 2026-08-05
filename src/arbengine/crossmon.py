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
        f"  {'game':<34}{'K away':>8}{'K home':>8}"
        f"{'P away':>8}{'P home':>8}{'best':>8}  verdict"
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
                    f"*** ARB +{best['profit'] * 100:.2f}%  buy away on "
                    f"{best['away_venue']}, home on {best['home_venue']} [{venues}]"
                )
            else:
                verdict = f"no arb ({best['profit'] * 100:+.2f}%)"

        lines.append(
            f"  {p.label[:33]:<34}{f(ka):>8}{f(kh):>8}{f(pa):>8}{f(ph):>8}"
            f"{total:>8}  {verdict}"
        )

    return "\n".join(lines)


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
