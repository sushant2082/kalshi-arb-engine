"""
Human-verified pair registry — a structural gate on cross-venue trading.

Adapted from the approach in NIkhil-cmd-cmd/kalshi-polymarket-arb, which gets
something right that price analysis cannot: a cross-venue position is only a
hedge if BOTH venues resolve on the same real-world outcome, and no amount of
order-book inspection establishes that. Someone has to read both rulebooks.

So pairs carry a status, and nothing may be traded on a pair that is not
`confirmed`. Automatic matching (crossmlb, matcher, players) still runs and
still finds candidates — it just cannot promote one to tradeable on its own.

This session produced repeated evidence for why that gate is needed. Every
large apparent edge measured here was an artifact of a matching or staleness
error, never a real mispricing:

    date-joined baseball series   one sharp line onto 6 games, one decided
    in-progress games             pregame line vs live price, up to +22.9%
    election district codes       "Nominee" market matched to "wins seat"
    15s Kalshi cache              +10.25% that vanished on the next read

Their repo records the same lesson independently: a Fed pair looked like a
40-point edge until the baseline rate assumption was corrected, after which
the two venues agreed to within a point.

MECHANICALLY-JOINED MARKETS ARE EXEMPT. Sports games matched on team pair and
first pitch (crossmlb) are joined on structured identifiers that cannot be
ambiguous, and both venues publish the same settlement rule — the winner of a
specific game at a specific time. Those do not need per-pair human review; the
join itself is the verification. This registry exists for everything else,
where the mapping is a judgement call.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

log = logging.getLogger(__name__)

PairStatus = Literal["confirmed", "needs_review", "rejected"]


@dataclass
class VerifiedPair:
    """A cross-venue mapping and what is actually known about it."""

    pair_id: str
    kalshi_ticker: str
    polymarket_condition_id: str
    label: str
    status: PairStatus = "needs_review"
    # What was checked, by whom, and what was found. Prose on purpose: the
    # reason a pair is or is not tradeable rarely fits a flag.
    note: str = ""
    reviewed_at: str = ""
    # Settlement differences that are known and accepted rather than unnoticed.
    known_divergences: list[str] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        return self.status == "confirmed"


# Manually reviewed pairs. Never flip a pair to "confirmed" without reading
# both venues' resolution text — `rules_primary` on Kalshi, `description` on
# Polymarket — and satisfying yourself they settle on the same event under the
# same edge cases.
PAIRS: list[VerifiedPair] = [
    VerifiedPair(
        pair_id="mlb-game-moneyline",
        kalshi_ticker="KXMLBGAME-*",
        polymarket_condition_id="mlb-*",
        label="MLB game moneylines (mechanically joined)",
        status="confirmed",
        reviewed_at="2026-08-05",
        note=(
            "Joined on team pair plus first pitch, both taken from structured "
            "identifiers: Kalshi's ticker carries date + HHMM Eastern, "
            "Polymarket publishes gameStartTime in UTC, and the two agree to "
            "the minute. Both venues resolve on the winner of that specific "
            "game. This is the one category where the join is itself the "
            "verification, so it does not need per-game review."
        ),
        known_divergences=[
            "Postponed or suspended games: Kalshi and Polymarket may differ on "
            "whether a rescheduled game settles the original market or voids "
            "it. Not yet checked against both rulebooks.",
            "Rain-shortened official games: Kalshi's MLB rules make a game "
            "official after 5 innings (4.5 with the home team leading). "
            "Polymarket's wording has not been compared against that.",
        ],
    ),
]


def get_pairs() -> list[VerifiedPair]:
    return list(PAIRS)


def confirmed_pairs() -> list[VerifiedPair]:
    return [p for p in PAIRS if p.tradeable]


def status_for(kalshi_ticker: str, polymarket_id: str = "") -> PairStatus:
    """
    Status for a mapping, matching registry entries by prefix wildcard.

    Defaults to `needs_review` for anything unlisted — an unknown pair is not
    an approved one, and the failure direction that costs money is treating an
    unreviewed mapping as tradeable.
    """
    for p in PAIRS:
        kt = p.kalshi_ticker
        if kt.endswith("*") and kalshi_ticker.startswith(kt[:-1]):
            return p.status
        if kt == kalshi_ticker:
            return p.status
    return "needs_review"


def is_tradeable(kalshi_ticker: str, polymarket_id: str = "") -> bool:
    return status_for(kalshi_ticker, polymarket_id) == "confirmed"


def review_reminder() -> str:
    unreviewed = [p for p in PAIRS if p.status == "needs_review"]
    if not unreviewed:
        return ""
    return (
        f"{len(unreviewed)} pair(s) awaiting rule review; they will be scanned "
        "and reported but never counted as tradeable."
    )
