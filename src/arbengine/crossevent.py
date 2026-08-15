"""
Event-driven cross-lifetime recorder.

The polling recorder answered how OFTEN the venues cross (1.4% of reads, rising
~40x from pregame to late innings) but not how LONG a cross survives — 78% of
them appeared in a single 18-second read, which only establishes "shorter than
18 seconds". That number decides everything: at two seconds nothing retail can
act, at thirty a two-leg bot has a real chance.

So this reacts to pushes from both venues' websockets instead of sampling on a
timer, and records the OPEN and CLOSE of each cross rather than periodic
snapshots. Duration comes out directly.

Design notes:

  - Both feeds maintain their own book state in memory. Re-evaluating a game is
    pure arithmetic over that state, no I/O, so the evaluation loop can run at
    50ms without touching either venue.
  - Kalshi pushes onto a queue; Polymarket keeps a dict. Draining one and
    reading the other on the same tick keeps the two sides aligned to within
    one loop interval.
  - Transitions are recorded, not samples. A cross that opens and closes
    between two ticks would be invisible, so the tick has to be far shorter
    than the shortest event of interest — hence 50ms against events suspected
    to last seconds.
  - Heartbeats are written even when nothing crosses. Without them a quiet feed
    and a dead feed produce identical files, which is the failure this whole
    project keeps rediscovering.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from arbengine.crossmlb import CrossQuote
from arbengine.crossmon import collect_pairs
from arbengine.source.kalshi import parse_book
from arbengine.source.polymarket import PolymarketClient
from arbengine.source.polystream import PolymarketStream

log = logging.getLogger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS cross_events (
    id            INTEGER PRIMARY KEY,
    game          TEXT    NOT NULL,
    condition_id  TEXT    NOT NULL,
    opened_at     TEXT    NOT NULL,
    closed_at     TEXT,
    duration_sec  REAL,
    -- Best state seen while the cross was open.
    peak_profit   REAL,
    peak_total    REAL,
    peak_sets     INTEGER,
    peak_dollars  REAL,
    -- State at the moment it opened, for reconstructing what caused it.
    open_kalshi_away  REAL,
    open_kalshi_home  REAL,
    open_poly_away    REAL,
    open_poly_home    REAL,
    minutes_in    REAL,
    ticks_open    INTEGER,
    -- Which venue's update opened the cross.
    trigger       TEXT
);
"""

_CREATE_HB = """
CREATE TABLE IF NOT EXISTS heartbeats (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    games          INTEGER,
    kalshi_updates INTEGER,
    poly_updates   INTEGER,
    evaluations    INTEGER,
    open_crosses   INTEGER
);
"""


@dataclass
class OpenCross:
    """A cross currently in progress."""

    game: str
    condition_id: str
    opened_at: datetime
    trigger: str
    open_state: tuple
    minutes_in: float | None
    peak_profit: float = 0.0
    peak_total: float = 1.0
    peak_sets: int = 0
    peak_dollars: float = 0.0
    ticks: int = 0

    def observe(self, best: dict) -> None:
        self.ticks += 1
        if best["profit"] > self.peak_profit:
            self.peak_profit = best["profit"]
            self.peak_total = best["total"]
            self.peak_sets = best["sets"]
            self.peak_dollars = best["dollar_profit"]


