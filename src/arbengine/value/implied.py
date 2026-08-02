def american_to_decimal(odds: int) -> float:
    """Convert American moneyline odds to decimal odds."""
    if odds > 0:
        return (odds / 100) + 1.0
    else:
        return (100 / abs(odds)) + 1.0


def decimal_to_implied(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability."""
    return 1.0 / decimal_odds


def american_to_implied(odds: int) -> float:
    """Convert American moneyline odds directly to implied probability."""
    return decimal_to_implied(american_to_decimal(odds))


def kalshi_cents_to_implied(cents: float) -> float:
    """Convert Kalshi YES contract price in cents (0-100) to implied probability."""
    return cents / 100.0


def kalshi_dollars_to_implied(dollars: float) -> float:
    """Convert Kalshi YES contract price in dollars (0-1) to implied probability."""
    return dollars
