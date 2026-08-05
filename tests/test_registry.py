"""
The registry is a gate, so the tests are mostly about what it refuses.
"""

from arbengine.registry import (
    PAIRS,
    VerifiedPair,
    confirmed_pairs,
    is_tradeable,
    status_for,
)


def test_unknown_pairs_default_to_needs_review() -> None:
    """
    An unlisted mapping is not an approved one. Defaulting to tradeable would
    invert the whole point of the gate.
    """
    assert status_for("KXSOMETHING-ELSE") == "needs_review"
    assert is_tradeable("KXSOMETHING-ELSE") is False


def test_wildcard_prefix_matches_a_series() -> None:
    assert status_for("KXMLBGAME-26AUG072305ATLNYY-ATL") == "confirmed"
    assert is_tradeable("KXMLBGAME-26AUG072305ATLNYY-ATL") is True


def test_a_near_miss_prefix_does_not_match() -> None:
    """KXMLBGAME must not confer approval on KXMLBASGAME or KXNFLGAME."""
    assert is_tradeable("KXNFLGAME-26AUG07-KC") is False
    assert is_tradeable("KXMLBASGAME-26JUL14") is False


def test_needs_review_pairs_are_not_tradeable() -> None:
    p = VerifiedPair(
        pair_id="x", kalshi_ticker="KXTEST", polymarket_condition_id="0x",
        label="test", status="needs_review",
    )
    assert p.tradeable is False


def test_rejected_pairs_are_not_tradeable() -> None:
    p = VerifiedPair(
        pair_id="x", kalshi_ticker="KXTEST", polymarket_condition_id="0x",
        label="test", status="rejected",
    )
    assert p.tradeable is False


def test_confirmed_pairs_carry_a_review_date_and_note() -> None:
    """
    A confirmed pair without a recorded rationale is indistinguishable from one
    someone flipped without checking.
    """
    for p in confirmed_pairs():
        assert p.reviewed_at, f"{p.pair_id} confirmed with no review date"
        assert len(p.note) > 40, f"{p.pair_id} confirmed with no real rationale"


def test_known_divergences_are_recorded_where_they_exist() -> None:
    """
    Confirming a pair means the settlement rules were compared, not that they
    are identical. Postponements and rain-shortened games are real divergence
    risks on MLB and are recorded rather than assumed away.
    """
    mlb = next(p for p in PAIRS if p.pair_id == "mlb-game-moneyline")
    assert mlb.known_divergences
    blob = " ".join(mlb.known_divergences).lower()
    assert "postpon" in blob or "suspend" in blob
