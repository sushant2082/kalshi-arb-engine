import random
from datetime import timedelta

import pytest

from arbengine.detectors.specialized import detect_partition
from arbengine.paper import PaperBroker, summarize
from tests.conftest import make_contract, make_group


def _partition_group():
    contracts = [
        make_contract("A", ask=0.30, ask_size=10),
        make_contract("B", ask=0.30, ask_size=10),
        make_contract("C", ask=0.30, ask_size=10),
    ]
    return make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])


def test_complete_fill_locks_the_expected_profit(now) -> None:
    """
    A fully filled partition must pay $1 per set no matter which state settles.
    Checking every state is the point — a lock that only works in one of them
    is not a lock.
    """
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)
    assert opp is not None

    for state in range(3):
        broker = PaperBroker(bankroll=1000.0, fee_multiplier=0.0)
        pos = broker.attempt(opp, now)
        assert pos.fill_status == "complete"
        assert pos.sets_filled == 10

        settled = broker.settle(pos, group, state, now + timedelta(hours=1))
        assert settled.realized_payout == pytest.approx(10.0)
        assert settled.pnl == pytest.approx(1.0)  # 0.10 × 10 sets
        assert broker.bankroll == pytest.approx(1001.0)


def test_broken_legs_leave_a_directional_exposure(now) -> None:
    """
    With a fill probability below 1, some legs miss. The residual is NOT a lock:
    it wins in the states its filled legs cover and loses in the rest. The
    simulator must keep it and settle it honestly rather than discard the trade.
    """
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)

    broker = PaperBroker(
        bankroll=1000.0, fee_multiplier=0.0,
        leg_fill_prob=0.5, rng=random.Random(7),
    )
    pos = broker.attempt(opp, now)
    assert pos is not None
    assert pos.fill_status == "broken"
    assert pos.sets_filled == 0
    assert pos.expected_profit == 0.0

    filled = {f.ticker for f in pos.fills if f.filled_qty > 0}
    missed = {f.ticker for f in pos.fills if f.filled_qty == 0}
    assert filled and missed, "this seed should produce a genuinely partial fill"

    # Settling in a state covered only by a missed leg must be a real loss.
    index = {c.ticker: i for i, c in enumerate(group.contracts)}
    lost_state = index[sorted(missed)[0]]
    settled = broker.settle(pos, group, lost_state, now + timedelta(hours=1))
    assert settled.realized_payout == 0.0
    assert settled.pnl < 0


def test_slippage_erodes_the_edge(now) -> None:
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)

    clean = PaperBroker(bankroll=1000.0, fee_multiplier=0.0)
    slipped = PaperBroker(bankroll=1000.0, fee_multiplier=0.0, slippage_cents=2.0)

    a = clean.settle(clean.attempt(opp, now), group, 0, now)
    b = slipped.settle(slipped.attempt(opp, now), group, 0, now)

    assert b.pnl < a.pnl
    # 3 legs × 2 cents × 10 sets = $0.60 of extra cost.
    assert a.pnl - b.pnl == pytest.approx(0.60, abs=1e-6)


def test_max_sets_caps_position_size(now) -> None:
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)

    broker = PaperBroker(bankroll=1000.0, fee_multiplier=0.0, max_sets_per_opp=4)
    pos = broker.attempt(opp, now)
    assert pos.sets_attempted == 4
    assert pos.fill_status == "partial"
    assert all(f.filled_qty == 4 for f in pos.fills)


def test_insufficient_bankroll_blocks_the_trade(now) -> None:
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)
    broker = PaperBroker(bankroll=1.0, fee_multiplier=0.0)
    assert broker.attempt(opp, now) is None


def test_settlement_outside_the_state_space_is_an_error(now) -> None:
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)
    broker = PaperBroker(bankroll=1000.0, fee_multiplier=0.0)
    pos = broker.attempt(opp, now)
    with pytest.raises(ValueError):
        broker.settle(pos, group, 99, now)


def test_summary_separates_locked_from_broken(now) -> None:
    """
    Blending locked and broken P&L hides the only number that decides whether
    this is tradeable for real, so the summary must keep them apart.
    """
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)

    broker = PaperBroker(bankroll=1000.0, fee_multiplier=0.0)
    good = broker.settle(broker.attempt(opp, now), group, 0, now)

    broken_broker = PaperBroker(
        bankroll=1000.0, fee_multiplier=0.0,
        leg_fill_prob=0.5, rng=random.Random(7),
    )
    bad_pos = broken_broker.attempt(opp, now)
    index = {c.ticker: i for i, c in enumerate(group.contracts)}
    missed = sorted(f.ticker for f in bad_pos.fills if f.filled_qty == 0)
    bad = broken_broker.settle(bad_pos, group, index[missed[0]], now)

    s = summarize([good, bad], starting_bankroll=1000.0)
    assert s["locked_count"] == 1
    assert s["broken_count"] == 1
    assert s["locked_pnl"] > 0
    assert s["broken_pnl"] < 0
    assert s["realization_ratio"] == pytest.approx(1.0, abs=1e-6)


def test_realization_ratio_flags_a_broken_fee_model(now) -> None:
    """
    If realized P&L on *locked* positions trails what the detector promised, the
    fee model or the state space is wrong. That ratio is the tripwire.
    """
    group = _partition_group()
    opp = detect_partition(group, fee_multiplier=0.0, now=now)

    # Detector priced with zero fees, execution charges the real fee.
    broker = PaperBroker(bankroll=1000.0, fee_multiplier=0.07)
    pos = broker.attempt(opp, now)
    settled = broker.settle(pos, group, 0, now)

    s = summarize([settled], starting_bankroll=1000.0)
    assert s["realization_ratio"] < 1.0
