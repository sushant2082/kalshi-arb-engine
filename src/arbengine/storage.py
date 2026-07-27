import json
import logging
from pathlib import Path

import aiosqlite

from arbengine.models import ArbOpportunity, Contract, PaperPosition, opportunity_key

log = logging.getLogger(__name__)

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id        INTEGER PRIMARY KEY,
    timestamp TEXT    NOT NULL,
    ticker    TEXT    NOT NULL,
    bid       REAL,
    ask       REAL,
    bid_size  INTEGER NOT NULL,
    ask_size  INTEGER NOT NULL
);
"""

_CREATE_SNAPSHOT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_ts
    ON order_book_snapshots (ticker, timestamp);
"""

# `opp_key` is UNIQUE so persistence tracking is an upsert: first_seen is set
# once and last_seen advances while the same violation stays live. Counting each
# poll tick as a new row would make a single 3-second glitch look like a
# recurring opportunity.
_CREATE_OPPORTUNITIES = """
CREATE TABLE IF NOT EXISTS arb_opportunities (
    id                INTEGER PRIMARY KEY,
    opp_key           TEXT    NOT NULL UNIQUE,
    timestamp         TEXT    NOT NULL,
    group_id          TEXT    NOT NULL,
    type              TEXT    NOT NULL,
    legs              TEXT    NOT NULL,
    total_cost        REAL    NOT NULL,
    total_fee         REAL    NOT NULL,
    guaranteed_profit REAL    NOT NULL,
    fillable_sets     INTEGER NOT NULL,
    min_leg_size      INTEGER NOT NULL,
    leg_count         INTEGER NOT NULL,
    first_seen        TEXT    NOT NULL,
    last_seen         TEXT    NOT NULL
);
"""

_CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id                INTEGER PRIMARY KEY,
    opportunity_key   TEXT    NOT NULL,
    group_id          TEXT    NOT NULL,
    type              TEXT    NOT NULL,
    entered_at        TEXT    NOT NULL,
    fills             TEXT    NOT NULL,
    fill_status       TEXT    NOT NULL,
    sets_attempted    INTEGER NOT NULL,
    sets_filled       INTEGER NOT NULL,
    net_cash          REAL    NOT NULL,
    total_fee         REAL    NOT NULL,
    expected_profit   REAL    NOT NULL,
    bankroll_at_entry REAL    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'open',
    realized_payout   REAL,
    pnl               REAL,
    settled_at        TEXT,
    settlement_state  TEXT
);
"""


async def init_db(db_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    await conn.execute(_CREATE_SNAPSHOTS)
    await conn.execute(_CREATE_SNAPSHOT_INDEX)
    await conn.execute(_CREATE_OPPORTUNITIES)
    await conn.execute(_CREATE_POSITIONS)
    await conn.commit()
    log.info("Database ready at %s", db_path)
    return conn


# ── Order books ───────────────────────────────────────────────────────────────

async def save_snapshots(
    conn: aiosqlite.Connection, contracts: list[Contract]
) -> None:
    await conn.executemany(
        """
        INSERT INTO order_book_snapshots (timestamp, ticker, bid, ask, bid_size, ask_size)
        VALUES (?,?,?,?,?,?)
        """,
        [
            (
                c.fetched_at.isoformat(), c.ticker, c.bid, c.ask,
                c.bid_size, c.ask_size,
            )
            for c in contracts
        ],
    )
    await conn.commit()


# ── Opportunities ─────────────────────────────────────────────────────────────

def _legs_json(opp: ArbOpportunity) -> str:
    return json.dumps([
        {
            "ticker": leg.ticker, "side": leg.side, "qty": leg.qty,
            "price": leg.price, "fee": leg.fee,
        }
        for leg in opp.legs
    ])


async def upsert_opportunity(
    conn: aiosqlite.Connection, opp: ArbOpportunity
) -> None:
    """
    Insert a newly seen opportunity, or extend `last_seen` on one already live.

    ON CONFLICT keeps the original `first_seen` and only advances `last_seen`,
    so `last_seen - first_seen` is the true persistence duration — the number
    that actually answers whether an opportunity was capturable at REST latency.
    """
    await conn.execute(
        """
        INSERT INTO arb_opportunities (
            opp_key, timestamp, group_id, type, legs, total_cost, total_fee,
            guaranteed_profit, fillable_sets, min_leg_size, leg_count,
            first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(opp_key) DO UPDATE SET
            last_seen         = excluded.last_seen,
            guaranteed_profit = excluded.guaranteed_profit,
            fillable_sets     = excluded.fillable_sets,
            min_leg_size      = excluded.min_leg_size,
            total_cost        = excluded.total_cost,
            total_fee         = excluded.total_fee,
            legs              = excluded.legs
        """,
        (
            opportunity_key(opp), opp.last_seen.isoformat(), opp.group_id,
            opp.type, _legs_json(opp), opp.total_cost, opp.total_fee,
            opp.guaranteed_profit, opp.fillable_sets, opp.min_leg_size,
            opp.leg_count, opp.first_seen.isoformat(), opp.last_seen.isoformat(),
        ),
    )
    await conn.commit()


async def get_opportunities(
    conn: aiosqlite.Connection, limit: int = 200
) -> list[dict]:
    async with conn.execute(
        """
        SELECT id, opp_key, group_id, type, legs, total_cost, total_fee,
               guaranteed_profit, fillable_sets, min_leg_size, leg_count,
               first_seen, last_seen
        FROM arb_opportunities ORDER BY last_seen DESC LIMIT ?
        """,
        (limit,),
    ) as cur:
        cols = [d[0] for d in cur.description]
        rows = await cur.fetchall()
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["legs"] = json.loads(d["legs"])
        out.append(d)
    return out


# ── Paper positions ───────────────────────────────────────────────────────────

async def save_position(
    conn: aiosqlite.Connection, pos: PaperPosition
) -> int:
    fills = json.dumps([f.model_dump() for f in pos.fills])
    async with conn.execute(
        """
        INSERT INTO paper_positions (
            opportunity_key, group_id, type, entered_at, fills, fill_status,
            sets_attempted, sets_filled, net_cash, total_fee, expected_profit,
            bankroll_at_entry, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pos.opportunity_key, pos.group_id, pos.type,
            pos.entered_at.isoformat(), fills, pos.fill_status,
            pos.sets_attempted, pos.sets_filled, pos.net_cash, pos.total_fee,
            pos.expected_profit, pos.bankroll_at_entry, pos.status,
        ),
    ) as cur:
        row_id = cur.lastrowid
    await conn.commit()
    return row_id


