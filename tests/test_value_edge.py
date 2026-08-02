"""
Value betting is the one strategy here that can lose. These tests mostly pin
the guards that stop a noisy signal from being treated as an edge.
"""

from datetime import datetime, timedelta, timezone

import pytest

from arbengine.value.edge import (
    KalshiSide,
    SharpQuote,
    evaluate,
    fair_probabilities,
    kelly_fraction,
    summarize_expectations,
)

NOW = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=3)


def _quote(home=0.55, away=0.50, at=NOW) -> SharpQuote:
    return SharpQuote(
        book="pinnacle", event_id="e1", home_team="Braves", away_team="Nationals",
        commence_time=START, home_implied=home, away_implied=away, fetched_at=at,
    )


def _side(ask=0.45, size=500, at=NOW) -> KalshiSide:
    return KalshiSide(
        ticker="KXMLBGAME-X-ATL", team="Braves", ask=ask, ask_size=size,
        fetched_at=at,
    )


# ── Devig ─────────────────────────────────────────────────────────────────────

def test_devig_removes_the_book_margin() -> None:
    q = _quote(0.55, 0.50)          # overround 1.05
    home, away = fair_probabilities(q, "proportional")
    assert home + away == pytest.approx(1.0)
    assert home < 0.55, "devigging must reduce the raw implied probability"


def test_devig_methods_agree_closely_on_a_tight_line() -> None:
    """On a low-margin book the method choice should barely matter."""
    q = _quote(0.512, 0.508)  # ~2% overround
    probs = [fair_probabilities(q, m)[0] for m in ("proportional", "power", "shin")]
    assert max(probs) - min(probs) < 0.01


# ── Kelly ─────────────────────────────────────────────────────────────────────

def test_kelly_is_zero_without_an_edge() -> None:
    assert kelly_fraction(0.50, 0.50) == 0.0
    assert kelly_fraction(0.40, 0.50) == 0.0


def test_kelly_scales_with_edge() -> None:
    assert kelly_fraction(0.60, 0.50) > kelly_fraction(0.55, 0.50) > 0


def test_kelly_handles_degenerate_prices() -> None:
    assert kelly_fraction(0.9, 1.0) == 0.0
    assert kelly_fraction(0.9, 0.0) == 0.0


# ── The guards ────────────────────────────────────────────────────────────────

def test_fees_must_be_cleared_before_an_edge_counts() -> None:
    """
    A 2-cent raw edge against a ~1.75-cent fee is not a 2-cent edge. Ignoring
    the fee is how a break-even strategy reads as profitable.
    """
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    side = _side(ask=home - 0.02)  # exactly 2 cents of raw edge
    assert evaluate(side, q, home, bankroll=10_000, min_net_edge=0.02) is None


def test_a_genuine_edge_survives() -> None:
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    side = _side(ask=home - 0.08)
    opp = evaluate(side, q, home, bankroll=10_000, min_net_edge=0.02)
    assert opp is not None
    assert opp.net_edge > 0.02
    assert opp.contracts > 0


def test_skewed_quotes_are_rejected_outright() -> None:
    """
    A sharp line and a Kalshi price captured minutes apart measure the market's
    movement, not an edge. This is a hard reject, not a warning.
    """
    q = _quote(0.52, 0.52, at=NOW - timedelta(minutes=10))
    home, _ = fair_probabilities(q)
    side = _side(ask=home - 0.10, at=NOW)
    assert evaluate(side, q, home, bankroll=10_000, max_quote_skew_sec=60) is None


def test_no_depth_means_no_opportunity() -> None:
    q = _quote()
    home, _ = fair_probabilities(q)
    assert evaluate(_side(ask=home - 0.10, size=0), q, home, bankroll=10_000) is None


def test_position_is_never_reported_as_riskless() -> None:
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.10), q, home, bankroll=10_000)
    assert opp.is_riskless is False
    assert opp.loses_full_stake_probability == pytest.approx(1 - opp.fair_prob)


# ── Warnings on the edges that are most likely to be wrong ────────────────────

def test_huge_edges_are_flagged_as_probably_wrong() -> None:
    """
    A 20-point disagreement with a sharp book is far more often a stale line or
    a bad match than a real mispricing. Flagging it is the difference between a
    tool that helps and one that confidently loses money.
    """
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.25), q, home, bankroll=10_000)
    assert opp is not None
    assert any("unusually large" in w for w in opp.warnings)


def test_wide_sharp_lines_are_flagged() -> None:
    q = _quote(0.62, 0.55)  # overround 1.17
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.10), q, home, bankroll=10_000)
    assert any("wide sharp line" in w for w in opp.warnings)


def test_longshots_are_flagged() -> None:
    q = _quote(0.09, 0.94)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=max(home - 0.03, 0.01)), q, home,
                   bankroll=10_000, min_net_edge=0.005)
    if opp is not None:
        assert any("longshot" in w for w in opp.warnings)


# ── Sizing ────────────────────────────────────────────────────────────────────

def test_stake_is_capped_regardless_of_kelly() -> None:
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.30, size=100_000), q, home,
                   bankroll=10_000, max_stake_fraction=0.05)
    assert opp.stake_fraction <= 0.05


def test_size_never_exceeds_available_depth() -> None:
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.20, size=7), q, home, bankroll=1_000_000)
    assert opp.contracts <= 7


def test_fractional_kelly_is_smaller_than_full() -> None:
    q = _quote(0.52, 0.52)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.10), q, home,
                   bankroll=10_000, kelly_multiplier=0.25)
    assert opp.stake_fraction < opp.kelly_fraction


# ── Reporting ─────────────────────────────────────────────────────────────────

def test_summary_reports_the_expected_loss_rate() -> None:
    """
    A portfolio of real edges on underdogs still loses most individual bets.
    Reporting only expected value invites reading a normal losing run as a
    broken strategy — or worse, chasing it.
    """
    q = _quote(0.35, 0.70)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.10), q, home, bankroll=10_000)
    s = summarize_expectations([opp])
    assert s["count"] == 1
    assert s["expected_value"] > 0
    assert s["expected_loss_rate"] > 0.5, "these lose more often than they win"


def test_empty_summary_is_safe() -> None:
    assert summarize_expectations([])["count"] == 0


# ── The in-progress guard ─────────────────────────────────────────────────────

def test_started_games_are_rejected_outright() -> None:
    """
    The most dangerous failure in this strategy. Kalshi prices a live game on
    its current state while the sharp feed is pregame, so after first pitch a
    team losing badly reads as a huge edge. Measured live, every apparent edge
    above 9% came from an in-progress game.
    """
    q = _quote(0.52, 0.52)
    q.commence_time = NOW - timedelta(minutes=40)
    home, _ = fair_probabilities(q)
    side = _side(ask=home - 0.20)
    assert evaluate(side, q, home, bankroll=10_000, now=NOW) is None


def test_games_about_to_start_are_rejected() -> None:
    """Too close to first pitch is the same problem with less warning."""
    q = _quote(0.52, 0.52)
    q.commence_time = NOW + timedelta(minutes=2)
    home, _ = fair_probabilities(q)
    assert evaluate(_side(ask=home - 0.20), q, home,
                    bankroll=10_000, now=NOW, min_minutes_to_start=5) is None


def test_pregame_edges_still_qualify() -> None:
    q = _quote(0.52, 0.52)
    q.commence_time = NOW + timedelta(hours=3)
    home, _ = fair_probabilities(q)
    opp = evaluate(_side(ask=home - 0.10), q, home, bankroll=10_000, now=NOW)
    assert opp is not None
