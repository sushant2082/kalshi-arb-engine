"""
O(n) detectors for the common coherence violations.

These run before the LP because they are cheap and because the portfolios they
produce are structurally easier to fill: a 2-leg monotonic lock or an all-legs
partition is far more executable than an arbitrary LP solution vector, and
Kalshi has no atomic multi-leg fill, so leg count is the dominant real-world
risk. Every detector here charges fees before flagging.

Anything these find, the LP must also find with at least as much profit. That
invariant is asserted in the tests — if it ever breaks, one of the two is wrong.
"""

import logging
from datetime import datetime

import numpy as np

from arbengine.fees import fee_per_contract
from arbengine.models import ArbOpportunity, Contract, ContractGroup, Leg

log = logging.getLogger(__name__)


def _buyable(c: Contract) -> bool:
    return c.ask is not None and c.ask_size > 0


def _sellable(c: Contract) -> bool:
    return c.bid is not None and c.bid_size > 0


def _mk(
    group_id: str,
    type_: str,
    legs: list[Leg],
    guaranteed_profit: float,
    fillable_sets: int,
    now: datetime,
) -> ArbOpportunity:
    total_fee = sum(leg.fee * leg.qty for leg in legs)
    total_cost = -sum(leg.cash_flow for leg in legs)
    return ArbOpportunity(
        group_id=group_id,
        type=type_,
        legs=legs,
        total_cost=total_cost,
        total_fee=total_fee,
        guaranteed_profit=guaranteed_profit,
        fillable_sets=fillable_sets,
        min_leg_size=min((leg.qty for leg in legs), default=0),
        leg_count=len(legs),
        first_seen=now,
        last_seen=now,
    )


# ── Complement ────────────────────────────────────────────────────────────────

def detect_complement(
    yes: Contract,
    no: Contract,
    fee_multiplier: float,
    now: datetime,
    min_profit: float = 0.0,
) -> ArbOpportunity | None:
    """
    A binary market's YES and NO together pay exactly $1 in every state. If both
    can be bought for less than $1 all-in, that difference is locked.

    `no` here is the complementary contract expressed as a YES-side buy (Kalshi
    quotes NO as its own book; source/kalshi.py normalizes it).
    """
    if not (_buyable(yes) and _buyable(no)):
        return None

    fee_yes = fee_per_contract(yes.ask, fee_multiplier)
    fee_no = fee_per_contract(no.ask, fee_multiplier)
    cost_per_set = yes.ask + fee_yes + no.ask + fee_no
    profit_per_set = 1.0 - cost_per_set

    if profit_per_set <= min_profit:
        return None

    sets = min(yes.ask_size, no.ask_size)
    if sets <= 0:
        return None

    legs = [
        Leg(ticker=yes.ticker, side="buy", qty=sets, price=yes.ask, fee=fee_yes),
        Leg(ticker=no.ticker, side="buy", qty=sets, price=no.ask, fee=fee_no),
    ]
    return _mk(
        yes.ticker, "complement", legs, profit_per_set * sets, sets, now,
    )


# ── Partition ─────────────────────────────────────────────────────────────────

def detect_partition(
    group: ContractGroup,
    fee_multiplier: float,
    now: datetime,
    min_profit: float = 0.0,
) -> ArbOpportunity | None:
    """
    n mutually exclusive, collectively exhaustive contracts pay exactly $1 in
    total in every state. If the all-in cost of one of each is under $1, the
    difference is locked.

    Requires the group to be a validated bracket set — the exhaustiveness check
    in groups.validate_group is what makes the $1 payout guaranteed. Ladders are
    nested, not exhaustive, so this does not apply to them.
    """
    if group.shape != "bracket":
        return None

    contracts = group.contracts
    if len(contracts) < 2:
        return None
    if not all(_buyable(c) for c in contracts):
        return None

    # Every state must be covered exactly once for the $1 guarantee to hold.
    col_sums = group.payoff.sum(axis=0)
    if not np.all(col_sums == 1):
        return None

    fees = [fee_per_contract(c.ask, fee_multiplier) for c in contracts]
    cost_per_set = sum(c.ask for c in contracts) + sum(fees)
    profit_per_set = 1.0 - cost_per_set

    if profit_per_set <= min_profit:
        return None

    sets = min(c.ask_size for c in contracts)
    if sets <= 0:
        return None

    legs = [
        Leg(ticker=c.ticker, side="buy", qty=sets, price=c.ask, fee=f)
        for c, f in zip(contracts, fees)
    ]
    return _mk(
        group.group_id, "partition", legs, profit_per_set * sets, sets, now,
    )


