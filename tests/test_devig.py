import pytest
from arbengine.value.devig import devig, power, proportional, shin

_TOL = 1e-6


def _sums_to_one(probs):
    assert abs(sum(probs) - 1.0) <= _TOL


# ── proportional ────────────────────────────────────────────────────────────

def test_proportional_symmetric():
    result = proportional([0.55, 0.55])
    assert result == pytest.approx([0.5, 0.5], abs=_TOL)
    _sums_to_one(result)


def test_proportional_asymmetric():
    result = proportional([0.60, 0.50])
    assert result == pytest.approx([6 / 11, 5 / 11], rel=1e-6)
    _sums_to_one(result)


def test_proportional_three_outcomes():
    qs = [q * 1.1 for q in [1 / 3, 1 / 3, 1 / 3]]
    result = proportional(qs)
    assert result == pytest.approx([1 / 3, 1 / 3, 1 / 3], abs=_TOL)
    _sums_to_one(result)


def test_proportional_single_raises():
    with pytest.raises(ValueError):
        proportional([0.55])


def test_proportional_zero_sum_raises():
    with pytest.raises(ValueError):
        proportional([0.0, 0.0])


# ── power ────────────────────────────────────────────────────────────────────

def test_power_symmetric():
    result = power([0.55, 0.55])
    _sums_to_one(result)
    # symmetric input → each side exactly 0.5 (power devig solves 2*0.55^k=1 → 0.55^k=0.5)
    assert result == pytest.approx([0.5, 0.5], abs=_TOL)


def test_power_asymmetric():
    result = power([0.60, 0.50])
    _sums_to_one(result)
    # favourite's fair prob is still larger, but smaller than raw quote
    assert result[0] > result[1]
    assert result[0] < 0.60


def test_power_single_raises():
    with pytest.raises(ValueError):
        power([0.55])


def test_power_overround_le_one_raises():
    with pytest.raises(ValueError):
        power([0.45, 0.50])  # sum = 0.95 < 1


# ── shin ─────────────────────────────────────────────────────────────────────

def test_shin_symmetric():
    result = shin([0.55, 0.55])
    _sums_to_one(result)
    # symmetric input → Shin also converges to 0.5 (same as proportional for equal overround)
    assert result == pytest.approx([0.5, 0.5], abs=1e-10)


def test_shin_asymmetric():
    result = shin([0.70, 0.40])
    _sums_to_one(result)
    assert result[0] > result[1]


def test_shin_single_raises():
    with pytest.raises(ValueError):
        shin([0.55])


def test_shin_overround_le_one_raises():
    with pytest.raises(ValueError):
        shin([0.45, 0.50])


# ── devig dispatcher ─────────────────────────────────────────────────────────

def test_devig_proportional():
    result = devig([0.55, 0.55], "proportional")
    assert result == pytest.approx([0.5, 0.5], abs=_TOL)


def test_devig_power():
    result = devig([0.55, 0.55], "power")
    _sums_to_one(result)


def test_devig_shin():
    result = devig([0.55, 0.55], "shin")
    _sums_to_one(result)


def test_devig_unknown_method_raises():
    with pytest.raises(ValueError, match="Unknown devig method"):
        devig([0.55, 0.55], "magic")
