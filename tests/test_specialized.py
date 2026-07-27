import pytest

from arbengine.detectors.specialized import (
    detect_complement,
    detect_monotonic,
    detect_partition,
)
from tests.conftest import make_contract, make_group


# ── Complement ────────────────────────────────────────────────────────────────

def test_complement_locks_profit_with_zero_fees(now) -> None:
    """YES at 0.48 + NO at 0.48 pays $1: 4 cents locked per set before fees."""
    yes = make_contract("MKT-YES", ask=0.48, ask_size=10)
    no = make_contract("MKT-NO", ask=0.48, ask_size=10)

    opp = detect_complement(yes, no, fee_multiplier=0.0, now=now)

    assert opp is not None
    assert opp.type == "complement"
    assert opp.profit_per_set == pytest.approx(0.04)
    assert opp.fillable_sets == 10
    assert opp.guaranteed_profit == pytest.approx(0.40)  # 0.04 × 10 sets
    assert opp.leg_count == 2


def test_realistic_fee_erases_a_one_cent_complement(now) -> None:
    """
    The whole point of charging fees before flagging: a 1-cent gross gap is not
    an arbitrage. At 0.495 the fee is $0.02 per contract, so the 1 cent of gross
    edge becomes a 3-cent loss.
    """
    yes = make_contract("MKT-YES", ask=0.495, ask_size=10)
    no = make_contract("MKT-NO", ask=0.495, ask_size=10)

    assert detect_complement(yes, no, fee_multiplier=0.0, now=now) is not None
    assert detect_complement(yes, no, fee_multiplier=0.07, now=now) is None


def test_complement_needs_depth_on_both_sides(now) -> None:
    yes = make_contract("MKT-YES", ask=0.40, ask_size=10)
    no = make_contract("MKT-NO", ask=0.40, ask_size=0)
    no = no.model_copy(update={"ask_size": 0})
    assert detect_complement(yes, no, fee_multiplier=0.0, now=now) is None


def test_complement_fillable_is_the_thinner_leg(now) -> None:
    yes = make_contract("MKT-YES", ask=0.40, ask_size=100)
    no = make_contract("MKT-NO", ask=0.40, ask_size=7)
    opp = detect_complement(yes, no, fee_multiplier=0.0, now=now)
    assert opp.fillable_sets == 7


def test_fairly_priced_complement_does_not_fire(now) -> None:
    yes = make_contract("MKT-YES", ask=0.50, ask_size=10)
    no = make_contract("MKT-NO", ask=0.50, ask_size=10)
    assert detect_complement(yes, no, fee_multiplier=0.0, now=now) is None


# ── Partition ─────────────────────────────────────────────────────────────────

def test_partition_of_three_at_thirty_cents(now) -> None:
    """Three MECE brackets at 0.30 each cost 0.90 and pay $1: 0.10 locked."""
    contracts = [
        make_contract("A", ask=0.30, ask_size=20),
        make_contract("B", ask=0.30, ask_size=20),
        make_contract("C", ask=0.30, ask_size=20),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])

    opp = detect_partition(group, fee_multiplier=0.0, now=now)

    assert opp is not None
    assert opp.type == "partition"
    assert abs(opp.profit_per_set - 0.10) < 1e-9
    assert opp.fillable_sets == 20
    assert opp.leg_count == 3


def test_partition_summing_to_one_does_not_fire(now) -> None:
    contracts = [
        make_contract("A", ask=0.30, ask_size=20),
        make_contract("B", ask=0.30, ask_size=20),
        make_contract("C", ask=0.40, ask_size=20),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    assert detect_partition(group, fee_multiplier=0.0, now=now) is None


def test_partition_rejects_non_exhaustive_coverage(now) -> None:
    """
    If some state is covered by no contract, buying one of each does NOT
    guarantee $1 — the outcome can land in the uncovered state and pay nothing.
    Flagging that as arbitrage would be a directional bet in disguise.
    """
    contracts = [
        make_contract("A", ask=0.10, ask_size=20),
        make_contract("B", ask=0.10, ask_size=20),
    ]
    # Three states, but nothing covers the third.
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0]])
    assert detect_partition(group, fee_multiplier=0.0, now=now) is None


