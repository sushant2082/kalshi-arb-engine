"""
Cross-venue (Kalshi x Polymarket) opportunity detection.

READ THIS BEFORE TRUSTING ANYTHING THIS MODULE REPORTS.

Intra-Kalshi arbitrage is risk-free in the strict sense: one exchange, one
rulebook, one settlement source. If the quoted prices contradict each other,
the money is there.

Cross-venue is NOT that, and no amount of price analysis can make it that.
Buying YES on Kalshi and NO on Polymarket for less than $1 only locks a profit
if BOTH VENUES RESOLVE THE SAME WAY. They are separate legal entities with
separate rulebooks and separate oracles — Kalshi settles against a published
CFTC-regulated rulebook, Polymarket against the UMA optimistic oracle with a
dispute window. When they diverge, the position does not net to zero: both
legs can lose. That is resolution basis risk, it is invisible in the order
book, and it is the dominant risk in this strategy.

So this module's real job is not finding price gaps. It is refusing to call a
price gap an arbitrage until the resolution criteria have been checked, and
attaching an explicit, honest risk tier to every one it does surface.
"""

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

log = logging.getLogger(__name__)


class ResolutionRisk(str, Enum):
    """
    How likely the two venues are to resolve the same question differently.

    Ordered from safest to least safe. The haircut applied to a reported profit
    scales with this, because a 2-cent gross edge is meaningless if there is a
    1% chance of a $1 divergence loss.
    """

    # Same objective, timestamped, numeric criterion against a public reference
    # (an asset price at an instant, an index close). Little room to disagree.
    MECHANICAL = "mechanical"
    # Same underlying event, criteria worded differently but materially aligned.
    ALIGNED = "aligned"
    # Same topic, but the venues handle edge cases differently (ties, voids,
    # postponements, revisions to official data).
    DIVERGENT = "divergent"
    # Could not establish that the two questions are the same at all.
    UNKNOWN = "unknown"


# Expected loss haircut per risk tier, as a fraction of the $1 notional per set.
# A divergence costs roughly the full $1 of the hedge, so these are direct
# probability estimates of a split resolution. They are judgement calls, not
# measurements — override via config once real settlement data accumulates.
DEFAULT_HAIRCUTS: dict[ResolutionRisk, float] = {
    ResolutionRisk.MECHANICAL: 0.002,
    ResolutionRisk.ALIGNED: 0.01,
    ResolutionRisk.DIVERGENT: 0.05,
    ResolutionRisk.UNKNOWN: 1.0,  # never tradeable; surfaces for inspection only
}


@dataclass
class VenueQuote:
    """One side of a cross-venue pair."""

    venue: Literal["kalshi", "polymarket"]
    ticker: str
    # Price to BUY the YES side, and to buy the NO side, both in dollars.
    yes_ask: float | None
    no_ask: float | None
    yes_ask_size: int
    no_ask_size: int
    fee_yes: float = 0.0  # per-share fee at yes_ask
    fee_no: float = 0.0   # per-share fee at no_ask
    fetched_at: datetime | None = None


@dataclass
class MatchedPair:
    """A Kalshi market matched to a Polymarket market, with a risk assessment."""

    kalshi: VenueQuote
    polymarket: VenueQuote
    risk: ResolutionRisk
    rationale: str
    # What the two questions were understood to be asking.
    subject: str = ""
    kalshi_rule: str = ""
    polymarket_rule: str = ""
    # Which venue may supply the YES leg: "kalshi", "polymarket", or "both".
    # None means unconstrained (equal strikes or not yet computed).
    safe_yes_venue: str | None = None
    strike_gap: float = 0.0


