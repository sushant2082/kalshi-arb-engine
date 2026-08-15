"""
Cross-venue paper trading with real settlement.

The recorders answer "did the venues cross". This answers "would it have made
money", which is a different and harder question — a cross is a quoted price,
while profit depends on filling both legs and on how each venue actually
settles the game.

SIMULATION ONLY. No order is ever placed on either venue. Positions are opened
against quoted depth and settled against the real game result read back from
Kalshi's `result` field once the market closes.

Three things separate this from just summing the detected edges:

  1. FILL REALISM. Neither venue offers atomic multi-leg fill, so the two legs
     are modelled as filling independently. When only one fills, the position
     is not a hedge — it is an outright bet on one team, and it is settled that
     way rather than discarded.
  2. REAL OUTCOMES. Settlement uses the actual winner, not the assumption that
     a hedge pays $1. If the legs were mismatched or one failed to fill, the
     real result is what decides the P&L.
  3. SEPARATE ACCOUNTING. Fully-hedged and broken positions are reported apart.
     Blending them hides the only number that decides whether this is tradeable
     for real money.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from arbengine.fees import order_fee

log = logging.getLogger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id             INTEGER PRIMARY KEY,
    opened_at      TEXT NOT NULL,
    game           TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    -- Which venue supplied which leg.
    away_venue     TEXT NOT NULL,
    home_venue     TEXT NOT NULL,
    away_team      TEXT,
    home_team      TEXT,
    away_price     REAL NOT NULL,
    home_price     REAL NOT NULL,
    sets_wanted    INTEGER NOT NULL,
    -- What actually filled, per leg.
    away_filled    INTEGER NOT NULL,
    home_filled    INTEGER NOT NULL,
    fill_status    TEXT NOT NULL,      -- hedged | broken
    cost           REAL NOT NULL,       -- cash out, fees included
    fees           REAL NOT NULL,
    expected_profit REAL NOT NULL,      -- what the cross implied
    -- Settlement.
    status         TEXT NOT NULL DEFAULT 'open',
    winner         TEXT,
    payout         REAL,
    pnl            REAL,
    settled_at     TEXT,
    kalshi_away_ticker TEXT,
    kalshi_home_ticker TEXT
);
"""


@dataclass
class PaperPosition:
    game: str
    condition_id: str
    away_venue: str
    home_venue: str
    away_team: str
    home_team: str
    away_price: float
    home_price: float
    sets_wanted: int
    away_filled: int
    home_filled: int
    cost: float
    fees: float
    expected_profit: float
    opened_at: datetime
    kalshi_away_ticker: str = ""
    kalshi_home_ticker: str = ""
    id: int | None = None

    @property
    def fill_status(self) -> str:
        """
        `hedged` only when both legs filled the same number of sets.

        Anything else is an outright directional position on whichever team
        actually filled, and calling it a hedge would be the single most
        expensive lie this system could tell itself.
        """
        if self.away_filled == self.home_filled == self.sets_wanted:
            return "hedged"
        if self.away_filled == self.home_filled and self.away_filled > 0:
            return "hedged"
        return "broken"


class CrossPaperTrader:
    """
    Simulates taking cross-venue positions and settles them on real results.

    `leg_fill_prob` is the probability an individual leg fills at its quoted
    price. 1.0 is the optimistic bound and WILL overstate performance, because
    the two legs are placed sequentially against two separate venues with no
    atomic fill. Lower it to see how much of the edge survives leg risk.
    """

    def __init__(
        self,
        bankroll: float = 10_000.0,
        max_sets: int = 200,
        leg_fill_prob: float = 1.0,
        fee_multiplier: float = 0.07,
        rng: random.Random | None = None,
    ) -> None:
        self.starting_bankroll = bankroll
        self.bankroll = bankroll
        self.max_sets = max_sets
        self.leg_fill_prob = leg_fill_prob
        self.fee_multiplier = fee_multiplier
        self._rng = rng or random.Random()

    def open_position(
        self, pair, best: dict, now: datetime
    ) -> PaperPosition | None:
        """Simulate taking a detected cross."""
        sets = min(best["sets"], self.max_sets)
        if sets <= 0:
            return None

        away_filled = sets if self._rng.random() <= self.leg_fill_prob else 0
        home_filled = sets if self._rng.random() <= self.leg_fill_prob else 0
        if away_filled == 0 and home_filled == 0:
            return None

        fees = 0.0
        if best["away_venue"] == "kalshi" and away_filled:
            fees += order_fee(best["away_price"], away_filled, self.fee_multiplier)
        if best["home_venue"] == "kalshi" and home_filled:
            fees += order_fee(best["home_price"], home_filled, self.fee_multiplier)

        cost = (
            best["away_price"] * away_filled
            + best["home_price"] * home_filled
            + fees
        )
        if cost > self.bankroll:
            log.debug("Skipping %s: needs $%.2f, have $%.2f",
                      pair.label, cost, self.bankroll)
            return None

        self.bankroll -= cost
        pos = PaperPosition(
            game=pair.label,
            condition_id=pair.pm.condition_id,
            away_venue=best["away_venue"],
            home_venue=best["home_venue"],
            away_team=pair.away_slug,
            home_team=pair.home_slug,
            away_price=best["away_price"],
            home_price=best["home_price"],
            sets_wanted=sets,
            away_filled=away_filled,
            home_filled=home_filled,
            cost=cost,
            fees=fees,
            expected_profit=best["profit"] * sets,
            opened_at=now,
            kalshi_away_ticker=pair.kalshi_tickers.get(pair.away_slug, ""),
            kalshi_home_ticker=pair.kalshi_tickers.get(pair.home_slug, ""),
        )
        if pos.fill_status == "broken":
            log.warning(
                "BROKEN FILL on %s: away %d/%d, home %d/%d — this is an "
                "outright bet, not a hedge",
                pair.label, away_filled, sets, home_filled, sets,
            )
        return pos

    def settle(
        self, pos: PaperPosition, winner_slug: str, now: datetime
    ) -> PaperPosition:
        """
        Settle against the real winner.

        Each contract on the winning team pays $1; the losing side pays nothing.
        A hedged position collects on exactly one leg. A broken one collects
        only if the team it actually holds happens to win.
        """
        payout = 0.0
        if winner_slug == pos.away_team:
            payout = float(pos.away_filled)
        elif winner_slug == pos.home_team:
            payout = float(pos.home_filled)

        pos.payout = payout
        pos.pnl = payout - pos.cost
        pos.status = "settled"
        self.bankroll += payout
        return pos


