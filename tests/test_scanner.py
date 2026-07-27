from datetime import timedelta

from arbengine.config import Settings
from arbengine.models import opportunity_key
from arbengine.scanner import PersistenceTracker, _fresh, dedupe, rank, scan_group
from tests.conftest import make_contract, make_group


def _settings(**overrides) -> Settings:
    base = {
        "kalshi_api_key_id": "test",
        "fee_multiplier": 0.0,
        "min_guaranteed_profit": 0.01,
        "min_fillable_sets": 1,
    }
    base.update(overrides)
    return Settings(**base)


def _arb_group():
    contracts = [
        make_contract("A", ask=0.30, ask_size=20),
        make_contract("B", ask=0.30, ask_size=20),
        make_contract("C", ask=0.30, ask_size=20),
    ]
    return make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])


# ── Detection wiring ──────────────────────────────────────────────────────────

def test_scan_group_finds_the_partition_arb(now) -> None:
    found = scan_group(_arb_group(), _settings(), now)
    assert found
    assert any(o.type == "partition" for o in found)


def test_scan_group_is_quiet_on_coherent_prices(now) -> None:
    contracts = [
        make_contract("A", ask=0.34, ask_size=20),
        make_contract("B", ask=0.34, ask_size=20),
        make_contract("C", ask=0.34, ask_size=20),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    assert scan_group(group, _settings(), now) == []


def test_min_fillable_sets_filters_thin_locks(now) -> None:
    contracts = [
        make_contract("A", ask=0.30, ask_size=1),
        make_contract("B", ask=0.30, ask_size=1),
        make_contract("C", ask=0.30, ask_size=1),
    ]
    group = make_group(contracts, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    assert scan_group(group, _settings(min_fillable_sets=5), now) == []


def test_min_guaranteed_profit_filters_noise(now) -> None:
    assert scan_group(_arb_group(), _settings(min_guaranteed_profit=1000.0), now) == []


# ── Staleness guard ───────────────────────────────────────────────────────────

def test_stale_quotes_are_rejected(now) -> None:
    """
    Comparing a fresh quote against a stale one manufactures phantom arbitrage.
    This is the easiest way for a scanner to lie to itself, so it is guarded.
    """
    group = _arb_group()
    assert _fresh(group, now, max_age_sec=30)
    assert not _fresh(group, now + timedelta(seconds=120), max_age_sec=30)


def test_skewed_feeds_are_rejected(now) -> None:
    group = _arb_group()
    stale_leg = group.contracts[0].model_copy(
        update={"fetched_at": now - timedelta(seconds=300)}
    )
    skewed = group.model_copy(
        update={"contracts": [stale_leg] + list(group.contracts[1:])}
    )
    assert not _fresh(skewed, now, max_age_sec=30)


# ── Dedupe and ranking ────────────────────────────────────────────────────────

def test_dedupe_keeps_the_more_profitable_duplicate(now) -> None:
    found = scan_group(_arb_group(), _settings(), now)
    keys = [opportunity_key(o) for o in found]
    assert len(keys) == len(set(keys))


def test_ranking_prefers_fewer_legs_over_raw_size(now) -> None:
    """
    Without atomic multi-leg fill, every extra leg is another chance to end up
    unhedged — so a clean 2-leg lock outranks a wide portfolio of similar size.
    """
    two_leg = scan_group(
        make_group(
            [make_contract("X", ask=0.40, ask_size=10),
             make_contract("Y", ask=0.40, ask_size=10)],
            [[1, 0], [0, 1]],
        ),
        _settings(), now,
    )
    many_leg = scan_group(_arb_group(), _settings(), now)

    ordered = rank(two_leg + many_leg, max_leg_count=4)
    assert ordered[0].leg_count <= ordered[-1].leg_count


def test_elevated_leg_count_ranks_last(now) -> None:
    two_leg = scan_group(
        make_group(
            [make_contract("X", ask=0.40, ask_size=10),
             make_contract("Y", ask=0.40, ask_size=10)],
            [[1, 0], [0, 1]],
        ),
        _settings(), now,
    )
    many_leg = scan_group(_arb_group(), _settings(), now)
    ordered = rank(many_leg + two_leg, max_leg_count=2)
    assert ordered[-1].leg_count > 2


def test_dedupe_collapses_identical_structures(now) -> None:
    found = scan_group(_arb_group(), _settings(), now)
    doubled = dedupe(found + found)
    assert len(doubled) == len(dedupe(found))


# ── Persistence ───────────────────────────────────────────────────────────────

def test_first_seen_survives_repeated_detection(now) -> None:
    """
    The persistence window is the real output of the tool. Re-detecting the same
    violation must extend one record, not start a new one.
    """
    tracker = PersistenceTracker()
    opp = scan_group(_arb_group(), _settings(), now)[0]

    first = tracker.observe(opp)
    later = opp.model_copy(update={
        "first_seen": now + timedelta(seconds=30),
        "last_seen": now + timedelta(seconds=30),
    })
    second = tracker.observe(later)

    assert second.first_seen == first.first_seen == now


def test_absent_opportunities_expire(now) -> None:
    tracker = PersistenceTracker()
    opp = scan_group(_arb_group(), _settings(), now)[0]
    tracker.observe(opp)

    gone = tracker.expire_absent(set())
    assert gone == [opportunity_key(opp)]

    # After expiry the same structure starts a fresh window.
    reappeared = opp.model_copy(update={"first_seen": now + timedelta(seconds=600)})
    assert tracker.observe(reappeared).first_seen == now + timedelta(seconds=600)