@dataclass
class CrossVenueOpportunity:
    """
    A cross-venue price gap, with the resolution risk stated up front.

    `gross_profit_per_set` is what the prices imply. `net_profit_per_set` is
    after the risk haircut. Only the net number should ever drive a decision,
    and even then it is an estimate of expected value, not a guaranteed lock.
    """

    pair: MatchedPair
    # Which venue supplies the YES leg; the other supplies NO.
    yes_venue: Literal["kalshi", "polymarket"]
    cost_per_set: float
    fee_per_set: float
    gross_profit_per_set: float
    haircut_per_set: float
    net_profit_per_set: float
    fillable_sets: int
    first_seen: datetime
    last_seen: datetime

    @property
    def risk(self) -> ResolutionRisk:
        return self.pair.risk

    @property
    def is_riskless(self) -> bool:
        """
        Always False. Cross-venue positions are never risk-free, whatever the
        price gap says. Kept explicit so no caller can mistake one of these for
        an intra-exchange lock.
        """
        return False

    @property
    def total_net_profit(self) -> float:
        return self.net_profit_per_set * self.fillable_sets


# ── Subject extraction ────────────────────────────────────────────────────────

_ASSET_ALIASES = {
    "btc": "BTC", "bitcoin": "BTC",
    "eth": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solana": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "doge": "DOGE", "dogecoin": "DOGE",
    "bnb": "BNB",
    "nasdaq": "NASDAQ", "s&p": "SPX", "sp500": "SPX", "spx": "SPX",
}

_THRESHOLD = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k\b)?", re.IGNORECASE)


@dataclass
class Subject:
    """A normalized "what is this market about" descriptor."""

    asset: str | None = None
    threshold: float | None = None
    direction: Literal["above", "below"] | None = None
    deadline: datetime | None = None

    def comparable(self, other: "Subject") -> bool:
        return (
            self.asset is not None
            and self.asset == other.asset
            and self.direction is not None
            and self.direction == other.direction
        )


def extract_asset(text: str) -> str | None:
    low = text.lower()
    for alias, canonical in _ASSET_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return canonical
    return None


def extract_direction(text: str) -> str | None:
    low = text.lower()
    if re.search(r"\b(above|over|greater than|at least|higher than|\bup\b)\b", low):
        return "above"
    if re.search(r"\b(below|under|less than|at most|lower than|\bdown\b)\b", low):
        return "below"
    return None


def extract_threshold(text: str) -> float | None:
    """
    Pull the numeric strike out of question text.

    Deliberately returns None on ambiguity. A wrong threshold silently pairs
    two different markets, and a "$110,000 or above" contract hedged against a
    "$105,000 or above" contract is not a hedge at all — it is a spread trade
    with a hole in the middle.
    """
    matches = _THRESHOLD.findall(text.replace(",", ""))
    values: list[float] = []
    for raw, kilo in matches:
        try:
            v = float(raw)
        except ValueError:
            continue
        if kilo:
            v *= 1000
        # Ignore small integers that are almost certainly dates or counts.
        if v >= 100:
            values.append(v)
    if len(values) != 1:
        return None
    return values[0]


def subject_from_text(text: str, deadline: datetime | None = None) -> Subject:
    return Subject(
        asset=extract_asset(text),
        threshold=extract_threshold(text),
        direction=extract_direction(text),
        deadline=deadline,
    )


# ── Risk assessment ───────────────────────────────────────────────────────────

# Wording that signals the venues may treat an edge case differently.
_DIVERGENCE_FLAGS = (
    "postpone", "suspend", "cancel", "void", "tie", "draw", "abandon",
    "revised", "revision", "preliminary", "final settlement", "dispute",
    "if the event does not occur", "at the discretion",
)

# Absolute deadline gap always tolerated, regardless of horizon.
DEADLINE_TOLERANCE = timedelta(minutes=5)

