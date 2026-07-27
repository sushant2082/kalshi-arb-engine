import numpy as np
import pytest

from arbengine.detectors.lp import detect_lp, solve_lp, state_prices
from arbengine.detectors.specialized import (
    detect_complement,
    detect_monotonic,
    detect_partition,
)
from tests.conftest import make_contract, make_group


# ── Basic detection ───────────────────────────────────────────────────────────

def test_lp_finds_the_partition_arbitrage(now) -> None:
    contracts = [
        make_contract("A", ask=0.30, ask_size=20),
        make_contract("B", ask=0.30, ask_size=20),
        make_contract("C", ask=0.30, ask_size=20),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])

    opp = detect_lp(group, fee_multiplier=0.0, now=now)

    assert opp is not None
    assert opp.type == "lp"
    # 0.10 per set × 20 sets available.
    assert opp.guaranteed_profit == pytest.approx(2.0, abs=1e-6)


def test_lp_finds_no_arb_in_a_coherent_partition(now) -> None:
    contracts = [
        make_contract("A", ask=0.30, ask_size=20),
        make_contract("B", ask=0.30, ask_size=20),
        make_contract("C", ask=0.40, ask_size=20),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    assert detect_lp(group, fee_multiplier=0.0, now=now) is None


def test_t_star_is_never_negative(now) -> None:
    """The zero portfolio with t=0 is always feasible, so t* >= 0 always."""
    payoff = np.array([[1.0, 0.0], [0.0, 1.0]])
    result = solve_lp(
        payoff, asks=[0.90, 0.90], bids=[0.85, 0.85],
        ask_sizes=[10, 10], bid_sizes=[10, 10], fee_multiplier=0.0,
    )
    assert result is not None
    t_star, _, _ = result
    assert t_star >= -1e-9


def test_lp_returns_none_when_there_is_no_depth(now) -> None:
    payoff = np.array([[1.0, 0.0], [0.0, 1.0]])
    assert solve_lp(
        payoff, asks=[0.30, 0.30], bids=[None, None],
        ask_sizes=[0, 0], bid_sizes=[0, 0], fee_multiplier=0.0,
    ) is None


def test_lp_respects_depth_bounds(now) -> None:
    """Profit scales with available size, not unboundedly."""
    contracts = [
        make_contract("A", ask=0.30, ask_size=5),
        make_contract("B", ask=0.30, ask_size=5),
        make_contract("C", ask=0.30, ask_size=5),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    opp = detect_lp(group, fee_multiplier=0.0, now=now)
    assert opp.guaranteed_profit == pytest.approx(0.5, abs=1e-6)  # 0.10 × 5


def test_lp_finds_the_monotonic_inversion(now) -> None:
    at_85 = make_contract("L-85", ask=0.60, bid=0.60, ask_size=10, bid_size=10)
    at_90 = make_contract("L-90", ask=0.65, bid=0.65, ask_size=10, bid_size=10)
    group = make_group([at_85, at_90], [[0, 1, 1], [0, 0, 1]], shape="ladder")

    opp = detect_lp(group, fee_multiplier=0.0, now=now)

    assert opp is not None
    assert opp.guaranteed_profit == pytest.approx(0.5, abs=1e-6)  # 0.05 × 10


# ── Cross-check: the LP is the general case ───────────────────────────────────

CROSS_CHECK_CASES = [
    (
        "complement",
        [make_contract("Y", ask=0.48, ask_size=10),
         make_contract("N", ask=0.48, ask_size=10)],
        [[1, 0], [0, 1]],
        "bracket",
    ),
    (
        "partition-3",
        [make_contract("A", ask=0.30, ask_size=20),
         make_contract("B", ask=0.30, ask_size=20),
         make_contract("C", ask=0.30, ask_size=20)],
        [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "bracket",
    ),
    (
        "partition-4-thin",
        [make_contract("A", ask=0.20, ask_size=3),
         make_contract("B", ask=0.25, ask_size=8),
         make_contract("C", ask=0.25, ask_size=15),
         make_contract("D", ask=0.25, ask_size=6)],
        [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        "bracket",
    ),
    (
        "monotonic",
        [make_contract("L-85", ask=0.60, bid=0.60, ask_size=10, bid_size=10),
         make_contract("L-90", ask=0.65, bid=0.65, ask_size=10, bid_size=10)],
        [[0, 1, 1], [0, 0, 1]],
        "ladder",
    ),
]


@pytest.mark.parametrize(
    "name, contracts, payoff, shape",
    CROSS_CHECK_CASES,
    ids=[c[0] for c in CROSS_CHECK_CASES],
)
def test_lp_profit_is_at_least_specialized_profit(
    name, contracts, payoff, shape, now
) -> None:
    """
    Every specialized detector is a special case of the LP, so the LP must never
    find less than they do. If this breaks, one of the two is wrong — and the
    scanner logs a warning at runtime for exactly this reason.
    """
    group = make_group(contracts, payoff, shape=shape)

    specialized = 0.0
    if len(contracts) == 2 and shape == "bracket":
        opp = detect_complement(contracts[0], contracts[1], 0.0, now)
        if opp:
            specialized = max(specialized, opp.guaranteed_profit)
    part = detect_partition(group, 0.0, now)
    if part:
        specialized = max(specialized, part.guaranteed_profit)
    for mono in detect_monotonic(group, 0.0, now):
        specialized = max(specialized, mono.guaranteed_profit)

    lp_opp = detect_lp(group, 0.0, now)
    lp_profit = lp_opp.guaranteed_profit if lp_opp else 0.0

    assert specialized > 0, f"{name}: specialized detector should have fired"
    assert lp_profit >= specialized - 1e-6, (
        f"{name}: LP found ${lp_profit:.4f} but specialized found ${specialized:.4f}"
    )


# ── Integer flooring ──────────────────────────────────────────────────────────

def test_integer_quantities_only(now) -> None:
    """Kalshi trades whole contracts; the reported portfolio must be integral."""
    contracts = [
        make_contract("A", ask=0.30, ask_size=7),
        make_contract("B", ask=0.30, ask_size=13),
        make_contract("C", ask=0.30, ask_size=11),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    opp = detect_lp(group, fee_multiplier=0.0, now=now)
    assert opp is not None
    for leg in opp.legs:
        assert isinstance(leg.qty, int)
        assert leg.qty > 0


def test_reported_profit_is_the_integer_worst_case(now) -> None:
    """
    Flooring the LP solution can only shrink the position, but it can also break
    the hedge. The reported profit must be the recomputed worst case over all
    states for the integer portfolio, never the LP's continuous optimum.
    """
    contracts = [
        make_contract("A", ask=0.30, ask_size=7),
        make_contract("B", ask=0.30, ask_size=7),
        make_contract("C", ask=0.30, ask_size=7),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    opp = detect_lp(group, fee_multiplier=0.0, now=now)

    net = np.zeros(3)
    for leg in opp.legs:
        i = [c.ticker for c in contracts].index(leg.ticker)
        net[i] += leg.qty if leg.side == "buy" else -leg.qty

    terminal = group.payoff.T @ net
    cash_out = sum(leg.price * leg.qty for leg in opp.legs)
    worst = terminal.min() - cash_out

    assert opp.guaranteed_profit == pytest.approx(worst, abs=1e-6)
    # And it must genuinely be a worst case: profitable in EVERY state.
    assert all(t - cash_out >= opp.guaranteed_profit - 1e-6 for t in terminal)


# ── State prices (no-arb diagnostic) ──────────────────────────────────────────

def test_state_prices_exist_when_coherent() -> None:
    """A coherent partition admits a valid state-price vector."""
    payoff = np.array([[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]])
    pi = state_prices(
        payoff, asks=[0.35, 0.35, 0.40], bids=[0.30, 0.30, 0.35],
        fee_multiplier=0.0,
    )
    assert pi is not None
    assert np.all(pi >= -1e-9)
    # Implied total should sit between the bid and ask sums.
    assert 0.95 <= pi.sum() <= 1.10


def test_state_prices_absent_when_arbitrage_exists() -> None:
    """
    An incoherent price system admits no valid state prices — that infeasibility
    IS the arbitrage. Bids above asks across a partition force pi to be both
    above and below itself.
    """
    payoff = np.array([[1.0, 0.0], [0.0, 1.0]])
    pi = state_prices(
        payoff, asks=[0.20, 0.20], bids=[0.70, 0.70], fee_multiplier=0.0,
    )
    assert pi is None
