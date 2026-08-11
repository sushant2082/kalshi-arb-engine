"""
CLI entrypoint. Detection and paper trading only — never places a real order.
"""

import argparse
import asyncio
import contextlib
import logging
import time
import sys
from datetime import datetime, timezone

from arbengine import alerts, backtest as bt, storage
from arbengine.dashboard import DashboardState, render
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
    # Python fully buffers stdout when it is not a tty, so a long run whose
    # output is redirected to a file writes nothing for minutes and looks hung.
    # That matters here: these are hour-plus scans, usually redirected, often
    # on a remote host where "no output" is indistinguishable from "crashed".
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

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


async def _run_stream(
    settings: Settings, duration_sec: float | None, ui: bool = False
) -> None:
    """Event-driven scan over the WebSocket feed."""
    if ui:
        return await _run_stream_ui(settings, duration_sec)
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


async def _run_stream_ui(settings: Settings, duration_sec: float | None) -> None:
    """Stream with the live terminal dashboard."""
    from rich.console import Console
    from rich.live import Live

    from arbengine.scanner import near_miss

    console = Console()
    key = load_private_key(settings.kalshi_private_key_path)
    conn = await storage.init_db(settings.db_path)
    tracker = PersistenceTracker()
    broker = _make_broker(settings)
    state = DashboardState(
        starting_bankroll=broker.starting_bankroll if broker else 0.0
    )
    positions: list = []

    async with _client(settings, key) as client:
        state.log("discovering groups…")
        with Live(render(state), console=console, refresh_per_second=4,
                  screen=True) as live:
            groups = await build_groups(client, settings)
            if not groups:
                state.log("no validated groups — check TARGET_SERIES", "bold red")
                live.update(render(state))
                await asyncio.sleep(3)
                await conn.close()
                return

            state.groups = len(groups)
            state.markets = sum(len(g.tickers) for g in groups)
            state.near_misses = [near_miss(g, settings) for g in groups]
            state.log(f"streaming {state.groups} groups", "cyan")
            live.update(render(state))

            async def on_opportunity(opp, group) -> None:
                now = datetime.now(timezone.utc)
                tracked = tracker.observe(opp).model_copy(
                    update={"last_seen": now}
                )
                await storage.upsert_opportunity(conn, tracked)
                alerts.append_to_csv(
                    tracked, settings.csv_output_path,
                    settings.max_leg_count_alert,
                )
                state.record_opportunity(tracked)
                if broker:
                    pos = broker.attempt(tracked, now)
                    if pos:
                        pos.id = await storage.save_position(conn, pos)
                        positions.append(pos)
                        state.record_position(pos)
                live.update(render(state))

            # Refresh the dashboard on a timer independent of the feed, so a
            # quiet market still shows a live clock rather than looking hung.
            async def repaint() -> None:
                while True:
                    await asyncio.sleep(0.5)
                    state.near_misses = [near_miss(g, settings) for g in groups]
                    live.update(render(state))

            painter = asyncio.create_task(repaint())
            try:
                stats = await stream_scan(
                    client, groups, settings, on_opportunity,
                    stop_after_sec=duration_sec, state=state,
                )
                state.updates = stats["updates"]
                state.scans = stats["scans"]
            finally:
                painter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await painter
                live.update(render(state))

    if broker and positions:
        alerts.print_summary(summarize(positions, broker.starting_bankroll))
    await _print_persistence(conn)
    await conn.close()


