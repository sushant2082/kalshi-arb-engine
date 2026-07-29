"""
CLI entrypoint. Detection and paper trading only — never places a real order.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from arbengine import alerts, backtest as bt, storage
from arbengine.config import Settings
from arbengine.models import opportunity_key
from arbengine.paper import PaperBroker, summarize
from arbengine.scanner import (
    PersistenceTracker,
    build_groups,
    near_miss,
    refresh_group,
    scan_all,
    stream_scan,
)
from arbengine.source.kalshi import KalshiClient, load_private_key

log = logging.getLogger("arbengine")


def _client(settings: Settings, key) -> KalshiClient:
    """Build a client paced to the configured Kalshi tier."""
    return KalshiClient(
        settings.kalshi_base_url,
        settings.kalshi_ws_url,
        settings.kalshi_api_key_id,
        key,
        read_budget=settings.kalshi_read_budget,
        request_cost=settings.kalshi_request_cost,
        bucket_capacity=settings.kalshi_read_budget * settings.kalshi_bucket_seconds,
        safety_factor=settings.kalshi_rate_safety,
    )


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


BRACKET_STRIKE_TYPES = (
    "between", "greater", "greater_or_equal", "less", "less_or_equal",
)

# Categories where outcome variables are numeric and get bracketed. Sports,
# Politics and Entertainment are overwhelmingly binary or parlay markets, and
# sweeping them just burns rate limit.
DISCOVER_CATEGORIES = (
    "Crypto",
    "Climate and Weather",
    "Economics",
    "Financials",
    "Commodities",
)


async def _probe_series(client: KalshiClient, ticker: str, fee_scale: float) -> dict | None:
    """Sample one series' open markets and score it as an arbitrage target."""
    try:
        markets = await client.list_markets(series_ticker=ticker, max_pages=1)
    except Exception:
        return None
    if not markets:
        return None

    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker") or "", []).append(m)

    bracketed = sum(
        1 for m in markets if m.get("strike_type") in BRACKET_STRIKE_TYPES
    )
    # Events with 3+ legs are where coherence actually has room to break; a
    # 2-leg event is effectively a binary market.
    multi_leg = sum(1 for ms in by_event.values() if len(ms) >= 3)
    widest = max((len(ms) for ms in by_event.values()), default=0)

    if not bracketed or not multi_leg:
        return None

    return {
        "ticker": ticker,
        "markets": len(markets),
        "events": len(by_event),
        "bracketed": bracketed,
        "multi_leg_events": multi_leg,
        "widest_event": widest,
        "fee_scale": fee_scale,
    }


async def _discover(settings: Settings) -> None:
    """List candidate bracketed/laddered series so TARGET_SERIES can be set."""
    key = load_private_key(settings.kalshi_private_key_path)
    async with _client(settings, key) as client:
        log.info("Reading series metadata...")
        all_series = await client.list_series()
        fee_scales = {
            s.get("ticker"): float(s.get("fee_multiplier", 1) or 0)
            for s in all_series
            if s.get("ticker")
        }

        candidates = [
            s for s in all_series
            if s.get("category") in DISCOVER_CATEGORIES
            # Recurring series regenerate brackets constantly and are the most
            # likely to have live, thin, uncoordinated books.
            and s.get("frequency") in ("hourly", "daily", "weekly", "monthly")
        ]
        log.info(
            "%d series in bracket-friendly categories; probing open markets...",
            len(candidates),
        )

        results = []
        probed = 0
        for s in candidates:
            ticker = s.get("ticker")
            row = await _probe_series(client, ticker, fee_scales.get(ticker, 1.0))
            probed += 1
            if row:
                row["category"] = s.get("category")
                row["frequency"] = s.get("frequency")
                results.append(row)
            if probed % 50 == 0:
                log.info("  probed %d/%d, %d live", probed, len(candidates), len(results))

        if not results:
            print("\nNo bracketed series with open multi-leg events found.\n")
            return

        # Rank by the widest single event first. Coherence breaks where many
        # thin related contracts share one outcome variable, so one 75-leg
        # bracket set is a far better target than a dozen 3-leg events — more
        # pairs that can contradict each other, and thinner books on each leg.
        results.sort(key=lambda r: (-r["widest_event"], -r["multi_leg_events"]))

        print(f"\n{len(results)} candidate series with live bracketed events\n")
        header = (
            f"{'SERIES':<22} {'CATEGORY':<20} {'FREQ':<8} {'MKTS':>5} "
            f"{'EVENTS':>6} {'BRACKET':>8} {'3+LEG':>6} {'WIDEST':>7} {'FEE':>5}"
        )
        print(header)
        print("─" * len(header))
        for r in results[:40]:
            print(
                f"{r['ticker']:<22} {r['category']:<20} {r['frequency']:<8} "
                f"{r['markets']:>5} {r['events']:>6} {r['bracketed']:>8} "
                f"{r['multi_leg_events']:>6} {r['widest_event']:>7} "
                f"{r['fee_scale']:>5.2g}"
            )

        top = [r["ticker"] for r in results[:8]]
        print(f"\nSuggested:\n  TARGET_SERIES={','.join(top)}\n")

        free = [r["ticker"] for r in results if r["fee_scale"] == 0]
        if free:
            print(
                f"Fee-free series (FEE=0), where thin locks actually survive:\n"
                f"  {','.join(free)}\n"
            )


