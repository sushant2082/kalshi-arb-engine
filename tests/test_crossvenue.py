"""
The invariant here is refusal, not detection. Calling two different questions
"the same" is the error that costs real money on a cross-venue position, so
most of these tests check that the matcher declines.
"""

from datetime import datetime, timedelta, timezone

import pytest

from arbengine.crossvenue import (
    DEFAULT_HAIRCUTS,
    MatchedPair,
    ResolutionRisk,
    Subject,
    VenueQuote,
    assess_risk,
    detect_cross_venue,
    extract_asset,
    extract_direction,
    extract_threshold,
    subject_from_text,
)

NOW = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
DEADLINE = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)


def _q(venue, ticker, yes_ask, no_ask, size=100, fee_yes=0.0, fee_no=0.0, at=NOW):
    return VenueQuote(
        venue=venue, ticker=ticker, yes_ask=yes_ask, no_ask=no_ask,
        yes_ask_size=size, no_ask_size=size,
        fee_yes=fee_yes, fee_no=fee_no, fetched_at=at,
    )


def _pair(risk=ResolutionRisk.MECHANICAL, k=None, p=None):
    return MatchedPair(
        kalshi=k or _q("kalshi", "KXBTCD-T110000", 0.45, 0.56),
        polymarket=p or _q("polymarket", "btc-110k", 0.47, 0.52),
        risk=risk, rationale="test",
    )


# ── Text extraction ───────────────────────────────────────────────────────────

def test_asset_extraction() -> None:
    assert extract_asset("Will Bitcoin be above $110,000?") == "BTC"
    assert extract_asset("ETH above 4000") == "ETH"
    assert extract_asset("Will it rain in Paris?") is None


def test_direction_extraction() -> None:
    assert extract_direction("BTC above $110,000") == "above"
    assert extract_direction("BTC below $110,000") == "below"
    assert extract_direction("BTC at $110,000") is None


def test_threshold_extraction_requires_one_unambiguous_number() -> None:
    assert extract_threshold("above $110,000") == 110000.0
    assert extract_threshold("above 110k") == 110000.0
    # Two candidate numbers is ambiguous: refuse rather than guess.
    assert extract_threshold("between 105,000 and 110,000") is None
    assert extract_threshold("no numbers here") is None


# ── Risk assessment: the refusals ─────────────────────────────────────────────

def test_different_thresholds_are_never_matched() -> None:
    """
    A $110k contract hedged against a $105k contract is not a hedge — it is a
    spread with a hole in the middle where both legs lose.
    """
    risk, why = assess_risk(
        "BTC above $110,000 at 8pm", "Will BTC be above $105,000 at 8pm?",
        Subject("BTC", 110000.0, "above", DEADLINE),
        Subject("BTC", 105000.0, "above", DEADLINE),
    )
    assert risk is ResolutionRisk.UNKNOWN
    assert "differ" in why


def test_different_settlement_times_are_never_matched() -> None:
    risk, why = assess_risk(
        "BTC above $110,000", "Will BTC be above $110,000?",
        Subject("BTC", 110000.0, "above", DEADLINE),
        Subject("BTC", 110000.0, "above", DEADLINE + timedelta(hours=1)),
    )
    assert risk is ResolutionRisk.UNKNOWN
    assert "settlement times differ" in why


def test_opposite_directions_are_never_matched() -> None:
    risk, _ = assess_risk(
        "BTC above $110,000", "Will BTC be below $110,000?",
        Subject("BTC", 110000.0, "above", DEADLINE),
        Subject("BTC", 110000.0, "below", DEADLINE),
    )
    assert risk is ResolutionRisk.UNKNOWN


def test_missing_deadline_is_never_matched() -> None:
    risk, why = assess_risk(
        "BTC above $110,000", "Will BTC be above $110,000?",
        Subject("BTC", 110000.0, "above", None),
        Subject("BTC", 110000.0, "above", DEADLINE),
    )
    assert risk is ResolutionRisk.UNKNOWN
    assert "deadline" in why


def test_edge_case_wording_downgrades_to_divergent() -> None:
    """Void/postpone/tie language is where two rulebooks actually part ways."""
    risk, why = assess_risk(
        "BTC above $110,000. If the event is postponed, the market voids.",
        "Will BTC be above $110,000?",
        Subject("BTC", 110000.0, "above", DEADLINE),
        Subject("BTC", 110000.0, "above", DEADLINE),
    )
    assert risk is ResolutionRisk.DIVERGENT
    assert "postpone" in why


