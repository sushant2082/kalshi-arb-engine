import math

DEFAULT_FEE_MULTIPLIER = 0.07


def order_fee(
    price: float, contracts: int, multiplier: float = DEFAULT_FEE_MULTIPLIER
) -> float:
    """
    Kalshi taker fee for an ORDER of `contracts` at `price`:

        fee = ceil(multiplier * C * P * (1 - P) * 100) / 100

    One rounding for the whole order. This is the real charge, and the only
    number that should ever be compared against a profit.

    VERIFY `multiplier` against current Kalshi docs per market category.
    """
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0, 1] dollars, got {price}")
    if contracts < 0:
        raise ValueError(f"contracts must be non-negative, got {contracts}")
    if contracts == 0:
        return 0.0
    raw = multiplier * contracts * price * (1.0 - price) * 100.0
    return math.ceil(raw) / 100.0


def fee_per_contract(price: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """
    Fee for a SINGLE contract — i.e. `order_fee(price, 1)`.

    Do not multiply this by a quantity to price a larger order. The ceiling
    applies once per order, so scaling a rounded-up single-contract fee
    overcharges catastrophically at low prices: a $0.005 contract carries a
    real fee of $0.0004, but rounds to a full cent on its own, which is 25x
    too much at size. That error silently suppressed every detection in these
    bracket sets, whose legs are mostly sub-penny. Use `order_fee` for real
    quantities and `linear_fee_rate` inside the LP.
    """
    return order_fee(price, 1, multiplier)


def linear_fee_rate(price: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """
    The per-contract fee rate with no rounding: `multiplier * P * (1 - P)`.

    The LP needs costs linear in quantity, and the true fee is linear apart
    from a single ceiling per order. Since

        ceil(m*C*P*(1-P)) <= m*C*P*(1-P) + 0.01

    an order's fee is this linear rate times quantity, plus at most one cent.
    So the LP charges this rate per contract and `ROUNDING_HEADROOM` once per
    leg — linear, and never understating the real fee.
    """
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0, 1] dollars, got {price}")
    return multiplier * price * (1.0 - price)


# Worst case the per-order ceiling can add, in dollars. Charged once per leg.
ROUNDING_HEADROOM = 0.01


def buy_cost(ask: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """
    Effective per-contract cost to buy at `ask`, for the LP.

    Uses the unrounded linear rate; the per-order ceiling is charged separately
    as a fixed per-leg amount so it is not multiplied by quantity.
    """
    return ask + linear_fee_rate(ask, multiplier)


def sell_proceeds(bid: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """Effective per-contract proceeds from selling at `bid`, for the LP."""
    return bid - linear_fee_rate(bid, multiplier)