async def _run_demo(settings: Settings, duration_sec: float = 60.0) -> None:
    """
    Drive the dashboard with SYNTHETIC dislocations against live market
    geometry.

    Real prices, real groups, real payoff matrices, real fee arithmetic and the
    real paper broker — only the quote perturbation is injected. This exists
    because the live books have not yet crossed, and a paper-trading path that
    has never executed is a path nobody should trust. Every trade here is
    labelled synthetic and written to a separate database so it can never be
    confused with a real detection in the P&L.
    """
    import random

    from rich.console import Console
    from rich.live import Live

    from arbengine import backtest as bt
    from arbengine.scanner import near_miss, scan_group

    console = Console()
    key = load_private_key(settings.kalshi_private_key_path)
    demo_db = settings.db_path.with_name("arbengine-demo.db")
    conn = await storage.init_db(demo_db)
    broker = _make_broker(settings)
    state = DashboardState(
        starting_bankroll=broker.starting_bankroll if broker else 0.0,
        mode="demo (SYNTHETIC dislocations)",
    )
    rng = random.Random(20260802)
    positions: list = []

    async with _client(settings, key) as client:
        with Live(render(state), console=console, refresh_per_second=6,
                  screen=True) as live:
            state.log("loading live market geometry…")
            live.update(render(state))
            groups = await build_groups(client, settings)
            if not groups:
                state.log("no validated groups", "bold red")
                live.update(render(state))
                await asyncio.sleep(3)
                await conn.close()
                return

            state.groups = len(groups)
            state.markets = sum(len(g.tickers) for g in groups)
            state.near_misses = [near_miss(g, settings) for g in groups]
            state.log(
                f"{state.groups} live groups — injecting synthetic dislocations",
                "yellow",
            )
            live.update(render(state))

            started = time.monotonic()
            while time.monotonic() - started < duration_sec:
                group = rng.choice(groups)
                margin = rng.choice([0.05, 0.04, 0.03, 0.02])

                perturbed = None
                if group.shape == "bracket":
                    total = sum(
                        c.ask for c in group.contracts if c.ask is not None
                    )
                    if total > 0:
                        perturbed = bt._cheapen_all_asks(
                            group, (1.0 - margin) / total
                        )
                if perturbed is None:
                    perturbed = bt._invert_ladder_pair(group, margin)
                if perturbed is None:
                    await asyncio.sleep(0.2)
                    continue

                now = datetime.now(timezone.utc)
                found = scan_group(perturbed, settings, now)
                state.scans += 1
                state.updates += len(perturbed.contracts)

                for opp in found:
                    await storage.upsert_opportunity(conn, opp)
                    state.record_opportunity(opp)
                    live.update(render(state))
                    await asyncio.sleep(0.4)

                    if broker:
                        pos = broker.attempt(opp, now)
                        if pos:
                            pos.id = await storage.save_position(conn, pos)
                            positions.append(pos)
                            state.record_position(pos)
                            live.update(render(state))
                            await asyncio.sleep(0.4)

                            # Settle against a randomly drawn outcome state, so
                            # a broken hedge shows its real loss rather than
                            # being quietly assumed to win.
                            outcome = rng.randrange(perturbed.state_space.n)
                            settled = broker.settle(
                                pos, perturbed, outcome, datetime.now(timezone.utc)
                            )
                            settled.id = pos.id
                            await storage.settle_position(conn, settled)
                            state.settle_position(settled)
                            positions[-1] = settled
                            live.update(render(state))
                            await asyncio.sleep(0.5)

                await asyncio.sleep(0.3)

            state.log("demo complete", "cyan")
            live.update(render(state))
            await asyncio.sleep(1.5)

    if broker and positions:
        alerts.print_summary(summarize(positions, broker.starting_bankroll))
    console.print(
        "[yellow]These were SYNTHETIC dislocations against live prices, "
        f"written to {demo_db.name} — not real detections.[/yellow]"
    )
    await conn.close()


async def _run_crossmlb(
    settings: Settings, duration_sec: float | None, interval_sec: float
) -> None:
    """Live Kalshi <-> Polymarket MLB monitor."""
    from arbengine.crossmon import monitor

    key = load_private_key(settings.kalshi_private_key_path)
    async with _client(settings, key) as client:
        stats = await monitor(
            client,
            duration_sec=duration_sec or 300.0,
            interval_sec=interval_sec,
            fee_multiplier=settings.fee_multiplier,
        )
    print(
        f"\n{stats['ticks']} snapshots over {stats['games']} shared games; "
        f"{stats['arbs']} game-ticks showed a cross below $1.\n"
    )


async def _run_crosslive(
    settings: Settings, duration_sec: float | None, interval_sec: float
) -> None:
    """Live cross-venue monitor on uncached endpoints, in-progress games only."""
    from arbengine.crossmon import monitor_live

    key = load_private_key(settings.kalshi_private_key_path)
    async with _client(settings, key) as client:
        stats = await monitor_live(
            client,
            duration_sec=duration_sec or 300.0,
            interval_sec=interval_sec if interval_sec != 30.0 else 5.0,
            fee_multiplier=settings.fee_multiplier,
        )
    print(
        f"\n{stats['ticks']} reads over {stats['games']} in-progress games; "
        f"{stats['crosses']} crosses seen."
    )
    if stats["best"] is not None:
        print(f"largest fillable cross: ${stats['best']:.2f}")
    print()