def test_matching_crypto_thresholds_are_mechanical() -> None:
    risk, _ = assess_risk(
        "BTC above $110,000 at 8pm ET", "Will BTC be above $110,000 at 8pm ET?",
        Subject("BTC", 110000.0, "above", DEADLINE),
        Subject("BTC", 110000.0, "above", DEADLINE),
    )
    assert risk is ResolutionRisk.MECHANICAL


# ── Detection ─────────────────────────────────────────────────────────────────

def test_unknown_risk_never_produces_an_opportunity() -> None:
    """UNKNOWN means we could not prove the questions match. Never tradeable."""
    assert detect_cross_venue(_pair(ResolutionRisk.UNKNOWN), NOW) is None


def test_detects_a_gap_and_reports_it_net_of_the_haircut() -> None:
    # Buy YES on Kalshi at 0.45, NO on Polymarket at 0.52 → cost 0.97.
    opp = detect_cross_venue(_pair(ResolutionRisk.MECHANICAL), NOW)
    assert opp is not None
    assert opp.cost_per_set == pytest.approx(0.97)
    assert opp.gross_profit_per_set == pytest.approx(0.03)
    assert opp.net_profit_per_set == pytest.approx(
        0.03 - DEFAULT_HAIRCUTS[ResolutionRisk.MECHANICAL]
    )


def test_a_cross_venue_position_is_never_reported_as_riskless() -> None:
    opp = detect_cross_venue(_pair(), NOW)
    assert opp is not None
    assert opp.is_riskless is False


def test_haircut_can_erase_a_thin_gap() -> None:
    """
    A 2-cent gap on a DIVERGENT pair is not worth a 5% chance of losing $1.
    The haircut has to be able to reject, or it is decoration.
    """
    k = _q("kalshi", "K", 0.48, 0.53)
    p = _q("polymarket", "P", 0.50, 0.50)
    assert detect_cross_venue(
        _pair(ResolutionRisk.MECHANICAL, k, p), NOW
    ) is not None
    assert detect_cross_venue(
        _pair(ResolutionRisk.DIVERGENT, k, p), NOW
    ) is None


def test_fees_are_charged_before_flagging() -> None:
    k = _q("kalshi", "K", 0.45, 0.56, fee_yes=0.02, fee_no=0.02)
    p = _q("polymarket", "P", 0.47, 0.52, fee_yes=0.02, fee_no=0.02)
    opp = detect_cross_venue(_pair(ResolutionRisk.MECHANICAL, k, p), NOW)
    assert opp is None, "a 3-cent gap cannot survive 4 cents of fees"


def test_stale_quotes_are_rejected() -> None:
    """
    Two independent venues drift apart faster than one. A skewed pair compares
    two different moments and manufactures a gap that never existed.
    """
    stale = _q("kalshi", "K", 0.45, 0.56, at=NOW - timedelta(minutes=5))
    pair = _pair(ResolutionRisk.MECHANICAL, k=stale)
    assert detect_cross_venue(pair, NOW, max_quote_age_sec=30) is None


def test_fillable_size_is_the_thinner_leg() -> None:
    k = _q("kalshi", "K", 0.45, 0.56, size=250)
    p = _q("polymarket", "P", 0.47, 0.52, size=17)
    opp = detect_cross_venue(_pair(ResolutionRisk.MECHANICAL, k, p), NOW)
    assert opp.fillable_sets == 17


def test_picks_the_cheaper_direction() -> None:
    """Both orientations are legal; the better one should win."""
    k = _q("kalshi", "K", 0.80, 0.21)   # cheap NO
    p = _q("polymarket", "P", 0.15, 0.86)  # cheap YES
    opp = detect_cross_venue(_pair(ResolutionRisk.MECHANICAL, k, p), NOW)
    assert opp is not None
    assert opp.yes_venue == "polymarket"  # 0.15 + 0.21 = 0.36
    assert opp.cost_per_set == pytest.approx(0.36)


def test_coherent_cross_venue_prices_do_not_fire() -> None:
    k = _q("kalshi", "K", 0.50, 0.51)
    p = _q("polymarket", "P", 0.50, 0.51)
    assert detect_cross_venue(_pair(ResolutionRisk.MECHANICAL, k, p), NOW) is None