async def _run_stream(settings: Settings, duration_sec: float | None) -> None:
    """Event-driven scan over the WebSocket feed."""
    key = load_private_key(settings.kalshi_private_key_path)
    conn = await storage.init_db(settings.db_path)
    tracker = PersistenceTracker()
    broker = _make_broker(settings)
    positions: list = []

    async with _client(settings, key) as client:
        log.info("Discovering groups for series: %s", ", ".join(settings.target_series))
        groups = await build_groups(client, settings)
        if not groups:
            log.error("No validated groups. Run `arbengine discover` to pick targets.")
            await conn.close()
            return

        legs = sum(len(g.tickers) for g in groups)
        log.info("Streaming %d groups (%d markets)", len(groups), legs)

        async def on_opportunity(opp, group) -> None:
            now = datetime.now(timezone.utc)
            tracked = tracker.observe(opp).model_copy(update={"last_seen": now})
            await storage.upsert_opportunity(conn, tracked)
            alerts.print_opportunity(tracked, settings.max_leg_count_alert)
            alerts.append_to_csv(
                tracked, settings.csv_output_path, settings.max_leg_count_alert
            )
            if broker:
                pos = broker.attempt(tracked, now)
                if pos:
                    pos.id = await storage.save_position(conn, pos)
                    positions.append(pos)
                    alerts.print_position(pos)

        try:
            stats = await stream_scan(
                client, groups, settings, on_opportunity, stop_after_sec=duration_sec
            )
            log.info(
                "Stream ended: %d book updates, %d group re-scans, %d opportunities",
                stats["updates"], stats["scans"], stats["opportunities"],
            )
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            if broker and positions:
                alerts.print_summary(summarize(positions, broker.starting_bankroll))
            await _print_persistence(conn)
            await conn.close()


async def _run_backtest(settings: Settings) -> None:
    """
    Prove the detect -> fill -> settle path on real market geometry by
    perturbing live quotes until coherence breaks.

    Until a genuine opportunity appears, this is the only thing standing
    between "the code compiles" and "the code works when it matters".
    """
    key = load_private_key(settings.kalshi_private_key_path)
    async with _client(settings, key) as client:
        groups = await build_groups(client, settings)

    if not groups:
        log.error("No validated groups to backtest.")
        return

    print(f"\nInjecting synthetic dislocations into {len(groups)} live groups\n")
    all_results = []
    for g in groups:
        results = bt.backtest_group(g, settings)
        if not results:
            continue
        print(f"{g.group_id}  ({g.shape}, {len(g.contracts)} legs, "
              f"fee_scale={g.fee_scale:g})")
        for r in results:
            print(r)
            for note in r.notes:
                print(f"      ! {note}")
        all_results.extend(results)

    s = bt.summarize(all_results)
    print("\n" + "─" * 70)
    print(
        f"  {s['scenarios']} scenarios: {s['passed']} verified riskless, "
        f"{s['failed']} fired but NOT riskless, {s['missed']} never fired"
    )
    if s["failed"]:
        print("\n  FAILURES — a portfolio that loses in some state is not arbitrage:")
        for r in s["failures"]:
            print(f"    {r.group_id} / {r.scenario}: worst=${r.worst_pnl:+.4f}")
    print("─" * 70 + "\n")


def _make_broker(settings: Settings) -> PaperBroker | None:
    if not settings.paper_enabled:
        return None
    return PaperBroker(
        bankroll=settings.paper_bankroll,
        max_sets_per_opp=settings.paper_max_sets_per_opp,
        leg_fill_prob=settings.paper_leg_fill_prob,
        slippage_cents=settings.paper_slippage_cents,
        fee_multiplier=settings.fee_multiplier,
    )


async def _print_persistence(conn) -> None:
    stats = await storage.persistence_stats(conn)
    if not stats:
        return
    print("── Persistence by type " + "─" * 42)
    for row in stats:
        print(
            f"  {row['type']:<15} n={row['n']:<5} "
            f"mean={row['mean_sec'] or 0:.1f}s  "
            f"max={row['max_sec'] or 0:.1f}s  "
            f"mean_profit=${row['mean_profit'] or 0:.2f}"
        )
    print("─" * 64)


