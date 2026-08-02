"""
Pair Kalshi markets with Polymarket markets that ask the same question.

This is the component that decides whether a cross-venue position is a hedge or
two independent bets wearing a trenchcoat. Everything downstream inherits its
mistakes, and a false match is worse than no match: it produces a position that
looks hedged, is sized as if hedged, and loses on both legs when the venues
resolve differently.

So the bias is refusal. A pair is only proposed when the asset, direction,
strike and settlement instant can all be extracted from BOTH venues and agree.
Anything else is reported as unmatched with a reason, which is also how the
coverage gaps become visible instead of silently shrinking the universe.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from arbengine.crossvenue import (
    MatchedPair,
    ResolutionRisk,
    Subject,
    VenueQuote,
    assess_risk,
    extract_asset,
    safe_orientation,
    extract_direction,
    extract_threshold,
)

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A normalized market from either venue, ready to be compared."""

    venue: str
    ticker: str
    text: str
    subject: Subject
    raw: dict = field(default_factory=dict)

    @property
    def matchable(self) -> bool:
        return (
            self.subject.asset is not None
            and self.subject.threshold is not None
            and self.subject.direction is not None
            and self.subject.deadline is not None
        )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Kalshi encodes the strike in the ticker suffix (…-T110000 or …-B97500) and
# the direction in strike_type. Trusting those beats parsing the title, which
# is prose and varies by series.
_KALSHI_STRIKE_SUFFIX = re.compile(r"-[TB](-?\d+(?:\.\d+)?)$")


def kalshi_candidate(market: dict) -> Candidate:
    """Normalize a Kalshi market using structured fields, not prose."""
    ticker = market.get("ticker", "")
    title = " ".join(
        str(market.get(k) or "")
        for k in ("title", "yes_sub_title", "subtitle")
    )

    strike_type = market.get("strike_type")
    direction = None
    threshold = None
    if strike_type in ("greater", "greater_or_equal"):
        direction = "above"
        threshold = market.get("floor_strike")
    elif strike_type in ("less", "less_or_equal"):
        direction = "below"
        threshold = market.get("cap_strike")

    if threshold is None:
        m = _KALSHI_STRIKE_SUFFIX.search(ticker)
        if m:
            try:
                threshold = float(m.group(1))
            except ValueError:
                threshold = None

    # Asset comes from the series prefix, which is stable, with the title as a
    # fallback for series whose prefix is not an asset symbol.
    asset = extract_asset(ticker.split("-")[0]) or extract_asset(title)

    return Candidate(
        venue="kalshi",
        ticker=ticker,
        text=title,
        subject=Subject(
            asset=asset,
            threshold=float(threshold) if threshold is not None else None,
            direction=direction,
            # close_time is when trading stops and the underlying is measured;
            # expected_expiration_time is ~5 minutes later and is just when
            # settlement is processed. The measurement instant is what has to
            # match across venues.
            deadline=_parse_iso(
                market.get("close_time")
                or market.get("expected_expiration_time")
                or market.get("expiration_time")
            ),
        ),
        raw=market,
    )


def polymarket_candidate(market: dict) -> Candidate:
    """
    Normalize a Polymarket market.

    Polymarket has no structured strike field, so everything comes from the
    question text. That is inherently weaker than Kalshi's metadata, which is
    why the risk assessment never rates a text-derived match above ALIGNED
    unless the asset is mechanically priced.
    """
    text = " ".join(
        str(market.get(k) or "") for k in ("question", "description")
    )[:2000]

    return Candidate(
        venue="polymarket",
        ticker=str(market.get("conditionId") or market.get("slug") or ""),
        text=text,
        subject=Subject(
            asset=extract_asset(text),
            threshold=extract_threshold(market.get("question") or ""),
            direction=extract_direction(market.get("question") or ""),
            # endDate carries the settlement TIME; endDateIso is date-only.
            # Preferring the latter silently collapses every deadline to
            # midnight, which makes genuinely simultaneous contracts look days
            # apart and rejects every match.
            deadline=_parse_iso(
                market.get("endDate") or market.get("endDateIso")
            ),
        ),
        raw=market,
    )


@dataclass
class MatchReport:
    """What matched, what didn't, and why — so gaps stay visible."""

    pairs: list[MatchedPair] = field(default_factory=list)
    kalshi_total: int = 0
    polymarket_total: int = 0
    kalshi_matchable: int = 0
    polymarket_matchable: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        key = reason.split(":")[0].split("—")[0].strip()[:60]
        self.rejected[key] = self.rejected.get(key, 0) + 1


def match(
    kalshi_markets: list[dict],
    polymarket_markets: list[dict],
    kalshi_quotes: dict[str, VenueQuote],
    polymarket_quotes: dict[str, VenueQuote],
    max_pairs: int = 500,
    now: datetime | None = None,
) -> MatchReport:
    """
    Find Kalshi/Polymarket pairs asking the same question.

    Indexes by (asset, direction) so the O(n*m) comparison only runs inside
    plausible buckets, then requires exact threshold and near-exact deadline
    agreement via assess_risk.
    """
    now = now or datetime.now(timezone.utc)
    report = MatchReport(
        kalshi_total=len(kalshi_markets),
        polymarket_total=len(polymarket_markets),
    )

    k_cands = [kalshi_candidate(m) for m in kalshi_markets]
    p_cands = [polymarket_candidate(m) for m in polymarket_markets]

    k_ok = [c for c in k_cands if c.matchable]
    p_ok = [c for c in p_cands if c.matchable]
    report.kalshi_matchable = len(k_ok)
    report.polymarket_matchable = len(p_ok)

    buckets: dict[tuple, list[Candidate]] = {}
    for c in p_ok:
        buckets.setdefault((c.subject.asset, c.subject.direction), []).append(c)

    for kc in k_ok:
        for pc in buckets.get((kc.subject.asset, kc.subject.direction), ()):
            risk, why = assess_risk(
                kc.text, pc.text, kc.subject, pc.subject, now=now
            )
            if risk is ResolutionRisk.UNKNOWN:
                report.reject(why)
                continue

            kq = kalshi_quotes.get(kc.ticker)
            pq = polymarket_quotes.get(pc.ticker)
            if kq is None or pq is None:
                report.reject("no live quote on one venue")
                continue

            orientation = safe_orientation(
                kc.subject.threshold, pc.subject.threshold,
                kc.subject.direction,
            )
            if orientation is None:
                report.reject("could not determine a safe orientation")
                continue

            report.pairs.append(
                MatchedPair(
                    kalshi=kq,
                    polymarket=pq,
                    risk=risk,
                    rationale=why,
                    safe_yes_venue=orientation,
                    strike_gap=abs(
                        kc.subject.threshold - pc.subject.threshold
                    ),
                    subject=(
                        f"{kc.subject.asset} {kc.subject.direction} "
                        f"K={kc.subject.threshold:g} P={pc.subject.threshold:g}"
                    ),
                    kalshi_rule=kc.text[:200],
                    polymarket_rule=pc.text[:200],
                )
            )
            if len(report.pairs) >= max_pairs:
                return report

    return report
