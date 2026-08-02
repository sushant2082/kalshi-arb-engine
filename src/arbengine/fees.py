import math

DEFAULT_FEE_MULTIPLIER = 0.07

# VERIFIED against the Kalshi Fee Schedule effective 2026-07-07:
#
#     taker: fees = round_up(M x 0.07   x C x P x (1-P))
#     maker: fees = round_up(M x 0.0175 x C x P x (1-P))
#
# M is a per-series multiplier, default 1 for takers and 0 for makers. Series
# listed with M=0 (KXBTCY, KXETHY, KXDOED, ...) are genuinely fee-free, which
# the /series API also reports as fee_multiplier.
MAKER_FEE_MULTIPLIER = 0.0175

# Granularity the per-order fee is rounded UP to.
#
# The sources disagree and the difference is worth stating plainly:
#
#   - The schedule's formula and docs.kalshi.com/getting_started/fee_rounding
#     both say the rounding is to a centicent ($0.0001).
#   - The schedule's own published fee TABLE matches cent rounding exactly on
#     all 21 rows, and does not match centicent rounding on 10 of them (e.g.
#     100 @ $0.05 is listed at $0.34 where the raw fee is $0.3325).
#
# Defaulting to the cent is the conservative reading: it can only overstate
# fees. Understating them is the one error that turns a loss into a reported
# arbitrage, and this engine exists to not do that. Set FEE_ROUNDING=0.0001 to
# take the formula's reading once the discrepancy is settled — it makes
# marginal opportunities slightly easier to clear.
FEE_ROUNDING = 0.01


def order_fee(
    price: float,
    contracts: int,
    multiplier: float = DEFAULT_FEE_MULTIPLIER,
    rounding: float = FEE_ROUNDING,
) -> float:
    """
    Kalshi taker fee for an ORDER of `contracts` at `price`:

        fee = roundup(multiplier * C * P * (1 - P), to=`rounding`)

    One rounding for the whole order — confirmed against Kalshi's fee-rounding
    docs, which state the accumulator is maintained per order across fills.
    This is the real charge, and the only number that should ever be compared
    against a profit.

    `multiplier` is still UNVERIFIED against the current fee schedule and is
    the single most load-bearing constant in this engine: every profit
    threshold scales with it.
    """
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0, 1] dollars, got {price}")
    if contracts < 0:
        raise ValueError(f"contracts must be non-negative, got {contracts}")
    if contracts == 0:
        return 0.0
    raw = multiplier * contracts * price * (1.0 - price)
    steps = math.ceil(raw / rounding - 1e-9)
    return steps * rounding


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
