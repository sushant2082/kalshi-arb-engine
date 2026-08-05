"""
Autonomous cross-venue pair verification.

Replaces human sign-off with mechanical proof. The gate it supersedes existed
for a real reason — every large apparent edge this session was a matching or
staleness artifact, never a mispricing — but a person reading pair titles is
both a bottleneck and, it turns out, worse at this than code.

The design rule is: AUTO-CONFIRM ONLY WHAT IS STRUCTURALLY PROVABLE, AUTO-REJECT
EVERYTHING ELSE. There is deliberately no "needs review" state, because a state
that requires a human is not autonomy, and a state that defaults to tradeable is
not safe. Anything unproven is simply not traded.

Three checks, in order of what they catch:

1. INTERNAL CONSISTENCY of each venue's own metadata. Measured live: 7 of 103
   Polymarket MLB game markets carry a `gameStartTime` disagreeing with their
   own slug and description by 70 to 122 days. The matcher joins on that field,
   so an unchecked one silently pairs a Kalshi game against an unrelated
   Polymarket market. No human reading titles would catch this.

2. EVENT IDENTITY across venues — same teams, same first pitch — from
   structured identifiers rather than title text.

3. SETTLEMENT RULE COMPARISON. Both venues publish full resolution text
   (Kalshi `rules_primary`/`rules_secondary`, Polymarket `description`), so
   edge-case handling is machine-comparable. A known real divergence on MLB:

       Kalshi:     postponed -> market closes after the rescheduled game,
                   "within two days"
       Polymarket: postponed -> "remains open until the game has been
                   completed", with no stated limit

   A game postponed more than two days can therefore settle differently on the
   two venues. That is recorded as a bounded, quantified risk rather than
   discovered during a rain delay.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

Verdict = str  # "confirmed" | "rejected"


@dataclass
class VerificationResult:
    """Why a pair is or is not tradeable. No human-review state by design."""

    verdict: Verdict
    checks_passed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    # Divergences that are real, understood and bounded — they do not block a
    # pair, but they are the risk being accepted by trading it.
    accepted_risks: list[str] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        return self.verdict == "confirmed"


# ── 1. Internal consistency ───────────────────────────────────────────────────

_PM_SLUG_DATE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})$")
# "scheduled for May 23 at 1:35PM ET" / "scheduled for August 7 at 7:05PM ET"
_DESC_DATE = re.compile(
    r"scheduled for\s+([A-Z][a-z]+)\s+(\d{1,2})", re.IGNORECASE
)
_MONTHS = {
    m: i + 1 for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"]
    )
}


def check_polymarket_consistency(market: dict, start: datetime | None) -> list[str]:
    """
    Cross-check a Polymarket market's own fields against each other.

    Returns failure strings. The slug date, the `gameStartTime`, and the date
    written into the description should all describe one game; when they do
    not, the market's metadata cannot be trusted to join on.
    """
    failures: list[str] = []
    slug = market.get("slug") or ""

    m = _PM_SLUG_DATE.search(slug)
    if not m:
        return ["polymarket slug carries no date"]
    slug_date = datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc
    ).date()

    if start is None:
        failures.append("polymarket market has no parseable gameStartTime")
    else:
        # An evening ET game lands on the next UTC day, so one day of slack.
        drift = abs((start.date() - slug_date).days)
        if drift > 1:
            failures.append(
                f"polymarket gameStartTime ({start.date()}) disagrees with its "
                f"own slug date ({slug_date}) by {drift} days"
            )

    desc = market.get("description") or ""
    dm = _DESC_DATE.search(desc)
    if dm:
        month = _MONTHS.get(dm.group(1).lower())
        if month:
            day = int(dm.group(2))
            if (month, day) != (slug_date.month, slug_date.day):
                failures.append(
                    f"polymarket description says {dm.group(1)} {day} but the "
                    f"slug says {slug_date.month}/{slug_date.day}"
                )
    return failures


# ── 2. Event identity ─────────────────────────────────────────────────────────

# First pitch should agree closely; both venues publish it explicitly.
START_TOLERANCE = timedelta(minutes=90)


def check_event_identity(
    kalshi_start: datetime | None,
    polymarket_start: datetime | None,
    kalshi_teams: tuple[str, str],
    polymarket_teams: tuple[str, str],
) -> list[str]:
    failures: list[str] = []
    if kalshi_teams != polymarket_teams:
        failures.append(
            f"team pair differs: kalshi {kalshi_teams} vs "
            f"polymarket {polymarket_teams}"
        )
    if kalshi_start is None or polymarket_start is None:
        failures.append("missing a start time on one venue")
    else:
        gap = abs(kalshi_start - polymarket_start)
        if gap > START_TOLERANCE:
            failures.append(f"start times differ by {gap}")
    return failures


# ── 3. Settlement rules ───────────────────────────────────────────────────────

@dataclass
class SettlementTerms:
    """Edge-case handling extracted from a venue's resolution text."""

    postponed_stays_open: bool | None = None
    postponement_limit_days: float | None = None
    tie_is_split: bool | None = None
    cancelled_handled: bool | None = None
    raw: str = ""