async def _run_scan(settings: Settings, once: bool, max_iterations: int | None) -> None:
    key = load_private_key(settings.kalshi_private_key_path)
    conn = await storage.init_db(settings.db_path)
    tracker = PersistenceTracker()
    broker = (
        PaperBroker(
            bankroll=settings.paper_bankroll,
            max_sets_per_opp=settings.paper_max_sets_per_opp,
            leg_fill_prob=settings.paper_leg_fill_prob,
            slippage_cents=settings.paper_slippage_cents,
            fee_multiplier=settings.fee_multiplier,
        )
        if settings.paper_enabled
        else None
    )
    positions = []

    async with _client(settings, key) as client:
        log.info("Discovering groups for series: %s", ", ".join(settings.target_series))
        groups = await build_groups(client, settings)

        if not groups:
            log.error(
                "No validated groups. Check TARGET_SERIES — run `arbengine discover` "
                "to see which series currently have bracketed markets."
            )
            await conn.close()
            return

        iteration = 0
        try:
            while True:
                iteration += 1
                now = datetime.now(timezone.utc)

                groups = [await refresh_group(client, g) for g in groups]
                for g in groups:
                    await storage.save_snapshots(conn, g.contracts)

                found = scan_all(groups, settings, now)
                seen_keys = set()

                for opp in found:
                    tracked = tracker.observe(opp)
                    seen_keys.add(opportunity_key(tracked))
                    tracked = tracked.model_copy(update={"last_seen": now})

                    await storage.upsert_opportunity(conn, tracked)
                    alerts.print_opportunity(tracked, settings.max_leg_count_alert)
                    alerts.append_to_csv(
                        tracked, settings.csv_output_path, settings.max_leg_count_alert
                    )

                    if broker:
                        pos = broker.attempt(tracked, now)
                        if pos:
                            pos.id = await storage.save_position(conn, pos)
                            positions.append(pos)
                            alerts.print_position(pos)

                tracker.expire_absent(seen_keys)

                if not found:
                    # "No violations" alone cannot be distinguished from a
                    # detector that is silently inert, so report how close the
                    # tightest groups actually came.
                    margins = [
                        m for m in (near_miss(g, settings) for g in groups)
                        if m["monotonic_margin"] is not None
                    ]
                    margins.sort(key=lambda m: -m["monotonic_margin"])
                    log.info(
                        "Scan %d: no violations across %d groups", iteration, len(groups)
                    )
                    for m in margins[:3]:
                        log.info(
                            "    closest: %s (%s, %d legs) %.4f from inverting",
                            m["group_id"], m["shape"], m["legs"],
                            -m["monotonic_margin"],
                        )

                if once or (max_iterations and iteration >= max_iterations):
                    break
                await asyncio.sleep(settings.kalshi_poll_sec)

        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            if broker and positions:
                alerts.print_summary(summarize(positions, broker.starting_bankroll))

            stats = await storage.persistence_stats(conn)
            if stats:
                print("── Persistence by type " + "─" * 42)
                for row in stats:
                    print(
                        f"  {row['type']:<15} n={row['n']:<5} "
                        f"mean={row['mean_sec'] or 0:.1f}s  "
                        f"max={row['max_sec'] or 0:.1f}s  "
                        f"mean_profit=${row['mean_profit'] or 0:.2f}"
                    )
                print("─" * 64)
            await conn.close()


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="arbengine",
        description="Kalshi static-arbitrage detection engine (read-only, paper trading only)",
    )
    parser.add_argument(
        "command", nargs="?", default="scan",
        choices=["scan", "stream", "discover", "backtest"],
        help=(
            "scan: REST polling loop. stream: event-driven WebSocket scan. "
            "discover: list candidate series. backtest: inject synthetic "
            "dislocations into live groups and verify the lock end to end."
        ),
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="stream only: stop after N seconds",
    )
    parser.add_argument("--once", action="store_true", help="single scan pass, then exit")
    parser.add_argument("--iterations", type=int, default=None, help="stop after N passes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    settings = Settings()

    if not settings.kalshi_api_key_id:
        log.error("KALSHI_API_KEY_ID is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    try:
        if args.command == "discover":
            asyncio.run(_discover(settings))
        elif args.command == "stream":
            asyncio.run(_run_stream(settings, args.duration))
        elif args.command == "backtest":
            asyncio.run(_run_backtest(settings))
        else:
            asyncio.run(_run_scan(settings, args.once, args.iterations))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