async def _run_crossrec(
    settings: Settings, duration_sec: float | None, interval_sec: float
) -> None:
    """Record a full game's worth of cross-venue reads to SQLite."""
    from arbengine.crossrec import record_games, summarize

    db = settings.db_path.with_name("crossreads.db")
    key = load_private_key(settings.kalshi_private_key_path)
    hours = (duration_sec or 10800.0) / 3600.0
    print(f"\nRecording cross-venue reads for {hours:.1f}h -> {db.name}")
    print("Logging every read, including non-crossing ones — otherwise a rare")
    print("event and a broken scanner look identical afterwards.\n")

    async with _client(settings, key) as client:
        stats = await record_games(
            client, db,
            duration_sec=duration_sec or 10800.0,
            interval_sec=interval_sec if interval_sec != 30.0 else 5.0,
            fee_multiplier=settings.fee_multiplier,
        )

    print(f"\n{stats['reads']:,} reads across {len(stats['games'])} games; "
          f"{stats['crosses']:,} crossed.")
    s = await summarize(db)
    if s["per_game"]:
        print(f"\n{'game':<38}{'reads':>7}{'cross':>7}{'max%':>8}{'max$':>9}")
        for g in s["per_game"][:12]:
            mp = (g["max_profit"] or 0) * 100
            md = g["max_dollars"] or 0
            print(f"  {str(g['game'])[:35]:<36}{g['reads']:>7}"
                  f"{g['crosses'] or 0:>7}{mp:>8.2f}{md:>9.2f}")
    if s["by_phase"]:
        print(f"\nby game phase (minutes since first pitch):")
        print(f"  {'phase':>8}{'reads':>8}{'crosses':>9}{'max%':>8}")
        for b in s["by_phase"]:
            print(f"  {b['bucket']:>6}m{b['reads']:>8}{b['crosses'] or 0:>9}"
                  f"{(b['max_profit'] or 0) * 100:>8.2f}")
    print()


async def _run_crossevent(
    settings: Settings, duration_sec: float | None
) -> None:
    """Event-driven cross-lifetime recorder over both websockets."""
    from arbengine.crossevent import record_events, summarize

    db = settings.db_path.with_name("crossevents.db")
    key = load_private_key(settings.kalshi_private_key_path)
    dur = duration_sec or 10800.0

    print(f"\nEvent-driven cross recorder -> {db.name}")
    print("Reacting to both venues' websocket pushes at 50ms, recording each")
    print("cross OPEN and CLOSE. Polling could only establish 'shorter than 18s'.\n")

    async with _client(settings, key) as client:
        stats = await record_events(
            client, db, duration_sec=dur, fee_multiplier=settings.fee_multiplier
        )

    print(f"\n{stats['ticks']:,} ticks | {stats['kalshi_updates']:,} kalshi + "
          f"{stats['poly_updates']:,} polymarket updates")
    print(f"{stats['evaluations']:,} evaluations over {stats['games']} games")
    print(f"{stats['events']} complete cross events recorded")

    s = await summarize(db)
    if s["events"]:
        print(f"\nCROSS LIFETIME: mean {s['avg_sec']}s, max {s['max_sec']}s")
        print(f"  {'duration':<14}{'count':>7}{'largest $':>12}")
        for b, n, d in s["buckets"]:
            print(f"  {b:<14}{n:>7}{d or 0:>12.2f}")
    print()


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
        choices=["scan", "stream", "discover", "backtest", "demo",
                 "crossmlb", "crosslive", "crossrec", "crossevent"],
        help=(
            "scan: REST polling loop. stream: event-driven WebSocket scan. "
            "discover: list candidate series. backtest: verify locks end to "
            "end on live geometry. demo: watch paper trading run against "
            "synthetic dislocations. crossmlb: Kalshi vs Polymarket MLB "
            "price monitor. crosslive: same on uncached/live feeds. "
            "crossrec: record a full game to SQLite. crossevent: measure "
            "true cross lifetime from both websockets."
        ),
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="stream only: stop after N seconds",
    )
    parser.add_argument(
        "--interval", type=float, default=30.0,
        help="crossmlb only: seconds between snapshots",
    )
    parser.add_argument(
        "--ui", action="store_true",
        help="stream only: live terminal dashboard",
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
            asyncio.run(_run_stream(settings, args.duration, args.ui))
        elif args.command == "backtest":
            asyncio.run(_run_backtest(settings))
        elif args.command == "demo":
            asyncio.run(_run_demo(settings, args.duration or 60.0))
        elif args.command == "crossmlb":
            asyncio.run(_run_crossmlb(settings, args.duration, args.interval))
        elif args.command == "crosslive":
            asyncio.run(_run_crosslive(settings, args.duration, args.interval))
        elif args.command == "crossrec":
            asyncio.run(_run_crossrec(settings, args.duration, args.interval))
        elif args.command == "crossevent":
            asyncio.run(_run_crossevent(settings, args.duration))
        else:
            asyncio.run(_run_scan(settings, args.once, args.iterations))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
