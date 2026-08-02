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

# Deadlines this far apart make the two contracts different instruments even if
# the question text matches, because the underlying can move in between.
DEADLINE_TOLERANCE = timedelta(minutes=5)


def assess_risk(
    kalshi_text: str,
    polymarket_text: str,
    kalshi_subject: Subject,
    polymarket_subject: Subject,
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

    if not math.isclose(
        kalshi_subject.threshold, polymarket_subject.threshold, rel_tol=1e-9
    ):
        return (
            ResolutionRisk.UNKNOWN,
            f"thresholds differ: {kalshi_subject.threshold} vs "
            f"{polymarket_subject.threshold} — these are different contracts",
        )

    kd, pd = kalshi_subject.deadline, polymarket_subject.deadline
    if kd is None or pd is None:
        return (
            ResolutionRisk.UNKNOWN,
            "missing a settlement deadline on one or both venues",
        )
    if abs(kd - pd) > DEADLINE_TOLERANCE:
        return (
            ResolutionRisk.UNKNOWN,
            f"settlement times differ by {abs(kd - pd)} — the underlying can "
            "move in between, so these do not hedge each other",
        )

    blob = f"{kalshi_text} {polymarket_text}".lower()
    hit = next((f for f in _DIVERGENCE_FLAGS if f in blob), None)
    if hit:
        return (
            ResolutionRisk.DIVERGENT,
            f"resolution text mentions {hit!r}; venues may handle that case "
            "differently",
        )

    if kalshi_subject.asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
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

    for yes_venue, yes_q, no_q in (
        ("kalshi", pair.kalshi, pair.polymarket),
        ("polymarket", pair.polymarket, pair.kalshi),
    ):
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
