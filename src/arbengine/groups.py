"""
Parse Kalshi market metadata into validated state spaces and payoff matrices.

This is the highest-bug-risk component in the engine: every downstream
arbitrage guarantee is conditional on the state space genuinely being mutually
exclusive and collectively exhaustive. A gap or an overlap silently converts a
"risk-free lock" into a directional bet. So this module rejects loudly rather
than guessing — a group that does not validate is skipped, not repaired.
"""

import logging
import math
import re
from datetime import datetime

import numpy as np

from arbengine.models import (
    Contract,
    ContractGroup,
    GroupValidation,
    Interval,
    StateSpace,
)

log = logging.getLogger(__name__)

NEG_INF = float("-inf")
POS_INF = float("inf")

# Tolerance for treating two bracket boundaries as the same point.
BOUNDARY_EPS = 1e-9


# ── Strike parsing ────────────────────────────────────────────────────────────

def interval_from_market(market: dict) -> Interval | None:
    """
    Derive the half-open interval [lo, hi) in which this market's YES settles
    true, from Kalshi market metadata.

    Kalshi encodes strikes in structured fields whose presence depends on
    `strike_type`:
      - "greater"          → floor_strike, YES iff x >  floor      → (floor, +inf)
      - "greater_or_equal" → floor_strike, YES iff x >= floor      → [floor, +inf)
      - "less"             → cap_strike,   YES iff x <  cap        → (-inf, cap)
      - "less_or_equal"    → cap_strike,   YES iff x <= cap        → (-inf, cap]
      - "between"          → floor_strike + cap_strike, inclusive both ends
      - "structured"/other → not a numeric range; unsupported here

    We normalize everything to half-open [lo, hi). Kalshi numeric strikes are
    quantized (whole dollars, whole degrees, one decimal on indices), so
    converting an inclusive upper bound to an exclusive one by adding a tick is
    not generally safe. Instead we keep boundaries as the raw values and rely on
    the sorted-union construction below, which only ever needs boundaries to be
    consistent *relative to each other*, never absolute.

    Returns None when the market is not a parseable numeric range, which causes
    the group to be skipped rather than mis-modelled.
    """
    strike_type = market.get("strike_type")
    floor_strike = market.get("floor_strike")
    cap_strike = market.get("cap_strike")

    if strike_type == "greater" or strike_type == "greater_or_equal":
        if floor_strike is None:
            return None
        return (float(floor_strike), POS_INF)

    if strike_type == "less" or strike_type == "less_or_equal":
        if cap_strike is None:
            return None
        return (NEG_INF, float(cap_strike))

    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            return None
        return (float(floor_strike), float(cap_strike))

    # Fall back to whatever strikes are present even if strike_type is missing
    # or unrecognized — but only when they are unambiguous.
    if floor_strike is not None and cap_strike is not None:
        return (float(floor_strike), float(cap_strike))
    if floor_strike is not None:
        return (float(floor_strike), POS_INF)
    if cap_strike is not None:
        return (NEG_INF, float(cap_strike))

    return None


# Kalshi subtitles carry currency symbols, thousands separators and degree
# marks ("$105,000 to $110,000", "72° to 74°"), so the number pattern has to
# tolerate all of them.
_NUM = r"\s*\$?\s*(-?\d[\d,]*(?:\.\d+)?)\s*°?\s*"

_SUBTITLE_RANGE = re.compile(
    rf"{_NUM}(?:to|-|–|—){_NUM}", re.IGNORECASE
)
_SUBTITLE_ABOVE = re.compile(
    rf"(?:above|over|greater than|more than|higher than|at least){_NUM}",
    re.IGNORECASE,
)
_SUBTITLE_BELOW = re.compile(
    rf"(?:below|under|less than|fewer than|lower than|at most){_NUM}",
    re.IGNORECASE,
)


def interval_from_subtitle(subtitle: str) -> Interval | None:
    """
    Last-resort parse of a bracket range out of subtitle text, for series where
    the structured strike fields are absent.

    Text parsing is strictly less trustworthy than structured strikes: it cannot
    distinguish inclusive from exclusive bounds, and Kalshi's phrasing is not
    stable across series. Any group assembled from subtitle-derived intervals
    should be treated as lower confidence, and the caller records which source
    was used. Returns None when nothing parses.
    """
    if not subtitle:
        return None

    m = _SUBTITLE_RANGE.search(subtitle)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        if lo <= hi:
            return (lo, hi)

    m = _SUBTITLE_ABOVE.search(subtitle)
    if m:
        return (float(m.group(1).replace(",", "")), POS_INF)

    m = _SUBTITLE_BELOW.search(subtitle)
    if m:
        return (NEG_INF, float(m.group(1).replace(",", "")))

    return None