# ── Monotonic ladder (implication) ────────────────────────────────────────────

def _implies(payoff_a: np.ndarray, payoff_b: np.ndarray) -> bool:
    """
    True if event A implies event B: every state where A pays, B also pays.
    A is then a subset of B and must be priced at or below B.
    """
    return bool(np.all(payoff_b >= payoff_a)) and bool(np.any(payoff_b > payoff_a))


def detect_monotonic(
    group: ContractGroup,
    fee_multiplier: float,
    now: datetime,
    min_profit: float = 0.0,
    type_: str = "monotonic",
) -> list[ArbOpportunity]:
    """
    Where event A implies event B, coherence requires price(A) <= price(B).

    On inversion — the strict subset A quoted richer than its superset B — buy
    the cheap superset and sell the rich subset. In every state where the short
    A pays out $1, the long B also pays $1 and covers it; in every other state
    the short pays nothing. So the position can never lose, and the entry credit
    net of fees is locked.

    Returns every inversion found, not just the best one, because they are
    independent 2-leg locks and the scanner ranks them itself.
    """
    found: list[ArbOpportunity] = []
    contracts = group.contracts
    payoff = group.payoff

    for i, a in enumerate(contracts):
        for j, b in enumerate(contracts):
            if i == j:
                continue
            if not _implies(payoff[i], payoff[j]):
                continue
            # A implies B, so we must have price(A) <= price(B).
            # Inversion: sell A at its bid, buy B at its ask, for a net credit.
            if not (_sellable(a) and _buyable(b)):
                continue

            fee_sell = fee_per_contract(a.bid, fee_multiplier)
            fee_buy = fee_per_contract(b.ask, fee_multiplier)
            credit_per_set = (a.bid - fee_sell) - (b.ask + fee_buy)

            if credit_per_set <= min_profit:
                continue

            sets = min(a.bid_size, b.ask_size)
            if sets <= 0:
                continue

            legs = [
                Leg(ticker=b.ticker, side="buy", qty=sets, price=b.ask, fee=fee_buy),
                Leg(ticker=a.ticker, side="sell", qty=sets, price=a.bid, fee=fee_sell),
            ]
            found.append(
                _mk(
                    group.group_id, type_, legs,
                    credit_per_set * sets, sets, now,
                )
            )

    return found


def detect_time_monotonic(
    group: ContractGroup,
    fee_multiplier: float,
    now: datetime,
    min_profit: float = 0.0,
) -> list[ArbOpportunity]:
    """
    "Happens by an earlier date" implies "happens by a later date", so prices
    must be non-decreasing in horizon. Structurally identical to the threshold
    ladder once the payoff matrix encodes the implication, so it reuses the same
    routine and only differs in how it is labelled and ranked.
    """
    return detect_monotonic(
        group, fee_multiplier, now, min_profit, type_="time_monotonic"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def run_specialized(
    group: ContractGroup,
    fee_multiplier: float,
    now: datetime,
    min_profit: float = 0.0,
) -> list[ArbOpportunity]:
    """Run every applicable specialized detector against a validated group."""
    out: list[ArbOpportunity] = []

    if group.shape == "binary" and len(group.contracts) == 2:
        opp = detect_complement(
            group.contracts[0], group.contracts[1], fee_multiplier, now, min_profit
        )
        if opp:
            out.append(opp)

    partition = detect_partition(group, fee_multiplier, now, min_profit)
    if partition:
        out.append(partition)

    out.extend(detect_monotonic(group, fee_multiplier, now, min_profit))

    return out