async def init_db(path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute(_CREATE)
    await conn.commit()
    return conn


async def save_position(conn: aiosqlite.Connection, pos: PaperPosition) -> int:
    async with conn.execute(
        """INSERT INTO paper_positions (
            opened_at,game,condition_id,away_venue,home_venue,away_team,home_team,
            away_price,home_price,sets_wanted,away_filled,home_filled,fill_status,
            cost,fees,expected_profit,kalshi_away_ticker,kalshi_home_ticker
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pos.opened_at.isoformat(), pos.game, pos.condition_id, pos.away_venue,
         pos.home_venue, pos.away_team, pos.home_team, pos.away_price,
         pos.home_price, pos.sets_wanted, pos.away_filled, pos.home_filled,
         pos.fill_status, pos.cost, pos.fees, pos.expected_profit,
         pos.kalshi_away_ticker, pos.kalshi_home_ticker),
    ) as cur:
        pos.id = cur.lastrowid
    await conn.commit()
    return pos.id


async def settle_open_positions(
    conn: aiosqlite.Connection, kalshi_client, trader: CrossPaperTrader
) -> int:
    """
    Settle any open position whose Kalshi market has resolved.

    The winner is read from Kalshi's `result` field rather than inferred from
    prices — a market trading at 0.99 has not necessarily settled, and
    assuming it has would book profits that never happened.
    """
    rows = await (await conn.execute(
        """SELECT id,game,away_team,home_team,away_filled,home_filled,cost,
                  kalshi_away_ticker,kalshi_home_ticker
           FROM paper_positions WHERE status='open'"""
    )).fetchall()

    settled = 0
    for (pid, game, away, home, af, hf, cost, atk, htk) in rows:
        winner = None
        for ticker, slug in ((atk, away), (htk, home)):
            if not ticker:
                continue
            try:
                market = await kalshi_client.get_market(ticker)
            except Exception:
                continue
            result = (market.get("result") or "").strip().lower()
            if result == "yes":
                winner = slug
                break
            if result == "no":
                winner = home if slug == away else away

        if winner is None:
            continue

        payout = float(af if winner == away else hf if winner == home else 0)
        pnl = payout - cost
        trader.bankroll += payout
        now = datetime.now(timezone.utc)
        await conn.execute(
            """UPDATE paper_positions
               SET status='settled', winner=?, payout=?, pnl=?, settled_at=?
               WHERE id=?""",
            (winner, payout, pnl, now.isoformat(), pid),
        )
        settled += 1
        log.info("SETTLED %s: %s won, P&L $%+.2f", game[:30], winner, pnl)

    await conn.commit()
    return settled


async def report(db_path: Path, starting_bankroll: float) -> dict:
    """
    P&L summary, with hedged and broken positions kept apart.

    The blended number is not the useful one: a run can show profit purely
    because broken positions happened to land on winning teams, which is
    variance, not edge.
    """
    conn = await aiosqlite.connect(db_path)
    try:
        async with conn.execute(
            """SELECT fill_status, status, COUNT(*), ROUND(SUM(cost),2),
                      ROUND(SUM(pnl),2), ROUND(SUM(expected_profit),2)
               FROM paper_positions GROUP BY fill_status, status"""
        ) as cur:
            rows = await cur.fetchall()

        async with conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)
               FROM paper_positions WHERE status='settled'"""
        ) as cur:
            n_settled, n_win = await cur.fetchone()

        realized = sum(r[4] or 0 for r in rows if r[1] == "settled")
        hedged_pnl = sum(
            r[4] or 0 for r in rows if r[0] == "hedged" and r[1] == "settled"
        )
        broken_pnl = sum(
            r[4] or 0 for r in rows if r[0] == "broken" and r[1] == "settled"
        )
        expected = sum(
            r[5] or 0 for r in rows if r[0] == "hedged" and r[1] == "settled"
        )

        return {
            "starting_bankroll": starting_bankroll,
            "ending_bankroll": starting_bankroll + realized,
            "realized_pnl": realized,
            "hedged_pnl": hedged_pnl,
            "broken_pnl": broken_pnl,
            "expected_on_hedged": expected,
            "settled": n_settled or 0,
            "winners": n_win or 0,
            "by_group": rows,
        }
    finally:
        await conn.close()
