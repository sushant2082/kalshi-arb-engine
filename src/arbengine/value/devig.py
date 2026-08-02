import math
from typing import Callable

from scipy.optimize import brentq

_TOLERANCE = 1e-6


def _check_sum(probs: list[float]) -> list[float]:
    total = sum(probs)
    if abs(total - 1.0) > _TOLERANCE:
        raise ValueError(f"Devigged probabilities sum to {total}, expected 1.0")
    return probs


def proportional(qs: list[float]) -> list[float]:
    """Proportional (multiplicative) devig: divide each quote by the overround."""
    if len(qs) < 2:
        raise ValueError("Need at least 2 outcomes")
    total = sum(qs)
    if total <= 0:
        raise ValueError(f"Overround must be positive, got {total}")
    return _check_sum([q / total for q in qs])


def power(qs: list[float]) -> list[float]:
    """Power devig: find exponent k > 1 such that sum(q**k) == 1."""
    if len(qs) < 2:
        raise ValueError("Need at least 2 outcomes")

    def _objective(k: float) -> float:
        return sum(q**k for q in qs) - 1.0

    # k=1 gives the raw sum (overround > 1), k large enough drives sum to 0
    if _objective(1.0) <= 0:
        raise ValueError("Overround must be > 1 for power devig")

    k = brentq(_objective, 1.0 + 1e-9, 100.0, xtol=1e-10)
    return _check_sum([q**k for q in qs])


def shin(qs: list[float]) -> list[float]:
    """Shin (2-outcome version): solve for insider fraction z in (0,1)."""
    if len(qs) < 2:
        raise ValueError("Need at least 2 outcomes")

    overround = sum(qs)
    if overround <= 1.0:
        raise ValueError("Overround must be > 1 for Shin devig")

    def _shin_prob(z: float, q: float) -> float:
        discriminant = z**2 + 4 * (1 - z) * q**2 / overround
        return (math.sqrt(discriminant) - z) / (2 * (1 - z))

    def _objective(z: float) -> float:
        return sum(_shin_prob(z, q) for q in qs) - 1.0

    # z=0 reduces to proportional (sum == 1), z→1 drives probabilities differently;
    # search in a small positive interval above zero
    z = brentq(_objective, 1e-9, 1.0 - 1e-9, xtol=1e-10)
    return _check_sum([_shin_prob(z, q) for q in qs])


def devig(qs: list[float], method: str) -> list[float]:
    """Dispatch to the named devig method."""
    methods: dict[str, Callable[[list[float]], list[float]]] = {
        "proportional": proportional,
        "power": power,
        "shin": shin,
    }
    if method not in methods:
        raise ValueError(f"Unknown devig method: {method!r}. Choose from {list(methods)}")
    return methods[method](qs)
