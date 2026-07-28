"""
Scan loop: pull books, build groups, run detectors, dedupe, track persistence.
"""

import logging
from datetime import datetime, timedelta, timezone

from arbengine.config import Settings
import numpy as np

from arbengine.detectors.lp import detect_lp
from arbengine.detectors.specialized import run_specialized
from arbengine.fees import fee_per_contract
from arbengine.groups import (
    build_group,
    contract_from_market,
    group_markets_by_event,
)
from arbengine.models import ArbOpportunity, ContractGroup, opportunity_key
from arbengine.source.kalshi import KalshiClient, quote_from_market

log = logging.getLogger(__name__)


class PersistenceTracker:
    """
    Holds `first_seen` for every live opportunity so repeated detections of the
    same violation extend one record instead of creating new ones. Opportunities
    absent for a full scan are expired, which is what closes out a persistence
    window.
    """

    def __init__(self) -> None:
        self._first_seen: dict[str, datetime] = {}
        self._live: set[str] = set()

    def observe(self, opp: ArbOpportunity) -> ArbOpportunity:
        key = opportunity_key(opp)
        first = self._first_seen.setdefault(key, opp.first_seen)
        self._live.add(key)
        return opp.model_copy(update={"first_seen": first})

    def expire_absent(self, seen_this_scan: set[str]) -> list[str]:
        """Drop tracking for opportunities that vanished; return their keys."""
        gone = self._live - seen_this_scan
        for key in gone:
            self._first_seen.pop(key, None)
        self._live = set(seen_this_scan)
        return sorted(gone)


def rank(opportunities: list[ArbOpportunity], max_leg_count: int) -> list[ArbOpportunity]:
    """
    Order opportunities by how executable they are, not just by size.

    A 2-leg specialized lock beats a wide LP portfolio of the same nominal
    profit, because without atomic multi-leg fill every extra leg is another
    chance to end up unhedged. So sort on (within-leg-budget, leg count, profit).
    """
    return sorted(
        opportunities,
        key=lambda o: (
            o.leg_count > max_leg_count,   # elevated-risk ones last
            o.leg_count,                   # fewer legs first
            -o.guaranteed_profit,          # then by size
        ),
    )


def dedupe(opportunities: list[ArbOpportunity]) -> list[ArbOpportunity]:
    """
    Collapse to one opportunity per (group, structure). A specialized detector
    and the LP will often describe the same violation; keep whichever reports
    more profit, preferring the specialized one on ties since it has fewer legs.
    """
    best: dict[str, ArbOpportunity] = {}
    for opp in opportunities:
        key = opportunity_key(opp)
        current = best.get(key)
        if current is None or opp.guaranteed_profit > current.guaranteed_profit:
            best[key] = opp
    return list(best.values())


def _fresh(group: ContractGroup, now: datetime, max_age_sec: int) -> bool:
    """
    Reject groups whose quotes are stale or skewed across legs.

    Comparing a fresh quote against a stale one manufactures phantom arbitrage,
    which is the single easiest way for a scanner like this to lie to itself.
    """
    cutoff = now - timedelta(seconds=max_age_sec)
    times = [c.fetched_at for c in group.contracts]
    if any(t < cutoff for t in times):
        return False
    skew = (max(times) - min(times)).total_seconds()
    return skew <= max_age_sec


def scan_group(
    group: ContractGroup, settings: Settings, now: datetime
) -> list[ArbOpportunity]:
    """
    Run specialized detectors then the LP against one validated group, and
    return everything that clears the profit and size thresholds.
    """
    # Kalshi scales the taker fee per series, so a fee-free series must not be
    # charged the standard rate — that would filter out exactly the locks most
    # likely to be real.
    fee_multiplier = settings.fee_multiplier * group.fee_scale

    found = run_specialized(
        group, fee_multiplier, now, min_profit=0.0
    )

    lp_opp = detect_lp(
        group,
        fee_multiplier,
        now,
        tolerance=settings.lp_tolerance,
        min_profit=0.0,
    )
    if lp_opp:
        found.append(lp_opp)

        # The LP is the general case of every specialized detector, so it should
        # never find less than they do. When it does, something is wrong in one
        # of them — surface it rather than quietly trusting the larger number.
        best_specialized = max(
            (o.guaranteed_profit for o in found if o.type != "lp"), default=0.0
        )
        if best_specialized > lp_opp.guaranteed_profit + settings.lp_tolerance:
            log.warning(
                "Cross-check failed on %s: specialized found $%.4f but LP only $%.4f",
                group.group_id, best_specialized, lp_opp.guaranteed_profit,
            )

    qualified = [
        o for o in found
        if o.guaranteed_profit >= settings.min_guaranteed_profit
        and o.fillable_sets >= settings.min_fillable_sets
    ]
    return dedupe(qualified)


