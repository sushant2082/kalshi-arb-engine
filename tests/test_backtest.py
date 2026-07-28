"""
The invariant these tests defend: anything the engine reports as arbitrage must
be profitable in its WORST settlement state. Average profit is not arbitrage —
a directional bet is profitable on average too.
"""

import numpy as np
import pytest

from arbengine.backtest import (
    _cheapen_all_asks,
    _invert_ladder_pair,
    backtest_group,
    run_scenario,
    summarize,
    verify_riskless,
)
from arbengine.config import Settings
from arbengine.scanner import scan_group
from tests.conftest import make_contract, make_group


def _settings(**kw) -> Settings:
    base = {
        "kalshi_api_key_id": "test",
        "fee_multiplier": 0.0,
        "min_guaranteed_profit": 0.005,
        "min_fillable_sets": 1,
    }
    base.update(kw)
    return Settings(**base)


def _partition_group(asks=(0.34, 0.34, 0.34)):
    contracts = [
        make_contract(t, ask=a, ask_size=20)
        for t, a in zip("ABC", asks)
    ]
    return make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])


def _ladder_group():
    contracts = [
        make_contract("L-85", ask=0.60, bid=0.58, ask_size=20, bid_size=20),
        make_contract("L-90", ask=0.50, bid=0.48, ask_size=20, bid_size=20),
    ]
    # States: [<85, 85-90, >=90]; "at least 90" implies "at least 85".
    return make_group(contracts, [[0, 1, 1], [0, 0, 1]], shape="ladder")


# ── Perturbation helpers ──────────────────────────────────────────────────────

def test_cheapening_makes_a_coherent_partition_incoherent(now) -> None:
    group = _partition_group()
    assert scan_group(group, _settings(), now) == []

    cheap = _cheapen_all_asks(group, 0.95 / sum(c.ask for c in group.contracts))
    assert scan_group(cheap, _settings(), now) != []


def test_inversion_picks_a_pair_with_headroom() -> None:
    """
    Injecting into a leg already near $0.99 would clamp the margin and make a
    correct refusal look like a missed detection. The injector must avoid that.
    """
    contracts = [
        make_contract("L-85", ask=0.98, bid=0.97, ask_size=20, bid_size=20),
        make_contract("L-90", ask=0.985, bid=0.975, ask_size=20, bid_size=20),
    ]
    group = make_group(contracts, [[0, 1, 1], [0, 0, 1]], shape="ladder")
    # No pair leaves room for a 5-cent inversion under the 0.99 ceiling.
    assert _invert_ladder_pair(group, 0.05) is None


def test_injected_inversion_actually_fires(now) -> None:
    group = _ladder_group()
    assert scan_group(group, _settings(), now) == []

    perturbed = _invert_ladder_pair(group, 0.05)
    assert perturbed is not None
    assert scan_group(perturbed, _settings(), now) != []


# ── The core invariant ────────────────────────────────────────────────────────

def test_reported_lock_is_profitable_in_every_state(now) -> None:
    group = _partition_group()
    cheap = _cheapen_all_asks(group, 0.90 / sum(c.ask for c in group.contracts))
    found = scan_group(cheap, _settings(), now)
    assert found

    opp = max(found, key=lambda o: o.guaranteed_profit)
    worst, best, n, _ = verify_riskless(opp, cheap, _settings())

    assert n == cheap.state_space.n, "every state must be settled, not just one"
    assert worst > 0, "a lock must pay in its WORST state"
    # A true partition lock is flat: identical P&L whatever settles.
    assert best == pytest.approx(worst, abs=1e-9)


def test_ladder_lock_pays_at_least_the_floor_in_every_state(now) -> None:
    """
    A 2-leg monotonic lock has upside in some states but must never dip below
    its guaranteed floor in any of them.
    """
    perturbed = _invert_ladder_pair(_ladder_group(), 0.05)
    found = scan_group(perturbed, _settings(), now)
    assert found

    opp = max(found, key=lambda o: o.guaranteed_profit)
    worst, best, n, _ = verify_riskless(opp, perturbed, _settings())
    assert n > 0
    assert worst > 0
    assert best >= worst


def test_verify_riskless_would_catch_a_losing_portfolio(now) -> None:
    """
    Guard on the guard: if a detector ever reported a portfolio that loses in
    some state, verify_riskless must report a negative worst case rather than
    averaging the loss away.
    """
    from arbengine.models import ArbOpportunity, Leg

    group = _partition_group()
    # Deliberately unbalanced: buy only one leg of a three-way partition. It
    # pays $1 in one state and nothing in the other two.
    bogus = ArbOpportunity(
        group_id=group.group_id, type="partition",
        legs=[Leg(ticker="A", side="buy", qty=10, price=0.34, fee=0.0)],
        total_cost=3.4, total_fee=0.0, guaranteed_profit=6.6,
        fillable_sets=10, min_leg_size=10, leg_count=1,
        first_seen=now, last_seen=now,
    )
    worst, best, n, _ = verify_riskless(bogus, group, _settings())
    assert n == 3
    assert worst < 0, "an unhedged position must show a losing state"
    assert best > 0


# ── Reporting ─────────────────────────────────────────────────────────────────

def test_fee_free_group_fires_on_a_thinner_dislocation(now) -> None:
    """
    Fees are what kill thin arbitrage, so a fee-free group must detect a
    dislocation that a fee-paying group of the same shape rejects.
    """
    group = _partition_group()
    cheap = _cheapen_all_asks(group, 0.99 / sum(c.ask for c in group.contracts))

    free = cheap.model_copy(update={"fee_scale": 0.0})
    paid = cheap.model_copy(update={"fee_scale": 1.0})
    settings = _settings(fee_multiplier=0.07)

    assert scan_group(free, settings, now) != []
    assert scan_group(paid, settings, now) == []


def test_summarize_separates_misses_from_false_locks(now) -> None:
    group = _partition_group()
    results = backtest_group(group, _settings())
    s = summarize(results)
    assert s["scenarios"] == len(results)
    assert s["failed"] == 0, "no scenario should fire without being riskless"


def test_scenario_records_a_non_firing_case(now) -> None:
    """A coherent group must produce a clean no-fire, not a spurious lock."""
    group = _partition_group()
    result = run_scenario(group, _settings(), "unchanged", group)
    assert not result.detected
    assert not result.passed
