import pytest
from arbengine.value.implied import (
    american_to_decimal,
    american_to_implied,
    decimal_to_implied,
    kalshi_cents_to_implied,
    kalshi_dollars_to_implied,
)


@pytest.mark.parametrize("odds,expected", [
    (-110, 110 / 210),   # risk $110 to win $100 → 110/210
    (+150, 100 / 250),   # risk $100 to win $150 → 100/250
    (-150, 150 / 250),
    (+100, 0.5),
    (-100, 0.5),
    (+200, 100 / 300),
    (-200, 200 / 300),
    (+110, 100 / 210),   # risk $100 to win $110 → 100/210
])
def test_american_to_implied(odds, expected):
    assert american_to_implied(odds) == pytest.approx(expected, rel=1e-6)


def test_american_to_implied_favorite_less_than_half():
    assert american_to_implied(-200) > 0.5


def test_american_to_implied_underdog_less_than_half():
    assert american_to_implied(+200) < 0.5


def test_decimal_to_implied():
    assert decimal_to_implied(2.0) == pytest.approx(0.5)
    assert decimal_to_implied(1.5) == pytest.approx(1 / 1.5)
    assert decimal_to_implied(4.0) == pytest.approx(0.25)


def test_american_to_decimal_positive():
    assert american_to_decimal(+200) == pytest.approx(3.0)
    assert american_to_decimal(+100) == pytest.approx(2.0)


def test_american_to_decimal_negative():
    assert american_to_decimal(-200) == pytest.approx(1.5)
    assert american_to_decimal(-100) == pytest.approx(2.0)


@pytest.mark.parametrize("cents,expected", [
    (63, 0.63),
    (50, 0.50),
    (0, 0.0),
    (100, 1.0),
    (1, 0.01),
])
def test_kalshi_cents_to_implied(cents, expected):
    assert kalshi_cents_to_implied(cents) == pytest.approx(expected)


@pytest.mark.parametrize("dollars,expected", [
    (0.63, 0.63),
    (0.50, 0.50),
    (0.0, 0.0),
    (1.0, 1.0),
])
def test_kalshi_dollars_to_implied(dollars, expected):
    assert kalshi_dollars_to_implied(dollars) == pytest.approx(expected)


def test_cents_dollars_consistency():
    for c in [10, 25, 50, 63, 75, 99]:
        assert kalshi_cents_to_implied(c) == pytest.approx(kalshi_dollars_to_implied(c / 100))
