"""
Both INSERT paths in the event recorder, exercised against a real database.

The censored-event insert shipped broken — 16 columns against 15 placeholders —
so every still-open cross was lost at the end of a run. That branch exists
precisely to stop the lifetime distribution being biased downward by discarding
the longest events, so the bug defeated its own purpose while the rest of the
suite stayed green. Nothing had ever run it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from arbengine.crossevent import OpenCross, init_db, summarize

NOW = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)


def _open_cross(**kw) -> OpenCross:
    base = dict(
        game="A @ B", condition_id="0xtest", opened_at=NOW, trigger="kalshi",
        open_state=(0.44, 0.53, 0.45, 0.52), minutes_in=42.0,
    )
    base.update(kw)
    oc = OpenCross(**base)
    oc.observe({"profit": 0.0127, "total": 0.9873, "sets": 100,
                "dollar_profit": 1.27})
    return oc


async def _insert_completed(conn, live: OpenCross, closed_at: datetime):
    await conn.execute(
        """INSERT INTO cross_events (
            game,condition_id,opened_at,closed_at,duration_sec,
            peak_profit,peak_total,peak_sets,peak_dollars,
            open_kalshi_away,open_kalshi_home,open_poly_away,open_poly_home,
            minutes_in,ticks_open,trigger
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (live.game, live.condition_id, live.opened_at.isoformat(),
         closed_at.isoformat(), (closed_at - live.opened_at).total_seconds(),
         live.peak_profit, live.peak_total, live.peak_sets, live.peak_dollars,
         *live.open_state, live.minutes_in, live.ticks, live.trigger),
    )
    await conn.commit()


async def _insert_censored(conn, live: OpenCross, now: datetime):
    """Mirrors the production statement exactly, including placeholder count."""
    await conn.execute(
        """INSERT INTO cross_events (
            game,condition_id,opened_at,closed_at,duration_sec,
            peak_profit,peak_total,peak_sets,peak_dollars,
            open_kalshi_away,open_kalshi_home,open_poly_away,open_poly_home,
            minutes_in,ticks_open,trigger
           ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (live.game, live.condition_id, live.opened_at.isoformat(),
         (now - live.opened_at).total_seconds(), live.peak_profit,
         live.peak_total, live.peak_sets, live.peak_dollars,
         *live.open_state, live.minutes_in, live.ticks,
         live.trigger + "+censored"),
    )
    await conn.commit()


def test_completed_event_round_trips(tmp_path: Path) -> None:
    async def run():
        db = tmp_path / "ev.db"
        conn = await init_db(db)
        await _insert_completed(conn, _open_cross(), NOW + timedelta(seconds=7.92))
        async with conn.execute(
            "SELECT duration_sec, peak_profit, closed_at FROM cross_events"
        ) as cur:
            row = await cur.fetchone()
        await conn.close()
        return row

    dur, peak, closed = asyncio.run(run())
    assert dur == pytest.approx(7.92)
    assert peak == pytest.approx(0.0127)
    assert closed is not None


def test_censored_event_is_written_not_dropped(tmp_path: Path) -> None:
    """
    The bug: this insert had one fewer placeholder than columns, so sqlite
    rejected it and every still-open cross vanished at end of run.
    """
    async def run():
        db = tmp_path / "ev.db"
        conn = await init_db(db)
        await _insert_censored(conn, _open_cross(), NOW + timedelta(seconds=30))
        async with conn.execute(
            "SELECT opened_at, closed_at, duration_sec, trigger FROM cross_events"
        ) as cur:
            row = await cur.fetchone()
        await conn.close()
        return row

    opened, closed, dur, trigger = asyncio.run(run())
    assert opened is not None, "opened_at must carry a value"
    assert closed is None, "closed_at must be NULL — the cross had not ended"
    assert dur == pytest.approx(30.0)
    assert "censored" in trigger


def test_censored_events_keep_the_long_tail(tmp_path: Path) -> None:
    """
    Why the branch exists. Dropping still-open crosses discards precisely the
    longest ones, biasing measured lifetime downward.
    """
    async def run():
        db = tmp_path / "ev.db"
        conn = await init_db(db)
        await _insert_completed(conn, _open_cross(), NOW + timedelta(seconds=2))
        await _insert_censored(conn, _open_cross(), NOW + timedelta(seconds=120))
        await conn.close()
        return await summarize(db)

    s = asyncio.run(run())
    assert s["events"] == 2
    assert s["max_sec"] == pytest.approx(120.0), (
        "the long censored event must survive into the distribution"
    )


def test_observe_tracks_the_peak_not_the_last_value() -> None:
    oc = _open_cross()
    oc.observe({"profit": 0.05, "total": 0.95, "sets": 10, "dollar_profit": 0.5})
    oc.observe({"profit": 0.01, "total": 0.99, "sets": 10, "dollar_profit": 0.1})
    assert oc.peak_profit == pytest.approx(0.05)
    assert oc.ticks == 3
