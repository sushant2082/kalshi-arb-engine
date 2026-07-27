import math

import pytest

from arbengine.fees import buy_cost, fee_per_contract, sell_proceeds


@pytest.mark.parametrize(
    "price, expected",
    [
        # fee(P) = ceil(0.07 * P * (1-P) * 100) / 100
        (0.50, 0.02),   # 0.07*0.25*100 = 1.75 → ceil 2 → $0.02
        (0.20, 0.02),   # 0.07*0.16*100 = 1.12 → ceil 2 → $0.02
        (0.90, 0.01),   # 0.07*0.09*100 = 0.63 → ceil 1 → $0.01
        (0.00, 0.00),   # no fee at the boundaries
        (1.00, 0.00),
    ],
)
def test_known_fee_values(price: float, expected: float) -> None:
    assert fee_per_contract(price) == pytest.approx(expected)


def test_fee_is_symmetric_about_one_half() -> None:
    for p in (0.1, 0.25, 0.4, 0.45):
        assert fee_per_contract(p) == fee_per_contract(1 - p)


def test_fee_peaks_at_one_half() -> None:
    peak = fee_per_contract(0.5)
    for p in (0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9):
        assert fee_per_contract(p) <= peak


def test_fee_always_rounds_up_to_whole_cents() -> None:
    for i in range(0, 101):
        fee = fee_per_contract(i / 100)
        assert math.isclose(fee * 100, round(fee * 100), abs_tol=1e-9)


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