# ── State space construction ──────────────────────────────────────────────────

def _boundaries(intervals: list[Interval]) -> list[float]:
    """Sorted unique finite boundary points across all intervals."""
    pts: set[float] = set()
    for lo, hi in intervals:
        if math.isfinite(lo):
            pts.add(lo)
        if math.isfinite(hi):
            pts.add(hi)
    return sorted(pts)


def build_state_space(intervals: list[Interval]) -> StateSpace:
    """
    Build the MECE partition induced by the sorted union of all bracket
    boundaries. Every input interval is then exactly a union of these states,
    which is what makes the payoff matrix well defined.

    Both open tails are ALWAYS included, so the states cover the whole real
    line. This matters: the outcome variable can in principle land outside every
    quoted bracket, and if we dropped those tails a bracket set that only tiles
    [a, b] would look exhaustive when it isn't — turning "buy every bracket for
    less than $1" into a directional bet rather than a lock. Leaving the tails
    in lets validate_group catch that case instead of hiding it.
    """
    if not intervals:
        return StateSpace(states=[], labels=[])

    pts = _boundaries(intervals)
    if not pts:
        # Every interval is (-inf, +inf): a single state.
        return StateSpace(states=[(NEG_INF, POS_INF)], labels=["any"])

    states: list[Interval] = [(NEG_INF, pts[0])]
    labels: list[str] = [f"< {_fmt(pts[0])}"]

    for a, b in zip(pts, pts[1:]):
        states.append((a, b))
        labels.append(f"[{_fmt(a)}, {_fmt(b)})")

    states.append((pts[-1], POS_INF))
    labels.append(f">= {_fmt(pts[-1])}")

    return StateSpace(states=states, labels=labels)


def _fmt(x: float) -> str:
    if x == int(x):
        return str(int(x))
    return f"{x:g}"


def _covers(interval: Interval, state: Interval) -> bool:
    """
    True if `interval` fully contains `state`. States are atoms of the partition,
    so containment is all-or-nothing: an interval either covers a state entirely
    or is disjoint from it. Partial overlap means the state space was built
    wrong, and `validate_group` checks for exactly that.
    """
    lo, hi = interval
    s_lo, s_hi = state
    return lo <= s_lo + BOUNDARY_EPS and hi >= s_hi - BOUNDARY_EPS


def _overlaps(interval: Interval, state: Interval) -> bool:
    lo, hi = interval
    s_lo, s_hi = state
    return lo < s_hi - BOUNDARY_EPS and hi > s_lo + BOUNDARY_EPS


def build_payoff_matrix(
    intervals: list[Interval], state_space: StateSpace
) -> np.ndarray:
    """M[i][s] = 1 if contract i's YES settles true in state s, else 0."""
    n, m = len(intervals), state_space.n
    matrix = np.zeros((n, m), dtype=float)
    for i, interval in enumerate(intervals):
        for s, state in enumerate(state_space.states):
            if _covers(interval, state):
                matrix[i][s] = 1.0
    return matrix


# ── Validation ────────────────────────────────────────────────────────────────

def validate_group(
    intervals: list[Interval],
    state_space: StateSpace,
    payoff: np.ndarray,
    shape: str,
) -> GroupValidation:
    """
    Reject any group whose contracts do not sit cleanly on the state partition.

    Checks, in order of how badly each one breaks the arbitrage guarantee:

    1. Partial overlap. A contract that covers part of a state means the state
       space is not fine enough, and the payoff matrix is a lie.
    2. Empty coverage. A contract that pays in no state cannot be priced.
    3. Uncovered state (brackets only). If some state is not covered by any
       contract, a "buy every bracket" portfolio is not actually guaranteed to
       pay $1, so partition detection would report phantom arbitrage.
    4. Duplicate contracts. Two contracts with identical payoff rows are fine
       mathematically but usually signal a parsing error upstream, so flag.
    """
    if not intervals:
        return GroupValidation(ok=False, reason="no contracts in group")
    if state_space.n == 0:
        return GroupValidation(ok=False, reason="empty state space")
    if len(intervals) < 2:
        return GroupValidation(ok=False, reason="need at least 2 contracts")

    for i, interval in enumerate(intervals):
        for s, state in enumerate(state_space.states):
            covers = _covers(interval, state)
            overlaps = _overlaps(interval, state)
            if overlaps and not covers:
                return GroupValidation(
                    ok=False,
                    reason=(
                        f"contract {i} interval {interval} partially overlaps "
                        f"state {state}; state space is not a valid refinement"
                    ),
                )

    row_sums = payoff.sum(axis=1)
    empty = np.where(row_sums == 0)[0]
    if empty.size:
        return GroupValidation(
            ok=False,
            reason=f"contract(s) {empty.tolist()} pay in no state",
        )

    if shape == "bracket":
        col_sums = payoff.sum(axis=0)
        uncovered = np.where(col_sums == 0)[0]
        if uncovered.size:
            labels = [state_space.labels[s] for s in uncovered.tolist()]
            return GroupValidation(
                ok=False,
                reason=f"state(s) {labels} covered by no contract; group is not exhaustive",
            )

        # For a true bracket set, every state should be covered exactly once.
        multi = np.where(col_sums > 1)[0]
        if multi.size:
            labels = [state_space.labels[s] for s in multi.tolist()]
            return GroupValidation(
                ok=False,
                reason=f"state(s) {labels} covered by multiple contracts; brackets overlap",
            )

    seen: dict[tuple, int] = {}
    for i in range(payoff.shape[0]):
        key = tuple(payoff[i])
        if key in seen:
            return GroupValidation(
                ok=False,
                reason=f"contracts {seen[key]} and {i} have identical payoffs (parse error?)",
            )
        seen[key] = i

    return GroupValidation(ok=True, reason="")


