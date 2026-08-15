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


# ── Zero-depth quotes are not crosses ─────────────────────────────────────────

def _quote(k_away, k_home, p_away, p_home, k_sz, p_sz):
    from arbengine.crossmlb import CrossQuote, GamePair, PmGame

    pm = PmGame(condition_id="0x", question="", slug="mlb-aaa-bbb-2026-08-15",
                away_slug="AAA", home_slug="BBB", start=None,
                outcome_names=["A", "B"], token_ids=["t1", "t2"])
    pair = GamePair(away_slug="AAA", home_slug="BBB", start=None, pm=pm,
                    kalshi_tickers={"AAA": "K-A", "BBB": "K-B"})
    cq = CrossQuote(pair=pair)
    cq.kalshi = {"AAA": k_away, "BBB": k_home}
    cq.poly = {"AAA": p_away, "BBB": p_home}
    cq.kalshi_size = {"AAA": k_sz, "BBB": k_sz}
    cq.poly_size = {"AAA": p_sz, "BBB": p_sz}
    return cq


def test_zero_depth_price_cross_is_not_fillable() -> None:
    """
    The bug: 18 of 37 recorded "crosses" had zero depth. They had the longest
    apparent lifetimes and the most absurd margins (+87%), because a stale
    quote nothing is offered at does not move.
    """
    cq = _quote(0.40, 0.55, 0.42, 0.53, k_sz=0, p_sz=0)
    best = cq.best(0.07)
    assert best is not None, "prices still visible for analysis"
    assert best["profit"] > 0
    assert best["fillable"] is False
    assert cq.best_fillable(0.07) is None, "nothing executable"


def test_fractional_depth_under_one_contract_is_not_fillable() -> None:
    """Kalshi trades whole contracts; 0.75 shares cannot be bought."""
    cq = _quote(0.40, 0.55, 0.42, 0.53, k_sz=0.75, p_sz=0.75)
    assert cq.best(0.07)["fillable"] is False


def test_real_depth_is_fillable() -> None:
    cq = _quote(0.40, 0.55, 0.42, 0.53, k_sz=500, p_sz=500)
    best = cq.best_fillable(0.07)
    assert best is not None
    assert best["fillable"] is True
    assert best["sets"] == 500
    assert best["dollar_profit"] > 0


def test_best_prefers_an_executable_combo_over_a_cheaper_phantom() -> None:
    """
    A zero-depth combination must never outrank a real one just by being
    cheaper on paper — that is how an untradeable quote becomes the headline.
    """
    from arbengine.crossmlb import CrossQuote, GamePair, PmGame

    pm = PmGame(condition_id="0x", question="", slug="mlb-aaa-bbb-2026-08-15",
                away_slug="AAA", home_slug="BBB", start=None,
                outcome_names=["A", "B"], token_ids=["t1", "t2"])
    pair = GamePair(away_slug="AAA", home_slug="BBB", start=None, pm=pm,
                    kalshi_tickers={"AAA": "K-A", "BBB": "K-B"})
    cq = CrossQuote(pair=pair)
    # Polymarket side is dirt cheap but has no depth; Kalshi side is real.
    cq.kalshi = {"AAA": 0.45, "BBB": 0.52}
    cq.poly = {"AAA": 0.10, "BBB": 0.52}
    cq.kalshi_size = {"AAA": 900, "BBB": 900}
    cq.poly_size = {"AAA": 0, "BBB": 900}

    best = cq.best(0.07)
    assert best["fillable"] is True
    assert best["away_venue"] == "kalshi", (
        "must not pick the phantom 0.10 quote with no depth behind it"
    )
