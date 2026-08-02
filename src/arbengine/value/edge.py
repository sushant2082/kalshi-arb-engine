"""
Sharp-line value betting against Kalshi prices.

THIS IS NOT ARBITRAGE. Everything else in this engine identifies positions that
cannot lose. This one identifies positions that are merely *favourably priced*
if a sharp sportsbook's devigged line is a better probability estimate than
Kalshi's. Every position here can lose the full stake, and most of the ones
that are correctly identified still lose roughly as often as the fair
probability says they will.

That distinction is structural, not a disclaimer, so it is enforced in the
types: a ValueOpportunity has no `guaranteed_profit`, only an `expected_value`,
and it is stored and reported separately so it can never be blended into the
arbitrage P&L.

What makes it plausibly positive-EV: Pinnacle operates on low margin and high
limits, and takes sharp money rather than restricting it, so its devigged line
is a strong probability estimate. When Kalshi's price disagrees materially, the
sharp line is more often right. "More often" is the whole claim — this needs
many bets to express itself and will have long losing runs.

Three things reliably turn an apparent edge into a real loss, all handled here:

  1. Fees. Kalshi charges ~1.75c per contract at mid prices. An edge under that
     is not an edge.
  2. Staleness. A sharp line and a Kalshi price captured minutes apart are not
     comparable; the market moved in between.
  3. Rule mismatch. Sportsbook and Kalshi settlement rules differ on edge cases
     (suspended games, extra innings, retirements). Those diverge exactly when
     an apparent edge looks largest.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from arbengine.fees import order_fee
from arbengine.value.devig import devig

log = logging.getLogger(__name__)


@dataclass
class SharpQuote:
    """A two-way sharp line for one event."""

    book: str
    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    home_implied: float  # raw implied probability, still containing vig
    away_implied: float
    fetched_at: datetime

    @property
    def overround(self) -> float:
        return self.home_implied + self.away_implied


@dataclass
class KalshiSide:
    """One side of a Kalshi game market."""

    ticker: str
    team: str
    ask: float
    ask_size: int
    fetched_at: datetime


@dataclass
class ValueOpportunity:
    """
    A Kalshi price that looks cheap against a sharp devigged line.

    Deliberately has no "guaranteed" anything. `expected_value` is an estimate
    that is only as good as the assumption that the sharp line is the better
    probability — which is a belief about the world, not a property of the
    prices.
    """

    ticker: str
    team: str
    book: str
    event_id: str

    kalshi_ask: float
    fair_prob: float          # devigged sharp probability
    raw_edge: float           # fair_prob - kalshi_ask, before costs
    fee_per_contract: float
    net_edge: float           # after fees; the number that matters
    expected_value: float     # net_edge, per $1 contract

    kelly_fraction: float     # full Kelly
    stake_fraction: float     # after the configured Kelly multiplier
    contracts: int            # sized against bankroll

    max_size: int             # depth available at the ask
    commence_time: datetime
    detected_at: datetime
    quote_skew_sec: float     # how far apart the two quotes were captured

    warnings: list[str] = field(default_factory=list)

    @property
    def is_riskless(self) -> bool:
        """
        Always False, and present so no caller can treat one of these like an
        arbitrage opportunity. This position loses its full stake whenever the
        team loses, which is most of the time on an underdog.
        """
        return False

    @property
    def loses_full_stake_probability(self) -> float:
        """The honest downside: how often this bet simply loses."""
        return 1.0 - self.fair_prob


def kelly_fraction(fair_prob: float, price: float) -> float:
    """
    Full Kelly for a binary contract bought at `price` paying $1.

        f* = (p - price) / (1 - price)

    Returns 0 on a non-positive edge. Full Kelly is famously too aggressive for
    real bankrolls — it maximizes long-run growth but tolerates brutal
    drawdowns, and it assumes `fair_prob` is exactly right, which it is not.
    Callers should apply a fraction of this.
    """
    if price >= 1.0 or price <= 0.0:
        return 0.0
    if fair_prob <= price:
        return 0.0
    return (fair_prob - price) / (1.0 - price)


def fair_probabilities(
    quote: SharpQuote, method: str = "shin"
) -> tuple[float, float]:
    """
    Strip the vig from a sharp two-way line.

    The raw implied probabilities sum above 1 by the book's margin. Devigging
    redistributes that back. Which method is right is genuinely contested:
    proportional is simplest and assumes margin scales with probability, shin
    models informed traders and tends to shade favourites less. On a
    low-margin book the three rarely disagree by more than a few tenths of a
    percent — but on a wide line they diverge, and that is exactly when an
    apparent edge is largest and least trustworthy.
    """
    home, away = devig([quote.home_implied, quote.away_implied], method)
    return home, away


def evaluate(
    side: KalshiSide,
    quote: SharpQuote,
    fair_prob: float,
    bankroll: float,
    fee_multiplier: float = 0.07,
    fee_scale: float = 1.0,
    kelly_multiplier: float = 0.25,
    min_net_edge: float = 0.02,
    max_quote_skew_sec: float = 60.0,
    max_stake_fraction: float = 0.05,
    now: datetime | None = None,
    min_minutes_to_start: float = 5.0,
) -> ValueOpportunity | None:
    """
    Score one Kalshi side against a sharp line.

    Returns None unless the edge survives fees, staleness and sizing. The
    default `min_net_edge` of 2% is deliberately above the ~1.75c fee: an edge
    that only just clears costs is indistinguishable from noise in the devig
    method, and acting on those is how a positive-EV strategy becomes a
    negative-EV one.
    """
    warnings: list[str] = []

    # HARD REJECT: the game must not have started.
    #
    # This is the single most dangerous failure mode in the strategy. Kalshi
    # prices a live game on its current state, while The Odds API serves
    # Pinnacle's pregame line (and notes it may lag). Compare the two after
    # first pitch and a team losing 5-0 looks like a 20-point "edge" — the
    # largest and most confident-looking signals the scanner can produce, all
    # of them just the game having already happened.
    #
    # Measured live: every apparent edge above 9% came from an in-progress
    # game, while genuinely pregame matchups agreed with Pinnacle to under 1%.
    reference = now or side.fetched_at
    if quote.commence_time is not None:
        minutes_out = (quote.commence_time - reference).total_seconds() / 60.0
        if minutes_out < min_minutes_to_start:
            return None

    skew = abs((side.fetched_at - quote.fetched_at).total_seconds())
    if skew > max_quote_skew_sec:
        # Not a warning — a hard reject. Comparing prices from different
        # moments measures the market's movement, not an edge.
        return None

    if side.ask_size <= 0:
        return None

    # Fee at a representative size; the per-order ceiling makes tiny orders
    # disproportionately expensive, which the sizing below has to respect.
    fee = order_fee(side.ask, 100, fee_multiplier * fee_scale) / 100.0

    raw = fair_prob - side.ask
    net = raw - fee
    if net < min_net_edge:
        return None

    full_kelly = kelly_fraction(fair_prob, side.ask + fee)
    stake_fraction = min(full_kelly * kelly_multiplier, max_stake_fraction)
    contracts = int((bankroll * stake_fraction) / side.ask) if side.ask > 0 else 0
    contracts = min(contracts, side.ask_size)

    if contracts <= 0:
        return None

    if quote.overround > 1.10:
        warnings.append(
            f"wide sharp line (overround {quote.overround:.3f}) — devig methods "
            "disagree most here, so the fair probability is least reliable"
        )
    if raw > 0.15:
        warnings.append(
            f"unusually large raw edge ({raw:.1%}) — far more often a stale "
            "line, a bad team match or a settlement-rule difference than a "
            "genuine mispricing"
        )
    if side.ask < 0.10:
        warnings.append(
            "longshot: devig error is proportionally largest at low prices, "
            "and the position loses outright most of the time"
        )

    return ValueOpportunity(
        ticker=side.ticker,
        team=side.team,
        book=quote.book,
        event_id=quote.event_id,
        kalshi_ask=side.ask,
        fair_prob=fair_prob,
        raw_edge=raw,
        fee_per_contract=fee,
        net_edge=net,
        expected_value=net,
        kelly_fraction=full_kelly,
        stake_fraction=stake_fraction,
        contracts=contracts,
        max_size=side.ask_size,
        commence_time=quote.commence_time,
        detected_at=side.fetched_at,
        quote_skew_sec=skew,
        warnings=warnings,
    )


def summarize_expectations(opps: list[ValueOpportunity]) -> dict:
    """
    Aggregate what a set of value bets actually implies.

    Reports the expected loss rate alongside the expected value, because the
    two together are the honest picture: a portfolio of +3% edges on 30%
    favourites still loses most individual bets, and a strategy that looks
    broken after twenty bets may be working exactly as designed.
    """
    if not opps:
        return {"count": 0}

    total_stake = sum(o.contracts * o.kalshi_ask for o in opps)
    total_ev = sum(o.contracts * o.expected_value for o in opps)
    mean_fair = sum(o.fair_prob for o in opps) / len(opps)

    return {
        "count": len(opps),
        "total_stake": total_stake,
        "expected_value": total_ev,
        "ev_pct_of_stake": (total_ev / total_stake) if total_stake else 0.0,
        "mean_fair_prob": mean_fair,
        # The number people forget: how often these lose outright.
        "expected_loss_rate": 1.0 - mean_fair,
        "flagged": sum(1 for o in opps if o.warnings),
    }