# ── Group assembly ────────────────────────────────────────────────────────────

def infer_shape(intervals: list[Interval]) -> str:
    """
    Classify the group so the right detectors and validation rules apply.

    - "ladder": every interval shares an open tail (all "at least K" or all
      "at most K"). These are nested, so states are covered many times over and
      the exhaustiveness check does not apply — implication monotonicity does.
    - "bracket": disjoint ranges that should tile the outcome variable.
    - "binary": exactly two complementary contracts.
    """
    if len(intervals) == 2:
        (a_lo, a_hi), (b_lo, b_hi) = intervals
        if (a_lo == NEG_INF and b_hi == POS_INF and a_hi == b_lo) or (
            b_lo == NEG_INF and a_hi == POS_INF and b_hi == a_lo
        ):
            return "binary"

    all_up = all(hi == POS_INF for _, hi in intervals)
    all_down = all(lo == NEG_INF for lo, _ in intervals)
    if all_up or all_down:
        return "ladder"

    return "bracket"


def build_group(
    group_id: str,
    series_ticker: str,
    event_ticker: str,
    contracts: list[Contract],
) -> ContractGroup | None:
    """
    Assemble a validated ContractGroup. Returns None when the group cannot be
    modelled — missing intervals, or a state space that fails validation.
    Callers should skip None rather than fall back to a looser check.
    """
    usable = [c for c in contracts if c.interval is not None]
    if len(usable) < 2:
        log.debug("Group %s: fewer than 2 contracts with parseable intervals", group_id)
        return None

    intervals = [c.interval for c in usable]
    shape = infer_shape(intervals)
    state_space = build_state_space(intervals)
    payoff = build_payoff_matrix(intervals, state_space)
    validation = validate_group(intervals, state_space, payoff, shape)

    if not validation.ok:
        log.info("Group %s rejected: %s", group_id, validation.reason)
        return None

    return ContractGroup(
        group_id=group_id,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
        contracts=usable,
        state_space=state_space,
        payoff=payoff,
        validation=validation,
        shape=shape,
    )


def contract_from_market(
    market: dict, book: dict | None, fetched_at: datetime
) -> Contract:
    """
    Build a Contract from Kalshi market metadata plus an optional parsed book.

    `book` is the already-normalized {bid, ask, bid_size, ask_size} dict from
    source/kalshi.py, kept separate so metadata parsing stays testable without
    live quotes.
    """
    interval = interval_from_market(market)
    subtitle = (
        market.get("yes_sub_title")
        or market.get("subtitle")
        or market.get("title")
        or ""
    )
    if interval is None:
        interval = interval_from_subtitle(subtitle)

    book = book or {}
    return Contract(
        ticker=market.get("ticker", ""),
        bid=book.get("bid"),
        ask=book.get("ask"),
        bid_size=book.get("bid_size") or 0,
        ask_size=book.get("ask_size") or 0,
        fetched_at=fetched_at,
        interval=interval,
        strike_type=market.get("strike_type"),
        subtitle=subtitle,
    )


def group_markets_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """
    Bucket markets into candidate groups by event ticker. One Kalshi event is
    the natural unit of a shared outcome variable: all brackets for one CPI
    print, one settlement date, one city-day high temperature.
    """
    groups: dict[str, list[dict]] = {}
    for m in markets:
        event = m.get("event_ticker") or ""
        if not event:
            continue
        groups.setdefault(event, []).append(m)
    return groups