# Beyond that, what matters is the gap RELATIVE to time remaining, not the gap
# itself. Kalshi settles crypto at 18:00/21:00 UTC and Polymarket at
# 16:00/17:00 — they never coincide, so an absolute rule matches nothing. But a
# two-hour gap means completely different things at different horizons: on an
# hourly contract it is the entire life of the market and the underlying can
# move percent in between, while on a six-month contract it is noise.
#
# So the gap is judged as a fraction of time-to-settlement. This is genuine
# timing basis risk, not a free pass — it is why a pair that only clears this
# test on relative grounds is capped at ALIGNED rather than MECHANICAL.
MAX_RELATIVE_DEADLINE_GAP = 0.01

# Below this horizon, only the absolute tolerance applies: a short-dated
# contract has no room for the relative rule to be meaningful.
MIN_HORIZON_FOR_RELATIVE = timedelta(days=2)


def assess_risk(
    kalshi_text: str,
    polymarket_text: str,
    kalshi_subject: Subject,
    polymarket_subject: Subject,
    now: datetime | None = None,
) -> tuple[ResolutionRisk, str]:
    """
    Classify how safely two markets can be treated as the same question.

    Conservative by construction: anything that cannot be positively
    established as equivalent lands in UNKNOWN, which is never tradeable. The
    failure that costs money is calling two different questions the same, so
    the default has to be refusal.
    """
    if not kalshi_subject.comparable(polymarket_subject):
        return (
            ResolutionRisk.UNKNOWN,
            "could not establish the same asset and direction in both questions",
        )

    if kalshi_subject.threshold is None or polymarket_subject.threshold is None:
        return (
            ResolutionRisk.UNKNOWN,
            "could not extract an unambiguous threshold from both questions",
        )

    # Strikes need not be equal — see safe_orientation. In the correct
    # orientation a strike gap is upside, not risk. Equality is only required
    # for BOTH orientations to be legal.

    kd, pd = kalshi_subject.deadline, polymarket_subject.deadline
    if kd is None or pd is None:
        return (
            ResolutionRisk.UNKNOWN,
            "missing a settlement deadline on one or both venues",
        )
    gap = abs(kd - pd)
    timing_capped = False
    if gap > DEADLINE_TOLERANCE:
        horizon = min(kd, pd) - now if now else None
        if horizon is None or horizon < MIN_HORIZON_FOR_RELATIVE:
            return (
                ResolutionRisk.UNKNOWN,
                f"settlement times differ by {gap} on a short-dated contract — "
                "the underlying can move materially in between, so these do "
                "not hedge each other",
            )
        relative = gap.total_seconds() / horizon.total_seconds()
        if relative > MAX_RELATIVE_DEADLINE_GAP:
            return (
                ResolutionRisk.UNKNOWN,
                f"settlement times differ by {gap}, which is "
                f"{relative:.1%} of the remaining horizon — too much to hedge",
            )
        # Tolerated, but it is still real timing basis risk.
        timing_capped = True

    blob = f"{kalshi_text} {polymarket_text}".lower()
    hit = next((f for f in _DIVERGENCE_FLAGS if f in blob), None)
    if hit:
        return (
            ResolutionRisk.DIVERGENT,
            f"resolution text mentions {hit!r}; venues may handle that case "
            "differently",
        )

    if kalshi_subject.asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
        if timing_capped:
            return (
                ResolutionRisk.ALIGNED,
                f"same asset and threshold, but settlement times differ by "
                f"{gap} — small against the horizon, still timing basis risk",
            )
        return (
            ResolutionRisk.MECHANICAL,
            "same asset, threshold and settlement time against a public price "
            "reference; little room for the venues to disagree",
        )

    return (
        ResolutionRisk.ALIGNED,
        "same asset, threshold and settlement time, but the underlying is not "
        "a mechanically-priced reference",
    )