async def settle_position(
    conn: aiosqlite.Connection, pos: PaperPosition
) -> None:
    await conn.execute(
        """
        UPDATE paper_positions
        SET status='settled', realized_payout=?, pnl=?, settled_at=?, settlement_state=?
        WHERE id=?
        """,
        (
            pos.realized_payout, pos.pnl,
            pos.settled_at.isoformat() if pos.settled_at else None,
            pos.settlement_state, pos.id,
        ),
    )
    await conn.commit()


async def get_positions(
    conn: aiosqlite.Connection, status: str | None = None, limit: int = 500
) -> list[dict]:
    sql = """
        SELECT id, opportunity_key, group_id, type, entered_at, fills,
               fill_status, sets_attempted, sets_filled, net_cash, total_fee,
               expected_profit, bankroll_at_entry, status, realized_payout,
               pnl, settled_at, settlement_state
        FROM paper_positions
    """
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY entered_at DESC LIMIT ?"
    params = params + (limit,)

    async with conn.execute(sql, params) as cur:
        cols = [d[0] for d in cur.description]
        rows = await cur.fetchall()
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["fills"] = json.loads(d["fills"])
        out.append(d)
    return out


# ── Analysis ──────────────────────────────────────────────────────────────────

async def persistence_stats(conn: aiosqlite.Connection) -> list[dict]:
    """
    Per-type persistence distribution: how long violations of each kind lasted.
    This is the real output of the whole tool — it answers whether an
    opportunity was ever capturable before any capital is risked.
    """
    async with conn.execute(
        """
        SELECT type,
               COUNT(*) AS n,
               AVG((julianday(last_seen) - julianday(first_seen)) * 86400.0) AS mean_sec,
               MAX((julianday(last_seen) - julianday(first_seen)) * 86400.0) AS max_sec,
               AVG(guaranteed_profit) AS mean_profit,
               MAX(guaranteed_profit) AS max_profit
        FROM arb_opportunities GROUP BY type ORDER BY n DESC
        """
    ) as cur:
        cols = [d[0] for d in cur.description]
        rows = await cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]