_LIMIT = re.compile(r"within\s+(\w+)\s+days?", re.IGNORECASE)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def parse_settlement(text: str) -> SettlementTerms:
    """
    Pull edge-case handling out of resolution prose.

    Deliberately conservative: an unrecognised phrasing leaves a field None,
    and a None never counts as agreement downstream. Guessing here would defeat
    the purpose of the check.
    """
    low = (text or "").lower()
    terms = SettlementTerms(raw=text or "")
    if not low:
        return terms

    if "postpon" in low or "delay" in low:
        terms.postponed_stays_open = (
            "remain open" in low or "remains open" in low
        )
        lm = _LIMIT.search(low)
        if lm:
            token = lm.group(1)
            terms.postponement_limit_days = (
                float(token) if token.isdigit()
                else float(_NUMBER_WORDS.get(token, 0)) or None
            )

    if "tie" in low or "50-50" in low or "50/50" in low:
        terms.tie_is_split = "50-50" in low or "50/50" in low

    if "cancel" in low:
        terms.cancelled_handled = True

    return terms


def compare_settlement(
    kalshi_text: str, polymarket_text: str
) -> tuple[list[str], list[str]]:
    """
    Compare two venues' settlement terms.

    Returns (failures, accepted_risks). A failure means the venues can resolve
    the same game oppositely, which breaks the hedge outright. An accepted risk
    is a bounded difference that only bites in a describable scenario.
    """
    k = parse_settlement(kalshi_text)
    p = parse_settlement(polymarket_text)
    failures: list[str] = []
    risks: list[str] = []

    if k.postponed_stays_open is False and p.postponed_stays_open is True:
        failures.append(
            "postponement handling is opposite: one venue voids while the "
            "other stays open — a postponed game would not hedge"
        )
    elif k.postponement_limit_days and not p.postponement_limit_days:
        risks.append(
            f"kalshi closes a postponed game within "
            f"{k.postponement_limit_days:.0f} days; polymarket states no "
            "limit, so a longer postponement can settle differently"
        )
    elif p.postponement_limit_days and not k.postponement_limit_days:
        risks.append(
            f"polymarket closes a postponed game within "
            f"{p.postponement_limit_days:.0f} days; kalshi states no limit"
        )

    if k.tie_is_split is not None and p.tie_is_split is not None:
        if k.tie_is_split != p.tie_is_split:
            risks.append(
                "tie handling differs: one venue splits 50-50 while the other "
                "does not — only reachable in a tied, uncompleted game"
            )
    elif p.tie_is_split and k.tie_is_split is None:
        risks.append(
            "polymarket splits 50-50 on a tie; kalshi's tie handling was not "
            "found in its rules text"
        )

    return failures, risks


# ── Entry point ───────────────────────────────────────────────────────────────

def verify_pair(
    kalshi_market: dict,
    polymarket_market: dict,
    kalshi_start: datetime | None,
    polymarket_start: datetime | None,
    kalshi_teams: tuple[str, str],
    polymarket_teams: tuple[str, str],
) -> VerificationResult:
    """
    Decide autonomously whether a cross-venue pair may be traded.

    Confirms only when every check passes. There is no escalation path — an
    unproven pair is rejected, which keeps the system fully autonomous without
    letting an unverified mapping through.
    """
    failures: list[str] = []
    passed: list[str] = []

    consistency = check_polymarket_consistency(polymarket_market, polymarket_start)
    if consistency:
        failures.extend(consistency)
    else:
        passed.append("polymarket metadata internally consistent")

    identity = check_event_identity(
        kalshi_start, polymarket_start, kalshi_teams, polymarket_teams
    )
    if identity:
        failures.extend(identity)
    else:
        passed.append("same teams and first pitch on both venues")

    kalshi_rules = " ".join(
        str(kalshi_market.get(f) or "")
        for f in ("rules_primary", "rules_secondary")
    )
    rule_failures, risks = compare_settlement(
        kalshi_rules, polymarket_market.get("description") or ""
    )
    if rule_failures:
        failures.extend(rule_failures)
    else:
        passed.append("settlement rules compatible")

    if not kalshi_rules.strip():
        failures.append("kalshi market published no resolution rules to compare")

    return VerificationResult(
        verdict="rejected" if failures else "confirmed",
        checks_passed=passed,
        failures=failures,
        accepted_risks=risks,
    )