def test_partition_rejects_overlapping_coverage(now) -> None:
    """Overlapping brackets can pay $2, but they can also pay $0 — not a lock."""
    contracts = [
        make_contract("A", ask=0.10, ask_size=20),
        make_contract("B", ask=0.10, ask_size=20),
    ]
    group = make_group(contracts, [[1, 1], [0, 1]])
    assert detect_partition(group, fee_multiplier=0.0, now=now) is None


def test_partition_skips_ladder_shaped_groups(now) -> None:
    contracts = [
        make_contract("A", ask=0.10, ask_size=20),
        make_contract("B", ask=0.10, ask_size=20),
    ]
    group = make_group(contracts, [[1, 1], [0, 1]], shape="ladder")
    assert detect_partition(group, fee_multiplier=0.0, now=now) is None


# ── Monotonic ladder ──────────────────────────────────────────────────────────

def test_monotonic_inversion_locks_the_difference(now) -> None:
    """
    "At least 90" implies "at least 85", so P(>=90) must be <= P(>=85).
    Quoted at 0.65 and 0.60 that is inverted: sell the 90 at 0.65, buy the 85
    at 0.60, lock 0.05.
    """
    at_85 = make_contract("L-85", ask=0.60, bid=0.60, ask_size=10, bid_size=10)
    at_90 = make_contract("L-90", ask=0.65, bid=0.65, ask_size=10, bid_size=10)

    # States: [<85, 85-90, >=90]. "at least 85" pays in the last two,
    # "at least 90" only in the last. So 90 implies 85.
    group = make_group(
        [at_85, at_90],
        [[0, 1, 1], [0, 0, 1]],
        shape="ladder",
    )

    found = detect_monotonic(group, fee_multiplier=0.0, now=now)

    assert len(found) == 1
    opp = found[0]
    assert opp.type == "monotonic"
    assert abs(opp.profit_per_set - 0.05) < 1e-9
    assert opp.leg_count == 2
    sides = {leg.ticker: leg.side for leg in opp.legs}
    assert sides["L-90"] == "sell"   # the rich subset
    assert sides["L-85"] == "buy"    # the cheap superset


def test_correctly_ordered_ladder_does_not_fire(now) -> None:
    at_85 = make_contract("L-85", ask=0.65, bid=0.64, ask_size=10, bid_size=10)
    at_90 = make_contract("L-90", ask=0.60, bid=0.59, ask_size=10, bid_size=10)
    group = make_group([at_85, at_90], [[0, 1, 1], [0, 0, 1]], shape="ladder")
    assert detect_monotonic(group, fee_multiplier=0.0, now=now) == []


def test_monotonic_fee_erases_a_thin_inversion(now) -> None:
    at_85 = make_contract("L-85", ask=0.60, bid=0.60, ask_size=10, bid_size=10)
    at_90 = make_contract("L-90", ask=0.61, bid=0.61, ask_size=10, bid_size=10)
    group = make_group([at_85, at_90], [[0, 1, 1], [0, 0, 1]], shape="ladder")

    assert len(detect_monotonic(group, fee_multiplier=0.0, now=now)) == 1
    assert detect_monotonic(group, fee_multiplier=0.07, now=now) == []


def test_monotonic_requires_bid_on_the_short_leg(now) -> None:
    at_85 = make_contract("L-85", ask=0.60, ask_size=10)
    at_90 = make_contract("L-90", ask=0.65, ask_size=10)  # no bid to sell into
    group = make_group([at_85, at_90], [[0, 1, 1], [0, 0, 1]], shape="ladder")
    assert detect_monotonic(group, fee_multiplier=0.0, now=now) == []