async def init_db(path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute(_CREATE)
    await conn.execute(_CREATE_HB)
    await conn.commit()
    return conn


async def record_events(
    kalshi_client,
    db_path: Path,
    duration_sec: float,
    fee_multiplier: float = 0.07,
    tick_sec: float = 0.05,
    heartbeat_sec: float = 30.0,
    include_pregame_minutes: float = 30.0,
) -> dict:
    """
    Watch both websockets and record every cross open/close with timestamps.

    Returns summary stats. The database is the real output.
    """
    from datetime import timedelta

    conn = await init_db(db_path)
    stats = {
        "kalshi_updates": 0, "poly_updates": 0, "evaluations": 0,
        "events": 0, "games": 0, "ticks": 0,
    }
    open_crosses: dict[str, OpenCross] = {}

    async with PolymarketClient() as pm:
        pairs, _, _ = await collect_pairs(kalshi_client, pm)
        now = datetime.now(timezone.utc)
        pairs = [
            p for p in pairs
            if p.start is not None
            and (now - p.start).total_seconds() > -include_pregame_minutes * 60
        ]
        stats["games"] = len(pairs)
        if not pairs:
            log.warning("No games in window")
            await conn.close()
            return stats

        # Kalshi: one ticker per team per game.
        tickers = [t for p in pairs for t in p.kalshi_tickers.values()]
        tokens = [
            tid for p in pairs
            for tid in (
                p.pm.token_for_slug(p.away_slug),
                p.pm.token_for_slug(p.home_slug),
            ) if tid
        ]

        log.info(
            "Event-driven: %d games, %d kalshi tickers, %d poly tokens",
            len(pairs), len(tickers), len(tokens),
        )

        kq: asyncio.Queue = asyncio.Queue()
        kalshi_task = asyncio.create_task(kalshi_client.stream_books(tickers, kq))
        stream = PolymarketStream(tokens)
        stream.start()
        await stream.wait_ready(25)

        # Local mirror of both venues, keyed the way the detector wants it.
        kalshi_books: dict[str, dict] = {}
        deadline = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
        last_hb = datetime.now(timezone.utc)
        last_poly_msgs = 0

        try:
            while datetime.now(timezone.utc) < deadline:
                stats["ticks"] += 1
                trigger = None

                # Drain whatever Kalshi pushed since the last tick.
                drained = 0
                while not kq.empty():
                    try:
                        ticker, book, _ts = kq.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    kalshi_books[ticker] = book
                    drained += 1
                if drained:
                    stats["kalshi_updates"] += drained
                    trigger = "kalshi"

                poly_books = stream.quotes()
                if stream.messages != last_poly_msgs:
                    stats["poly_updates"] += stream.messages - last_poly_msgs
                    last_poly_msgs = stream.messages
                    trigger = "polymarket" if trigger is None else "both"

                if trigger is None:
                    await asyncio.sleep(tick_sec)
                    continue

                now = datetime.now(timezone.utc)
                for p in pairs:
                    cq = CrossQuote(pair=p, at=now)
                    for slug in (p.away_slug, p.home_slug):
                        tk = p.kalshi_tickers.get(slug)
                        kb = kalshi_books.get(tk) if tk else None
                        if kb:
                            cq.kalshi[slug] = kb["ask"]
                            cq.kalshi_size[slug] = kb["ask_size"]
                        tid = p.pm.token_for_slug(slug)
                        pb = poly_books.get(tid) if tid else None
                        if pb:
                            cq.poly[slug] = pb["ask"]
                            cq.poly_size[slug] = pb["ask_size"]

                    stats["evaluations"] += 1
                    best = cq.best(fee_multiplier)
                    key = p.pm.condition_id
                    crossing = bool(best and best["profit"] > 0)
                    live = open_crosses.get(key)

                    if crossing and live is None:
                        mins = (
                            (now - p.start).total_seconds() / 60
                            if p.start else None
                        )
                        oc = OpenCross(
                            game=p.label, condition_id=key, opened_at=now,
                            trigger=trigger or "?",
                            open_state=(
                                cq.kalshi.get(p.away_slug),
                                cq.kalshi.get(p.home_slug),
                                cq.poly.get(p.away_slug),
                                cq.poly.get(p.home_slug),
                            ),
                            minutes_in=mins,
                        )
                        oc.observe(best)
                        open_crosses[key] = oc

                    elif crossing and live is not None:
                        live.observe(best)

                    elif not crossing and live is not None:
                        dur = (now - live.opened_at).total_seconds()
                        await conn.execute(
                            """INSERT INTO cross_events (
                                game,condition_id,opened_at,closed_at,duration_sec,
                                peak_profit,peak_total,peak_sets,peak_dollars,
                                open_kalshi_away,open_kalshi_home,
                                open_poly_away,open_poly_home,
                                minutes_in,ticks_open,trigger
                               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (live.game, key, live.opened_at.isoformat(),
                             now.isoformat(), dur, live.peak_profit,
                             live.peak_total, live.peak_sets, live.peak_dollars,
                             *live.open_state, live.minutes_in, live.ticks,
                             live.trigger),
                        )
                        await conn.commit()
                        stats["events"] += 1
                        log.info(
                            "CROSS %s  %.2fs  peak +%.2f%% ($%.2f) after %d ticks",
                            live.game[:28], dur, live.peak_profit * 100,
                            live.peak_dollars, live.ticks,
                        )
                        del open_crosses[key]

                if (now - last_hb).total_seconds() >= heartbeat_sec:
                    await conn.execute(
                        """INSERT INTO heartbeats
                           (at,games,kalshi_updates,poly_updates,evaluations,open_crosses)
                           VALUES (?,?,?,?,?,?)""",
                        (now.isoformat(), len(pairs), stats["kalshi_updates"],
                         stats["poly_updates"], stats["evaluations"],
                         len(open_crosses)),
                    )
                    await conn.commit()
                    last_hb = now

                await asyncio.sleep(tick_sec)

            # Anything still open when time runs out is recorded as censored:
            # its true duration is at least this long. Dropping them would bias
            # the measured lifetime downward by discarding the longest events.
            now = datetime.now(timezone.utc)
            for key, live in open_crosses.items():
                # opened_at carries a value; closed_at is NULL because the
                # cross had not ended when the run did. Getting these the wrong
                # way round is what broke this insert: 16 columns against 15
                # placeholders, so the whole censored batch was lost — exactly
                # the events this branch exists to preserve.
                await conn.execute(
                    """INSERT INTO cross_events (
                        game,condition_id,opened_at,closed_at,duration_sec,
                        peak_profit,peak_total,peak_sets,peak_dollars,
                        open_kalshi_away,open_kalshi_home,open_poly_away,
                        open_poly_home,minutes_in,ticks_open,trigger
                       ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (live.game, key, live.opened_at.isoformat(),
                     (now - live.opened_at).total_seconds(), live.peak_profit,
                     live.peak_total, live.peak_sets, live.peak_dollars,
                     *live.open_state, live.minutes_in, live.ticks,
                     live.trigger + "+censored"),
                )
            await conn.commit()
        finally:
            kalshi_task.cancel()
            await stream.stop()
            await conn.close()

    return stats


async def summarize(db_path: Path) -> dict:
    """Lifetime distribution — the number this whole module exists to produce."""
    conn = await aiosqlite.connect(db_path)
    try:
        async with conn.execute(
            "SELECT COUNT(*), ROUND(AVG(duration_sec),2), ROUND(MAX(duration_sec),2) "
            "FROM cross_events"
        ) as cur:
            n, avg, mx = await cur.fetchone()

        async with conn.execute(
            """SELECT CASE
                 WHEN duration_sec < 0.5 THEN 'under 0.5s'
                 WHEN duration_sec < 1   THEN '0.5-1s'
                 WHEN duration_sec < 5   THEN '1-5s'
                 WHEN duration_sec < 15  THEN '5-15s'
                 WHEN duration_sec < 60  THEN '15-60s'
                 ELSE 'over 60s' END AS bucket,
               COUNT(*), ROUND(MAX(peak_dollars),2)
               FROM cross_events GROUP BY bucket"""
        ) as cur:
            buckets = await cur.fetchall()

        return {"events": n or 0, "avg_sec": avg, "max_sec": mx,
                "buckets": buckets}
    finally:
        await conn.close()
