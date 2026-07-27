"""
Scan loop: pull books, build groups, run detectors, dedupe, track persistence.
"""

import logging
from datetime import datetime, timedelta, timezone

from arbengine.config import Settings
from arbengine.detectors.lp import detect_lp
from arbengine.detectors.specialized import run_specialized
from arbengine.groups import (
    build_group,
    contract_from_market,
    group_markets_by_event,
)
from arbengine.models import ArbOpportunity, ContractGroup, opportunity_key
from arbengine.source.kalshi import KalshiClient

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
    found = run_specialized(
        group, settings.fee_multiplier, now, min_profit=0.0
    )

    lp_opp = detect_lp(
        group,
        settings.fee_multiplier,
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

    for series in settings.target_series:
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

        for event_ticker, event_markets in by_event.items():
            if len(event_markets) < 2:
                continue

            tickers = [m.get("ticker", "") for m in event_markets if m.get("ticker")]
            books = await client.get_books(tickers)
            now = datetime.now(timezone.utc)

            contracts = [
                contract_from_market(m, books.get(m.get("ticker", "")), now)
                for m in event_markets
            ]
            contracts = [c for c in contracts if c.tradeable]

            group = build_group(event_ticker, series, event_ticker, contracts)
            if group:
                groups.append(group)

    log.info("Built %d validated groups", len(groups))
    return groups


async def refresh_group(
    client: KalshiClient, group: ContractGroup
) -> ContractGroup:
    """Re-fetch quotes for an existing group, keeping its state space intact."""
    books = await client.get_books(group.tickers)
    now = datetime.now(timezone.utc)
    updated = []
    for c in group.contracts:
        book = books.get(c.ticker)
        if book is None:
            updated.append(c)
            continue
        updated.append(c.model_copy(update={
            "bid": book["bid"],
            "ask": book["ask"],
            "bid_size": book["bid_size"],
            "ask_size": book["ask_size"],
            "fetched_at": now,
        }))
    return group.model_copy(update={"contracts": updated})


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
