from datetime import datetime, timezone

import numpy as np

from arbengine.groups import (
    NEG_INF,
    POS_INF,
    build_group,
    build_payoff_matrix,
    build_state_space,
    contract_from_market,
    group_markets_by_event,
    infer_shape,
    interval_from_market,
    interval_from_subtitle,
    validate_group,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


# ── Strike parsing ────────────────────────────────────────────────────────────

def test_between_strike_becomes_a_bounded_interval() -> None:
    assert interval_from_market({
        "strike_type": "between", "floor_strike": 70, "cap_strike": 75,
    }) == (70.0, 75.0)


def test_greater_strike_becomes_an_upper_tail() -> None:
    assert interval_from_market({
        "strike_type": "greater", "floor_strike": 90,
    }) == (90.0, POS_INF)


def test_less_strike_becomes_a_lower_tail() -> None:
    assert interval_from_market({
        "strike_type": "less", "cap_strike": 60,
    }) == (NEG_INF, 60.0)


def test_unparseable_market_returns_none() -> None:
    assert interval_from_market({"strike_type": "structured"}) is None
    assert interval_from_market({}) is None


def test_subtitle_fallback_parses_ranges_and_tails() -> None:
    assert interval_from_subtitle("70 to 75") == (70.0, 75.0)
    assert interval_from_subtitle("$105,000 to $110,000") == (105000.0, 110000.0)
    assert interval_from_subtitle("Above 90") == (90.0, POS_INF)
    assert interval_from_subtitle("Below 60") == (NEG_INF, 60.0)
    assert interval_from_subtitle("something else entirely") is None


def test_structured_strikes_take_priority_over_subtitle() -> None:
    """
    Text parsing cannot distinguish inclusive from exclusive bounds, so the
    structured fields must win whenever both are present.
    """
    contract = contract_from_market(
        {
            "ticker": "T", "strike_type": "between",
            "floor_strike": 70, "cap_strike": 75,
            "yes_sub_title": "99 to 100",
        },
        None,
        NOW,
    )
    assert contract.interval == (70.0, 75.0)


# ── State space ───────────────────────────────────────────────────────────────

def test_state_space_tiles_a_bracket_set() -> None:
    intervals = [(NEG_INF, 70.0), (70.0, 75.0), (75.0, 80.0), (80.0, POS_INF)]
    space = build_state_space(intervals)
    assert space.states == [
        (NEG_INF, 70.0), (70.0, 75.0), (75.0, 80.0), (80.0, POS_INF),
    ]


def test_state_space_always_includes_both_tails() -> None:
    """
    Even for brackets that only tile a middle range, the tails must exist as
    states — otherwise a non-exhaustive set would look exhaustive and a
    directional bet would be reported as a lock.
    """
    space = build_state_space([(70.0, 75.0), (75.0, 80.0)])
    assert space.states[0] == (NEG_INF, 70.0)
    assert space.states[-1] == (80.0, POS_INF)


def test_state_space_refines_overlapping_boundaries() -> None:
    """Union of all boundaries, so every input interval is a union of states."""
    space = build_state_space([(NEG_INF, 80.0), (75.0, POS_INF)])
    assert (75.0, 80.0) in space.states


# ── Payoff matrix ─────────────────────────────────────────────────────────────

def test_payoff_matrix_for_a_clean_partition() -> None:
    intervals = [(NEG_INF, 70.0), (70.0, 75.0), (75.0, POS_INF)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    assert np.array_equal(matrix, np.eye(3))


def test_payoff_matrix_for_a_ladder_is_nested() -> None:
    """Each rung of an "at least K" ladder covers a superset of the next."""
    intervals = [(80.0, POS_INF), (85.0, POS_INF), (90.0, POS_INF)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    # States: <80, [80,85), [85,90), >=90
    assert np.array_equal(matrix, np.array([
        [0, 1, 1, 1],
        [0, 0, 1, 1],
        [0, 0, 0, 1],
    ], dtype=float))


# ── Validation ────────────────────────────────────────────────────────────────

def test_validation_accepts_a_clean_bracket_set() -> None:
    intervals = [(NEG_INF, 70.0), (70.0, 75.0), (75.0, POS_INF)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    assert validate_group(intervals, space, matrix, "bracket").ok


def test_validation_rejects_a_gap_in_coverage() -> None:
    """A missing bracket means "buy them all" is not guaranteed to pay $1."""
    intervals = [(NEG_INF, 70.0), (75.0, POS_INF)]  # nothing covers [70, 75)
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    result = validate_group(intervals, space, matrix, "bracket")
    assert not result.ok
    assert "no contract" in result.reason


def test_validation_rejects_overlapping_brackets() -> None:
    intervals = [(NEG_INF, 80.0), (70.0, POS_INF)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    result = validate_group(intervals, space, matrix, "bracket")
    assert not result.ok
    assert "multiple contracts" in result.reason


def test_validation_accepts_a_nested_ladder() -> None:
    """Ladders are nested, not exhaustive — the bracket rules do not apply."""
    intervals = [(80.0, POS_INF), (85.0, POS_INF), (90.0, POS_INF)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    assert validate_group(intervals, space, matrix, "ladder").ok


def test_validation_rejects_duplicate_payoffs() -> None:
    intervals = [(70.0, 75.0), (70.0, 75.0)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    result = validate_group(intervals, space, matrix, "ladder")
    assert not result.ok
    assert "identical payoffs" in result.reason


def test_validation_rejects_single_contract_groups() -> None:
    intervals = [(70.0, 75.0)]
    space = build_state_space(intervals)
    matrix = build_payoff_matrix(intervals, space)
    assert not validate_group(intervals, space, matrix, "bracket").ok


# ── Shape inference ───────────────────────────────────────────────────────────

def test_shape_inference() -> None:
    assert infer_shape([(NEG_INF, 70.0), (70.0, POS_INF)]) == "binary"
    assert infer_shape([(80.0, POS_INF), (85.0, POS_INF)]) == "ladder"
    assert infer_shape(
        [(NEG_INF, 70.0), (70.0, 75.0), (75.0, POS_INF)]
    ) == "bracket"


# ── Group assembly ────────────────────────────────────────────────────────────

def _market(ticker: str, floor=None, cap=None, strike_type="between") -> dict:
    return {
        "ticker": ticker, "event_ticker": "EVT", "strike_type": strike_type,
        "floor_strike": floor, "cap_strike": cap,
    }


def _book(bid=0.30, ask=0.35, size=10) -> dict:
    return {"bid": bid, "ask": ask, "bid_size": size, "ask_size": size}


def test_build_group_assembles_a_valid_bracket_set() -> None:
    markets = [
        _market("A", cap=70, strike_type="less"),
        _market("B", floor=70, cap=75),
        _market("C", floor=75, strike_type="greater"),
    ]
    contracts = [contract_from_market(m, _book(), NOW) for m in markets]
    group = build_group("EVT", "SER", "EVT", contracts)

    assert group is not None
    assert group.shape == "bracket"
    assert group.state_space.n == 3
    assert np.array_equal(group.payoff, np.eye(3))


def test_build_group_returns_none_on_a_gap() -> None:
    """A group that fails validation is skipped, never silently repaired."""
    markets = [
        _market("A", cap=70, strike_type="less"),
        _market("C", floor=75, strike_type="greater"),
    ]
    contracts = [contract_from_market(m, _book(), NOW) for m in markets]
    assert build_group("EVT", "SER", "EVT", contracts) is None


def test_build_group_returns_none_without_parseable_intervals() -> None:
    markets = [
        {"ticker": "A", "event_ticker": "EVT", "strike_type": "structured"},
        {"ticker": "B", "event_ticker": "EVT", "strike_type": "structured"},
    ]
    contracts = [contract_from_market(m, _book(), NOW) for m in markets]
    assert build_group("EVT", "SER", "EVT", contracts) is None


def test_markets_bucket_by_event() -> None:
    markets = [
        {"ticker": "A", "event_ticker": "E1"},
        {"ticker": "B", "event_ticker": "E1"},
        {"ticker": "C", "event_ticker": "E2"},
        {"ticker": "D"},  # no event: dropped
    ]
    buckets = group_markets_by_event(markets)
    assert set(buckets) == {"E1", "E2"}
    assert len(buckets["E1"]) == 2
