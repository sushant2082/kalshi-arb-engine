import math

import pytest

from arbengine.fees import buy_cost, fee_per_contract, sell_proceeds


@pytest.mark.parametrize(
    "price, expected",
    [
        # Single-contract column of the official fee schedule (2026-07-07).
        (0.50, 0.02),
        (0.20, 0.02),
        (0.90, 0.01),
        (0.00, 0.00),   # no fee at the boundaries
        (1.00, 0.00),
    ],
)
def test_known_fee_values(price: float, expected: float) -> None:
    assert fee_per_contract(price) == pytest.approx(expected, abs=1e-9)


def test_fee_is_symmetric_about_one_half() -> None:
    for p in (0.1, 0.25, 0.4, 0.45):
        assert fee_per_contract(p) == fee_per_contract(1 - p)


def test_fee_peaks_at_one_half() -> None:
    peak = fee_per_contract(0.5)
    for p in (0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9):
        assert fee_per_contract(p) <= peak


def test_fee_lands_on_the_configured_granularity() -> None:
    from arbengine.fees import FEE_ROUNDING

    for i in range(0, 101):
        fee = fee_per_contract(i / 100)
        steps = fee / FEE_ROUNDING
        assert math.isclose(steps, round(steps), abs_tol=1e-6)


OFFICIAL_100_CONTRACT_FEES = {
    0.01: 0.07, 0.05: 0.34, 0.10: 0.63, 0.15: 0.90, 0.20: 1.12,
    0.25: 1.32, 0.30: 1.47, 0.35: 1.60, 0.40: 1.68, 0.45: 1.74,
    0.50: 1.75, 0.55: 1.74, 0.60: 1.68, 0.65: 1.60, 0.70: 1.47,
    0.75: 1.32, 0.80: 1.12, 0.85: 0.90, 0.90: 0.63, 0.95: 0.34,
    0.99: 0.07,
}


@pytest.mark.parametrize("price, expected", sorted(OFFICIAL_100_CONTRACT_FEES.items()))
def test_matches_official_fee_schedule_table(price: float, expected: float) -> None:
    """
    Every row of the published 100-contract fee table, effective 2026-07-07.
    This is the ground truth the whole engine's profitability math rests on.
    """
    from arbengine.fees import order_fee

    assert order_fee(price, 100) == pytest.approx(expected, abs=1e-9)


def test_maker_fee_is_a_quarter_of_taker() -> None:
    """
    Maker is 0.0175 vs taker 0.07. Recorded because it bounds what a resting
    strategy would pay — this engine models taker fills, which is the
    expensive side, since an arbitrage leg has to execute immediately.
    """
    from arbengine.fees import MAKER_FEE_MULTIPLIER, linear_fee_rate

    # Compare the unrounded rates: rounding to the cent breaks the exact
    # ratio at any particular size.
    taker = linear_fee_rate(0.5, 0.07)
    maker = linear_fee_rate(0.5, MAKER_FEE_MULTIPLIER)
    assert maker == pytest.approx(taker / 4, rel=1e-9)
    assert MAKER_FEE_MULTIPLIER == 0.0175


def test_rounding_is_always_upward() -> None:
    from arbengine.fees import order_fee

    for price in (0.005, 0.13, 0.5, 0.87):
        for qty in (1, 3, 17, 250):
            raw = 0.07 * qty * price * (1 - price)
            assert order_fee(price, qty) >= raw - 1e-12


def test_multiplier_scales_fee() -> None:
    assert fee_per_contract(0.5, 0.0) == 0.0
    assert fee_per_contract(0.5, 0.14) > fee_per_contract(0.5, 0.07)


def test_buy_cost_and_sell_proceeds_straddle_the_quote() -> None:
    # Fees always work against you on both sides.
    assert buy_cost(0.60) > 0.60
    assert sell_proceeds(0.60) < 0.60


def test_price_outside_unit_interval_rejected() -> None:
    with pytest.raises(ValueError):
        fee_per_contract(1.5)
    with pytest.raises(ValueError):
        fee_per_contract(-0.1)


# ── Per-order rounding ────────────────────────────────────────────────────────

def test_fee_rounds_once_per_order_not_per_contract() -> None:
    """
    Kalshi rounds the whole order up, once — per its fee-rounding docs, the
    accumulator is maintained per order across fills.

    Scaling a rounded single-contract fee always overcharges, because it pays
    the rounding penalty once per contract instead of once per order.
    """
    from arbengine.fees import order_fee

    # 100 at $0.005: 0.07 * 100 * 0.005 * 0.995 = $0.034825 -> $0.04
    assert order_fee(0.005, 100) == pytest.approx(0.04, abs=1e-9)
    # Per-contract scaling pays the rounding penalty 100 times over.
    assert fee_per_contract(0.005) * 100 == pytest.approx(1.00)


def test_rounding_granularity_drives_the_size_of_the_old_bug() -> None:
    """
    Pins the interaction between the two fee errors, because the magnitude of
    the first depends entirely on the second.

    Rounding per contract is wrong at any granularity, but at cent granularity
    it is catastrophic: a $0.005 contract owes $0.0003 and rounds to a full
    cent standalone, so scaling it charged 25x. Sub-penny legs are most of a
    wide bracket set, which is why it suppressed detection outright.
    """
    from arbengine.fees import order_fee

    cent_scaled = order_fee(0.005, 1, rounding=0.01) * 100
    assert cent_scaled == pytest.approx(1.00)
    assert cent_scaled / order_fee(0.005, 100, rounding=0.01) == pytest.approx(25.0)

    # At centicent granularity the same mistake is only ~1.1x.
    cc_scaled = order_fee(0.005, 1, rounding=0.0001) * 100
    assert 1.0 < cc_scaled / order_fee(0.005, 100, rounding=0.0001) < 1.5


def test_order_fee_of_one_matches_fee_per_contract() -> None:
    from arbengine.fees import order_fee

    for p in (0.01, 0.2, 0.5, 0.9):
        assert order_fee(p, 1) == fee_per_contract(p)


def test_order_fee_is_monotone_in_quantity() -> None:
    from arbengine.fees import order_fee

    prev = 0.0
    for c in range(1, 50):
        fee = order_fee(0.3, c)
        assert fee >= prev
        prev = fee


def test_zero_contracts_costs_nothing() -> None:
    from arbengine.fees import order_fee

    assert order_fee(0.5, 0) == 0.0


def test_linear_rate_never_understates_the_real_fee() -> None:
    """
    The LP charges the unrounded rate plus one cent of headroom per leg. That
    bound must hold everywhere, or the LP could report a profit that fees erase.
    """
    from arbengine.fees import ROUNDING_HEADROOM, linear_fee_rate, order_fee

    for price in (0.005, 0.01, 0.05, 0.25, 0.5, 0.75, 0.99):
        for qty in (1, 7, 50, 500, 5000):
            modelled = linear_fee_rate(price) * qty + ROUNDING_HEADROOM
            assert modelled >= order_fee(price, qty) - 1e-12, (
                f"linear model understates fee at P={price}, C={qty}"
            )


def test_linear_rate_is_the_unrounded_formula() -> None:
    from arbengine.fees import linear_fee_rate

    assert linear_fee_rate(0.5, 0.07) == pytest.approx(0.07 * 0.25)
    assert linear_fee_rate(0.005, 0.07) == pytest.approx(0.07 * 0.005 * 0.995)


def test_negative_quantity_rejected() -> None:
    from arbengine.fees import order_fee

    with pytest.raises(ValueError):
        order_fee(0.5, -1)
