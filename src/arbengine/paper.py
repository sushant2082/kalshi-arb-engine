"""
Paper-trading simulator.

SIMULATION ONLY. Nothing in this module calls a Kalshi write endpoint, and the
read-only client it sits behind does not expose one. This exists to answer
"would this have made money" before any real capital is committed.

The central honesty problem: Kalshi has no atomic multi-leg fill. A naive
simulator that assumes every leg fills at the quoted price will report a
flawless win rate for a strategy that in reality gets picked off leg by leg. So
this simulator models legs as filling *independently and sequentially*, and when
they fill unevenly it does not discard the trade — it keeps the unhedged
residual as a real directional exposure and settles it honestly. A "broken"
position is the whole point of running this.
"""

import logging
import random
from datetime import datetime

import numpy as np

from arbengine.fees import fee_per_contract
from arbengine.models import (
    ArbOpportunity,
    ContractGroup,
    PaperFill,
    PaperPosition,
    opportunity_key,
)

log = logging.getLogger(__name__)


class PaperBroker:
    """
    Simulates execution of detected locks against quoted depth.

    `leg_fill_prob` is the per-leg probability of getting the quoted size at the
    quoted price. At 1.0 this is the optimistic bound and will overstate real
    performance; lower it to stress-test how much of the edge survives leg risk.
    `rng` is injectable so tests are deterministic.
    """

    def __init__(
        self,
        bankroll: float,
        max_sets_per_opp: int = 50,
        leg_fill_prob: float = 1.0,
        slippage_cents: float = 0.0,
        fee_multiplier: float = 0.07,
        rng: random.Random | None = None,
    ) -> None:
        self.starting_bankroll = bankroll
        self.bankroll = bankroll
        self.max_sets_per_opp = max_sets_per_opp
        self.leg_fill_prob = leg_fill_prob
        self.slippage_cents = slippage_cents
        self.fee_multiplier = fee_multiplier
        self._rng = rng or random.Random()

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _effective_price(self, price: float, side: str) -> float:
        """Apply adverse slippage: pay up when buying, receive less when selling."""
        slip = self.slippage_cents / 100.0
        adjusted = price + slip if side == "buy" else price - slip
        return min(max(adjusted, 0.0), 1.0)

    def attempt(
        self, opp: ArbOpportunity, now: datetime
    ) -> PaperPosition | None:
        """
        Simulate executing an opportunity. Returns None if the position cannot
        be afforded or nothing filled at all.
        """
        sets = min(opp.fillable_sets, self.max_sets_per_opp)
        if sets <= 0:
            return None

        scale = sets / opp.fillable_sets if opp.fillable_sets else 0.0

        fills: list[PaperFill] = []
        for leg in opp.legs:
            requested = max(int(leg.qty * scale), 0)
            if requested <= 0:
                continue

            filled = requested if self._rng.random() <= self.leg_fill_prob else 0
            price = self._effective_price(leg.price, leg.side)
            fills.append(
                PaperFill(
                    ticker=leg.ticker,
                    side=leg.side,
                    requested_qty=requested,
                    filled_qty=filled,
                    price=price,
                    fee=fee_per_contract(price, self.fee_multiplier),
                )
            )

        if not fills or all(f.filled_qty == 0 for f in fills):
            return None

        net_cash = sum(f.cash_flow for f in fills)
        cash_required = -net_cash if net_cash < 0 else 0.0
        if cash_required > self.bankroll:
            log.debug(
                "Skipping %s: needs $%.2f, bankroll $%.2f",
                opp.group_id, cash_required, self.bankroll,
            )
            return None

        if all(f.filled_qty == f.requested_qty for f in fills):
            fill_status = "complete" if sets == opp.fillable_sets else "partial"
            sets_filled = sets
        else:
            fill_status = "broken"
            sets_filled = 0

        self.bankroll += net_cash
        total_fee = sum(f.fee * f.filled_qty for f in fills)

        if fill_status == "broken":
            log.warning(
                "BROKEN LEG on %s: %s — residual is directional, not a lock",
                opp.group_id,
                [(f.ticker, f.filled_qty, f.requested_qty) for f in fills],
            )

        return PaperPosition(
            opportunity_key=opportunity_key(opp),
            group_id=opp.group_id,
            type=opp.type,
            entered_at=now,
            fills=fills,
            fill_status=fill_status,
            sets_attempted=sets,
            sets_filled=sets_filled,
            net_cash=net_cash,
            total_fee=total_fee,
            expected_profit=opp.profit_per_set * sets_filled,
            bankroll_at_entry=self.bankroll - net_cash,
        )

    # ── Settlement ────────────────────────────────────────────────────────────

    def settle(
        self,
        position: PaperPosition,
        group: ContractGroup,
        outcome_state: int,
        now: datetime,
    ) -> PaperPosition:
        """
        Settle a position given which state the outcome landed in.

        Payout is the terminal value of the net position in that state: each
        long YES contract that covers the state pays $1, each short YES contract
        that covers it costs $1. P&L is that payout plus the entry cash flow
        (which is negative for a net debit), so fees are already baked in.
        """
        if outcome_state < 0 or outcome_state >= group.state_space.n:
            raise ValueError(
                f"outcome_state {outcome_state} outside state space of size "
                f"{group.state_space.n}"
            )

        index = {c.ticker: i for i, c in enumerate(group.contracts)}
        net = np.zeros(len(group.contracts))
        for f in position.fills:
            i = index.get(f.ticker)
            if i is None:
                log.warning(
                    "Fill on %s has no contract in group %s; treating as zero payout",
                    f.ticker, group.group_id,
                )
                continue
            net[i] += f.filled_qty if f.side == "buy" else -f.filled_qty

        payout = float(group.payoff[:, outcome_state] @ net)
        pnl = payout + position.net_cash

        self.bankroll += payout

        return position.model_copy(update={
            "status": "settled",
            "realized_payout": payout,
            "pnl": pnl,
            "settled_at": now,
            "settlement_state": group.state_space.labels[outcome_state],
        })


def summarize(positions: list[PaperPosition], starting_bankroll: float) -> dict:
    """
    Aggregate paper results.

    Reports locked and broken performance separately on purpose. Blending them
    hides the only number that decides whether this is tradeable for real: what
    happens when the legs don't all fill.
    """
    settled = [p for p in positions if p.status == "settled" and p.pnl is not None]
    locked = [p for p in settled if p.fill_status in ("complete", "partial")]
    broken = [p for p in settled if p.fill_status == "broken"]

    total_pnl = sum(p.pnl for p in settled)
    locked_pnl = sum(p.pnl for p in locked)
    broken_pnl = sum(p.pnl for p in broken)

    wins = sum(1 for p in settled if p.pnl > 0)
    losses = sum(1 for p in settled if p.pnl < 0)

    expected = sum(p.expected_profit for p in locked)

    return {
        "starting_bankroll": starting_bankroll,
        "ending_bankroll": starting_bankroll + total_pnl,
        "total_pnl": total_pnl,
        "locked_pnl": locked_pnl,
        "broken_pnl": broken_pnl,
        "positions_total": len(positions),
        "positions_settled": len(settled),
        "positions_open": len(positions) - len(settled),
        "locked_count": len(locked),
        "broken_count": len(broken),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(settled) if settled else None,
        "expected_locked_profit": expected,
        # If realized materially trails expected on locked positions, the fee
        # model or the state space is wrong — this ratio is the tripwire.
        "realization_ratio": (locked_pnl / expected) if expected else None,
    }