def safe_orientation(
    kalshi_strike: float, polymarket_strike: float, direction: str
) -> Literal["kalshi", "polymarket", "both"] | None:
    """
    Which venue may supply the YES leg so the pair can never pay $0.

    Kalshi and Polymarket almost never quote the same strike — Kalshi uses
    `...99.99` levels, Polymarket round numbers — so requiring equality finds
    nothing. It is not required. With mismatched strikes exactly one
    orientation is still riskless, and the other has a hole in it.

    For "above" contracts, with Kalshi strike Ks and Polymarket strike Ps:

        buy Kalshi YES + Polymarket NO  pays  1{X > Ks} + 1{X <= Ps}

    If Ks <= Ps this is >= 1 everywhere, and pays 2 in the band between the
    strikes — the gap is a bonus, not an exposure. If Ks > Ps the same position
    pays 0 for X in (Ps, Ks]: both legs lose together. So the venue with the
    LOWER strike must supply YES. For "below" contracts the inequality flips
    and the HIGHER strike supplies YES.

    Returns "both" on equal strikes, the safe venue on a gap, or None if the
    direction is not understood.
    """
    if direction == "above":
        if math.isclose(kalshi_strike, polymarket_strike, rel_tol=1e-12):
            return "both"
        return "kalshi" if kalshi_strike < polymarket_strike else "polymarket"
    if direction == "below":
        if math.isclose(kalshi_strike, polymarket_strike, rel_tol=1e-12):
            return "both"
        return "kalshi" if kalshi_strike > polymarket_strike else "polymarket"
    return None


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_cross_venue(
    pair: MatchedPair,
    now: datetime,
    min_net_profit: float = 0.0,
    haircuts: dict[ResolutionRisk, float] | None = None,
    max_quote_age_sec: float = 30.0,
) -> CrossVenueOpportunity | None:
    """
    Look for a lock across the two venues: buy YES on one and NO on the other.

    If the two contracts resolve identically, exactly one of the legs pays $1,
    so an all-in cost below $1 is a profit. Every word of that sentence depends
    on the "if", which is what the risk tier and haircut are for.
    """
    haircuts = haircuts or DEFAULT_HAIRCUTS

    if pair.risk is ResolutionRisk.UNKNOWN:
        return None

    # Stale quotes across two independent venues are worse than on one: the
    # feeds are uncorrelated, so a skew silently compares two different moments.
    for q in (pair.kalshi, pair.polymarket):
        if q.fetched_at is None:
            return None
        if (now - q.fetched_at).total_seconds() > max_quote_age_sec:
            return None

    best: CrossVenueOpportunity | None = None

    # Only orientations that cannot pay $0 are considered. With mismatched
    # strikes one of the two is a trap that loses both legs together.
    allowed = pair.safe_yes_venue or "both"
    orientations = [
        o for o in (
            ("kalshi", pair.kalshi, pair.polymarket),
            ("polymarket", pair.polymarket, pair.kalshi),
        )
        if allowed == "both" or o[0] == allowed
    ]

    for yes_venue, yes_q, no_q in orientations:
        if yes_q.yes_ask is None or no_q.no_ask is None:
            continue
        sets = min(yes_q.yes_ask_size, no_q.no_ask_size)
        if sets <= 0:
            continue

        cost = yes_q.yes_ask + no_q.no_ask
        fee = yes_q.fee_yes + no_q.fee_no
        gross = 1.0 - cost - fee
        haircut = haircuts.get(pair.risk, 1.0)
        net = gross - haircut

        if net <= min_net_profit:
            continue

        candidate = CrossVenueOpportunity(
            pair=pair,
            yes_venue=yes_venue,
            cost_per_set=cost,
            fee_per_set=fee,
            gross_profit_per_set=gross,
            haircut_per_set=haircut,
            net_profit_per_set=net,
            fillable_sets=sets,
            first_seen=now,
            last_seen=now,
        )
        if best is None or candidate.net_profit_per_set > best.net_profit_per_set:
            best = candidate

    return best


def match_key(opp: CrossVenueOpportunity) -> str:
    """Stable identity for persistence tracking across scans."""
    return (
        f"xvenue|{opp.pair.kalshi.ticker}|{opp.pair.polymarket.ticker}"
        f"|{opp.yes_venue}"
    )
