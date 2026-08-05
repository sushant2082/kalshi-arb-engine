"""
Autonomous verification. No human-review state exists by design: anything
unproven is rejected, which is what makes full autonomy safe rather than
merely unattended.
"""

from datetime import datetime, timedelta, timezone

from arbengine.verify import (
    check_event_identity,
    check_polymarket_consistency,
    compare_settlement,
    parse_settlement,
    verify_pair,
)

START = datetime(2026, 8, 7, 23, 5, tzinfo=timezone.utc)

KALSHI_RULES = (
    "If San Francisco wins the Detroit vs San Francisco professional baseball "
    "game originally scheduled for Aug 7, 2026 at 10:15 PM EDT, then the "
    "market resolves to Yes."
)
KALSHI_SECONDARY = (
    "If this game is postponed or delayed, the market will remain open and "
    "close after the rescheduled game has finished (within two days). If the "
    "game is cancelled, the market resolves to No."
)
PM_DESC = (
    "In the upcoming MLB game between the Detroit Tigers and San Francisco "
    "Giants, scheduled for August 7 at 7:05PM ET: "
    "If the game is postponed, this market will remain open until the game has "
    "been completed. If the game is canceled entirely, with no make-up game, "
    "or ends in a tie, this market will resolve 50-50."
)


def _pm(slug="mlb-det-sf-2026-08-07", desc=PM_DESC):
    return {"slug": slug, "description": desc}


def _k():
    return {"rules_primary": KALSHI_RULES, "rules_secondary": KALSHI_SECONDARY}


# ── Internal consistency: the check a human skimming titles would miss ───────

def test_gamestarttime_disagreeing_with_slug_is_rejected() -> None:
    """
    Measured live: 7 of 103 Polymarket MLB markets carry a gameStartTime 70-122
    days from their own slug. The matcher joins on that field, so an unchecked
    one pairs a Kalshi game against an unrelated market.
    """
    bad = datetime(2026, 9, 22, 17, 5, tzinfo=timezone.utc)
    fails = check_polymarket_consistency(_pm("mlb-tb-nyy-2026-05-23"), bad)
    assert fails
    assert "disagrees with its own slug" in " ".join(fails)


def test_evening_game_crossing_utc_midnight_is_allowed() -> None:
    """An ET night game lands on the next UTC day; that is not an error."""
    start = datetime(2026, 8, 8, 1, 40, tzinfo=timezone.utc)
    assert check_polymarket_consistency(_pm("mlb-det-sf-2026-08-07"), start) == []


def test_description_date_contradicting_the_slug_is_rejected() -> None:
    desc = "scheduled for May 23 at 1:35PM ET: ..."
    fails = check_polymarket_consistency(_pm("mlb-det-sf-2026-08-07", desc), START)
    assert any("description says" in f for f in fails)


def test_missing_start_time_is_rejected() -> None:
    assert check_polymarket_consistency(_pm(), None)


# ── Event identity ────────────────────────────────────────────────────────────

def test_mismatched_teams_rejected() -> None:
    fails = check_event_identity(START, START, ("DET", "SF"), ("DET", "SEA"))
    assert any("team pair differs" in f for f in fails)


def test_mismatched_start_rejected() -> None:
    fails = check_event_identity(
        START, START + timedelta(hours=5), ("DET", "SF"), ("DET", "SF")
    )
    assert any("start times differ" in f for f in fails)


# ── Settlement rules ──────────────────────────────────────────────────────────

def test_parses_postponement_limit() -> None:
    t = parse_settlement(KALSHI_SECONDARY)
    assert t.postponed_stays_open is True
    assert t.postponement_limit_days == 2


def test_parses_polymarket_tie_split() -> None:
    t = parse_settlement(PM_DESC)
    assert t.tie_is_split is True
    assert t.postponed_stays_open is True


def test_real_postponement_divergence_is_surfaced_as_a_risk() -> None:
    """
    Kalshi closes a postponed game within two days; Polymarket states no limit.
    A longer postponement can settle differently. Bounded, so it is an accepted
    risk rather than a blocker — but it must be reported, not discovered during
    a rain delay.
    """
    failures, risks = compare_settlement(
        KALSHI_RULES + " " + KALSHI_SECONDARY, PM_DESC
    )
    assert failures == []
    assert any("postponed" in r and "two days" in r.replace("2", "two")
               or "2 days" in r for r in risks)


def test_opposite_postponement_handling_is_a_hard_failure() -> None:
    """One venue voiding while the other stays open breaks the hedge outright."""
    kalshi_voids = "If this game is postponed, the market resolves to No."
    failures, _ = compare_settlement(kalshi_voids, PM_DESC)
    assert any("postponement handling is opposite" in f for f in failures)


def test_unrecognised_phrasing_never_counts_as_agreement() -> None:
    t = parse_settlement("Resolution follows the official result.")
    assert t.postponed_stays_open is None
    assert t.tie_is_split is None


# ── End to end ────────────────────────────────────────────────────────────────

def test_a_good_pair_is_confirmed_with_its_risks_recorded() -> None:
    r = verify_pair(_k(), _pm(), START, START, ("DET", "SF"), ("DET", "SF"))
    assert r.tradeable
    assert r.verdict == "confirmed"
    assert len(r.checks_passed) == 3
    # Confirmed does not mean identical — the divergence is stated.
    assert r.accepted_risks


def test_inconsistent_metadata_blocks_an_otherwise_good_pair() -> None:
    bad = datetime(2026, 9, 22, 17, 5, tzinfo=timezone.utc)
    r = verify_pair(_k(), _pm(), START, bad, ("DET", "SF"), ("DET", "SF"))
    assert not r.tradeable
    assert r.verdict == "rejected"


def test_missing_kalshi_rules_blocks_the_pair() -> None:
    """Nothing to compare is not the same as nothing to worry about."""
    r = verify_pair({}, _pm(), START, START, ("DET", "SF"), ("DET", "SF"))
    assert not r.tradeable


def test_there_is_no_needs_review_verdict() -> None:
    """
    A state requiring a human is not autonomy; a state defaulting to tradeable
    is not safe. Only confirmed and rejected exist.
    """
    for args in [
        (_k(), _pm(), START, START, ("DET", "SF"), ("DET", "SF")),
        ({}, _pm(), None, None, ("A", "B"), ("C", "D")),
    ]:
        assert verify_pair(*args).verdict in ("confirmed", "rejected")
