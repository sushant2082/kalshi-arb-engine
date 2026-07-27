import math

DEFAULT_FEE_MULTIPLIER = 0.07


def fee_per_contract(price: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """
    Kalshi taker fee for one contract at `price` (dollars, in [0, 1]).

        fee(P) = ceil(multiplier * P * (1 - P) * 100) / 100

    Treated as a per-contract constant at the quoted price so the LP stays
    linear. This is slightly conservative versus Kalshi's per-order rounding
    (which rounds once for the whole order, not per contract), so it understates
    profit — the safe direction for an arbitrage check.

    VERIFY `multiplier` against current Kalshi docs per market category.
    """
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0, 1] dollars, got {price}")
    raw = multiplier * price * (1.0 - price) * 100.0
    return math.ceil(raw) / 100.0


def buy_cost(ask: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """Effective all-in cost to buy one contract at `ask`."""
    return ask + fee_per_contract(ask, multiplier)


def sell_proceeds(bid: float, multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """Effective net proceeds from selling one contract at `bid`."""
    return bid - fee_per_contract(bid, multiplier)
