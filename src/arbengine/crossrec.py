"""
Full-game cross-venue recorder.

The short live windows answered "do real crosses exist" (yes, at single basis
points) but not "do they get materially larger during high-volatility moments".
That question needs a whole game, and it needs every read persisted rather than
printed, because the interesting analysis is after the fact:

  - how cross size is distributed over a game
  - whether crosses cluster around scoring plays and late innings
  - how long each one survives, which decides whether it is capturable
  - how much depth sits behind them

So this records every read of both venues to SQLite, including the reads where
nothing crossed. Only logging the crosses would make it impossible to tell a
genuinely rare event from a scanner that stopped working.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from arbengine.crosspaper import (
    CrossPaperTrader, init_db as paper_db, report, save_position,
    settle_open_positions,
)
from arbengine.crossmon import collect_pairs, live_snapshot
from arbengine.source.polymarket import PolymarketClient, token_ids
from arbengine.source.polystream import PolymarketStream

log = logging.getLogger(__name__)

_CREATE_READS = """
CREATE TABLE IF NOT EXISTS cross_reads (
    id            INTEGER PRIMARY KEY,
    read_at       TEXT    NOT NULL,
    game          TEXT    NOT NULL,
    condition_id  TEXT    NOT NULL,
    start_utc     TEXT,
    minutes_in    REAL,
    kalshi_away   REAL,
    kalshi_home   REAL,
    kalshi_away_size INTEGER,
    kalshi_home_size INTEGER,
    poly_away     REAL,
    poly_home     REAL,
    poly_away_size   INTEGER,
    poly_home_size   INTEGER,
    best_total    REAL,
    best_profit   REAL,
    best_route    TEXT,
    best_sets     INTEGER,
    best_dollars  REAL,
    is_cross      INTEGER NOT NULL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cross_reads_game_time
    ON cross_reads (condition_id, read_at);
"""


async def init_db(path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute(_CREATE_READS)
    await conn.execute(_CREATE_INDEX)
    await conn.commit()
    return conn


async def record_read(conn: aiosqlite.Connection, cq, fee_multiplier: float) -> bool:
    """Persist one game's read. Returns whether it crossed."""
    p = cq.pair
    best = cq.best(fee_multiplier)
    now = cq.at or datetime.now(timezone.utc)
    minutes_in = (
        (now - p.start).total_seconds() / 60.0 if p.start else None
    )
    is_cross = bool(best and best["profit"] > 0)

    await conn.execute(
        """
        INSERT INTO cross_reads (
            read_at, game, condition_id, start_utc, minutes_in,
            kalshi_away, kalshi_home, kalshi_away_size, kalshi_home_size,
            poly_away, poly_home, poly_away_size, poly_home_size,
            best_total, best_profit, best_route, best_sets, best_dollars,
            is_cross
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now.isoformat(), p.label, p.pm.condition_id,
            p.start.isoformat() if p.start else None, minutes_in,
            cq.kalshi.get(p.away_slug), cq.kalshi.get(p.home_slug),
            cq.kalshi_size.get(p.away_slug), cq.kalshi_size.get(p.home_slug),
            cq.poly.get(p.away_slug), cq.poly.get(p.home_slug),
            int(cq.poly_size.get(p.away_slug, 0)),
            int(cq.poly_size.get(p.home_slug, 0)),
            best["total"] if best else None,
            best["profit"] if best else None,
            f"{best['away_venue']}/{best['home_venue']}" if best else None,
            best["sets"] if best else None,
            best["dollar_profit"] if best else None,
            int(is_cross),
        ),
    )
    return is_cross


async def record_games(
    kalshi_client,
    db_path: Path,
    duration_sec: float,
    interval_sec: float = 5.0,
    fee_multiplier: float = 0.07,
    include_pregame_minutes: float = 30.0,
    use_stream: bool = True,
    max_stream_staleness_sec: float = 120.0,
    trader: "CrossPaperTrader | None" = None,
    paper_db_path: Path | None = None,
) -> dict:
    """
    Record every read for games in progress (and those starting shortly).

    Re-discovers the slate periodically so games that start mid-run are picked
    up — a three-hour window will usually span several first pitches.
    """
    conn = await init_db(db_path)
    stats = {
        "reads": 0, "crosses": 0, "games": set(), "rediscoveries": 0,
        "stream_reads": 0, "rest_fallback_reads": 0,
        "positions": 0, "settled": 0,
    }
    pconn = await paper_db(paper_db_path) if (trader and paper_db_path) else None
    # One position per game at a time: re-entering the same cross on every
    # read would book the same opportunity dozens of times over.
    open_games: set[str] = set()

    stream: PolymarketStream | None = None
    try:
        async with PolymarketClient() as pm:
            loop = asyncio.get_event_loop()
            started = loop.time()
            # Wall clock, not the monotonic loop clock. On macOS the monotonic
            # clock does not advance while the system sleeps, so a "run for 11
            # hours" budget measured that way only consumes waking time — an
            # overnight sleep stretched one run to 23h41m of wall time. For a
            # window meant to cover a day's game slate, wall clock is the
            # intent.
            wall_deadline = datetime.now(timezone.utc) + timedelta(
                seconds=duration_sec
            )
            pairs: list = []
            last_discovery = -1e9

            while datetime.now(timezone.utc) < wall_deadline:
                # Refresh the slate every 10 minutes.
                if loop.time() - last_discovery > 600:
                    all_pairs, _, _ = await collect_pairs(kalshi_client, pm)
                    now = datetime.now(timezone.utc)
                    pairs = [
                        p for p in all_pairs
                        if p.start is not None
                        and (now - p.start).total_seconds()
                        > -include_pregame_minutes * 60
                    ]
                    last_discovery = loop.time()
                    stats["rediscoveries"] += 1
                    if trader and pconn:
                        n = await settle_open_positions(
                            pconn, kalshi_client, trader
                        )
                        stats["settled"] += n
                    log.info("Tracking %d games", len(pairs))

                    # (Re)subscribe the stream to the current slate. Games are
                    # added as they start, so the token set changes over a
                    # multi-hour run.
                    if use_stream and pairs:
                        wanted = [
                            tid for p in pairs
                            for tid in (
                                p.pm.token_for_slug(p.away_slug),
                                p.pm.token_for_slug(p.home_slug),
                            ) if tid
                        ]
                        if stream is None or set(wanted) - set(stream.asset_ids):
                            if stream is not None:
                                await stream.stop()
                            stream = PolymarketStream(wanted)
                            stream.start()
                            ready = await stream.wait_ready(20)
                            log.info(
                                "Polymarket stream %s on %d tokens",
                                "ready" if ready else "NOT ready (REST fallback)",
                                len(wanted),
                            )

                # Rate-limit reality check: each game costs 2 uncached Kalshi
                # orderbook requests, and the measured basic-tier ceiling is
                # ~3.6 req/s. A 15-game slate therefore needs >8s per read no
                # matter what interval is requested. The token bucket enforces
                # this correctly, but the requested interval becomes a floor
                # rather than the actual cadence, so it is logged once.
                if pairs and stats["rediscoveries"] == 1 and stats["reads"] == 0:
                    min_read = (len(pairs) * 2) / 3.6
                    if min_read > interval_sec:
                        log.info(
                            "%d games needs ~%.0fs per read; requested interval "
                            "%.0fs will stretch to that",
                            len(pairs), min_read, interval_sec,
                        )

                if pairs:
                    # Prefer the websocket, fall back to REST when it has
                    # nothing fresh. An unattended multi-hour run cannot afford
                    # a silently dead feed: the stream connects cleanly even
                    # when a subscription is wrong, so "no messages" must never
                    # be mistaken for "no price movement". Staleness is checked
                    # against the wall clock rather than trusting `connected`.
                    source = pm
                    if stream is not None:
                        fresh = stream.quotes()
                        ages = stream.staleness()
                        newest = min(ages.values()) if ages else 1e9
                        if fresh and newest <= max_stream_staleness_sec:
                            source = stream
                            stats["stream_reads"] += 1
                        else:
                            stats["rest_fallback_reads"] += 1
                            if stats["rest_fallback_reads"] % 20 == 1:
                                log.warning(
                                    "Polymarket stream stale (%.0fs) — using REST",
                                    newest,
                                )

                    quotes = await live_snapshot(
                        kalshi_client, source, pairs, fee_multiplier
                    )
                    crossed = 0
                    for cq in quotes:
                        is_cross = await record_read(conn, cq, fee_multiplier)
                        if is_cross:
                            crossed += 1
                        stats["games"].add(cq.pair.label)

                        if trader and pconn and is_cross:
                            key = cq.pair.pm.condition_id
                            if key not in open_games:
                                best = cq.best(fee_multiplier)
                                pos = trader.open_position(
                                    cq.pair, best, datetime.now(timezone.utc)
                                )
                                if pos:
                                    await save_position(pconn, pos)
                                    open_games.add(key)
                                    stats["positions"] += 1
                                    log.info(
                                        "PAPER %s %s: %d sets @ %.4f, "
                                        "expect $%.2f",
                                        pos.fill_status.upper(),
                                        pos.game[:26], pos.sets_wanted,
                                        best["total"], pos.expected_profit,
                                    )
                    await conn.commit()
                    stats["reads"] += len(quotes)
                    stats["crosses"] += crossed

                    if crossed:
                        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        log.info("%s  %d/%d games crossing", stamp, crossed, len(quotes))

                remaining = (
                    wall_deadline - datetime.now(timezone.utc)
                ).total_seconds()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(interval_sec, remaining))
    finally:
        if trader and pconn:
            # Final settlement sweep: games that finished during the run.
            try:
                stats["settled"] += await settle_open_positions(
                    pconn, kalshi_client, trader
                )
            except Exception as exc:
                log.warning("Final settlement sweep failed: %s", exc)
            await pconn.close()
        if stream is not None:
            await stream.stop()
        await conn.close()

    stats["games"] = sorted(stats["games"])
    return stats


# ── Analysis ──────────────────────────────────────────────────────────────────

async def summarize(db_path: Path) -> dict:
    """Post-hoc read of a recorded session."""
    conn = await aiosqlite.connect(db_path)
    try:
        async with conn.execute(
            "SELECT COUNT(*), SUM(is_cross), COUNT(DISTINCT condition_id) FROM cross_reads"
        ) as cur:
            reads, crosses, games = await cur.fetchone()

        async with conn.execute(
            """
            SELECT game,
                   COUNT(*)                              AS reads,
                   SUM(is_cross)                         AS crosses,
                   MAX(best_profit)                      AS max_profit,
                   MAX(CASE WHEN is_cross THEN best_dollars END) AS max_dollars,
                   AVG(best_total)                       AS mean_total
            FROM cross_reads GROUP BY game ORDER BY crosses DESC
            """
        ) as cur:
            per_game = [
                dict(zip([d[0] for d in cur.description], row))
                for row in await cur.fetchall()
            ]

        # Does cross size grow later in a game?
        async with conn.execute(
            """
            SELECT CAST(minutes_in / 30 AS INTEGER) * 30 AS bucket,
                   COUNT(*) AS reads,
                   SUM(is_cross) AS crosses,
                   MAX(best_profit) AS max_profit
            FROM cross_reads WHERE minutes_in IS NOT NULL
            GROUP BY bucket ORDER BY bucket
            """
        ) as cur:
            by_phase = [
                dict(zip([d[0] for d in cur.description], row))
                for row in await cur.fetchall()
            ]

        return {
            "reads": reads or 0,
            "crosses": crosses or 0,
            "games": games or 0,
            "per_game": per_game,
            "by_phase": by_phase,
        }
    finally:
        await conn.close()
