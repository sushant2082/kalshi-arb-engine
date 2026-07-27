"""
CLI entrypoint. Detection and paper trading only — never places a real order.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from arbengine import alerts, storage
from arbengine.config import Settings
from arbengine.models import opportunity_key
from arbengine.paper import PaperBroker, summarize
from arbengine.scanner import (
    PersistenceTracker,
    build_groups,
    refresh_group,
    scan_all,
)
from arbengine.source.kalshi import KalshiClient, load_private_key

log = logging.getLogger("arbengine")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def _discover(settings: Settings) -> None:
    """List candidate bracketed/laddered series so TARGET_SERIES can be set."""
    key = load_private_key(settings.kalshi_private_key_path)
    async with KalshiClient(
        settings.kalshi_base_url, settings.kalshi_ws_url,
        settings.kalshi_api_key_id, key,
    ) as client:
        markets = await client.list_markets()
        by_series: dict[str, list[dict]] = {}
        for m in markets:
            series = (m.get("ticker") or "").split("-")[0]
            by_series.setdefault(series, []).append(m)

        print(f"\n{len(markets)} open markets across {len(by_series)} series\n")
        print(f"{'SERIES':<20} {'MKTS':>5} {'EVENTS':>7} {'BRACKETED':>10}  SAMPLE")
        print("─" * 90)

        rows = []
        for series, ms in by_series.items():
            events = {m.get("event_ticker") for m in ms}
            bracketed = sum(
                1 for m in ms
                if m.get("strike_type") in
                ("between", "greater", "greater_or_equal", "less", "less_or_equal")
            )
            rows.append((series, len(ms), len(events), bracketed, ms[0].get("ticker", "")))

        # Rank by bracket density — that is where coherence actually breaks.
        rows.sort(key=lambda r: (-r[3], -r[1]))
        for series, n, n_ev, brk, sample in rows[:40]:
            print(f"{series:<20} {n:>5} {n_ev:>7} {brk:>10}  {sample}")

        print(
            "\nSet TARGET_SERIES to the series with high bracket counts and "
            "many markets per event.\nTwo-sided moneylines (BRACKETED=0) are "
            "not worth scanning — they almost never violate coherence.\n"
        )


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

    async with KalshiClient(
        settings.kalshi_base_url, settings.kalshi_ws_url,
        settings.kalshi_api_key_id, key,
    ) as client:
        log.info("Discovering groups for series: %s", ", ".join(settings.target_series))
        groups = await build_groups(client, settings)

        if not groups:
            log.error(
                "No validated groups. Check TARGET_SERIES — run `arbengine discover` "
                "to see which series currently have bracketed markets."
            )
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
                    log.info(
                        "Scan %d: no violations across %d groups", iteration, len(groups)
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
        "command", nargs="?", default="scan", choices=["scan", "discover"],
        help="scan: run the detection loop. discover: list candidate series.",
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
        else:
            asyncio.run(_run_scan(settings, args.once, args.iterations))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