async def build_groups(
    client: KalshiClient, settings: Settings
) -> list[ContractGroup]:
    """
    Discover markets for the target series, fetch their books, and assemble
    validated groups. Groups that fail state-space validation are skipped, not
    repaired — see groups.validate_group for why.
    """
    groups: list[ContractGroup] = []

    try:
        fee_scales = await client.series_fee_scales(settings.target_series)
    except Exception as exc:
        log.warning(
            "Could not read per-series fee metadata (%s); "
            "falling back to the standard rate for every series", exc,
        )
        fee_scales = {}

    for series in settings.target_series:
        fee_scale = fee_scales.get(series, 1.0)
        if fee_scale != 1.0:
            log.info(
                "Series %s has a non-standard fee scale of %.3f "
                "(effective multiplier %.4f)",
                series, fee_scale, settings.fee_multiplier * fee_scale,
            )

        try:
            markets = await client.list_markets(series_ticker=series)
        except Exception as exc:
            log.warning("Could not list markets for %s: %s", series, exc)
            continue

        if not markets:
            log.info("Series %s returned no open markets", series)
            continue

        by_event = group_markets_by_event(markets)
        log.info("Series %s: %d markets in %d events", series, len(markets), len(by_event))

        now = datetime.now(timezone.utc)
        for event_ticker, event_markets in by_event.items():
            if len(event_markets) < 2:
                continue

            # Quotes come from the same /markets payload as the strikes, so
            # every leg in an event shares one timestamp — no cross-leg skew,
            # and no per-market request storm.
            contracts = [
                contract_from_market(m, quote_from_market(m), now)
                for m in event_markets
            ]
            contracts = [c for c in contracts if c.tradeable]

            group = build_group(
                event_ticker, series, event_ticker, contracts, fee_scale=fee_scale
            )
            if group:
                groups.append(group)

    log.info("Built %d validated groups", len(groups))
    return groups


async def refresh_group(
    client: KalshiClient, group: ContractGroup
) -> ContractGroup:
    """
    Re-fetch quotes for an existing group, keeping its state space intact.

    One /markets call scoped to the event refreshes every leg at once. Beyond
    saving requests, this is what keeps the legs time-consistent: quotes pulled
    one-by-one drift apart, and comparing a fresh leg against a stale one
    manufactures arbitrage that was never there.
    """
    try:
        markets = await client.list_markets(event_ticker=group.event_ticker)
    except Exception as exc:
        log.debug("Refresh failed for %s: %s", group.group_id, exc)
        return group

    quotes = {
        m.get("ticker"): quote_from_market(m) for m in markets if m.get("ticker")
    }
    now = datetime.now(timezone.utc)

    updated = []
    for c in group.contracts:
        q = quotes.get(c.ticker)
        if q is None:
            updated.append(c)
            continue
        updated.append(c.model_copy(update={
            "bid": q["bid"],
            "ask": q["ask"],
            "bid_size": q["bid_size"],
            "ask_size": q["ask_size"],
            "fetched_at": now,
        }))
    return group.model_copy(update={"contracts": updated})


def near_miss(group: ContractGroup, settings: Settings) -> dict:
    """
    Measure how far a group is from violating coherence, whether or not it does.

    A bare "no violations" is indistinguishable from a detector that is silently
    broken. The margin makes the difference observable: a ladder sitting 2 cents
    from inversion is a live near-miss worth watching, while one sitting 40
    cents away is genuinely coherent.

    Returns the best (least negative) margin in dollars per set. Positive means
    an actual violation.
    """
    fee_mult = settings.fee_multiplier * group.fee_scale
    contracts = group.contracts
    payoff = group.payoff

    best_mono = None
    for i, a in enumerate(contracts):
        for j, b in enumerate(contracts):
            if i == j:
                continue
            if not (
                bool(np.all(payoff[j] >= payoff[i]))
                and bool(np.any(payoff[j] > payoff[i]))
            ):
                continue
            if a.bid is None or b.ask is None or a.bid_size <= 0 or b.ask_size <= 0:
                continue
            margin = (a.bid - fee_per_contract(a.bid, fee_mult)) - (
                b.ask + fee_per_contract(b.ask, fee_mult)
            )
            if best_mono is None or margin > best_mono:
                best_mono = margin

    partition_cost = None
    if group.shape == "bracket" and all(
        c.ask is not None and c.ask_size > 0 for c in contracts
    ):
        if np.all(payoff.sum(axis=0) == 1):
            partition_cost = sum(
                c.ask + fee_per_contract(c.ask, fee_mult) for c in contracts
            )

    return {
        "group_id": group.group_id,
        "shape": group.shape,
        "legs": len(contracts),
        "quoted": sum(1 for c in contracts if c.ask is not None),
        "monotonic_margin": best_mono,
        "partition_cost": partition_cost,
    }


def scan_all(
    groups: list[ContractGroup], settings: Settings, now: datetime
) -> list[ArbOpportunity]:
    """Run every group through the detectors, filtering out stale quotes."""
    out: list[ArbOpportunity] = []
    for group in groups:
        if not _fresh(group, now, settings.max_quote_age_sec):
            log.debug("Skipping %s: stale or skewed quotes", group.group_id)
            continue
        out.extend(scan_group(group, settings, now))
    return rank(out, settings.max_leg_count_alert)
