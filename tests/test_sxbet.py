"""
SX Bet is peer-to-peer: makers post the side THEY back, so a taker pays the
complement. These tests exist mostly to pin that inversion — reading an order
as an offer to sell you that outcome quotes every market at its mirror image,
which is plausible-looking and completely wrong.
"""

from datetime import datetime, timezone

import pytest

from arbengine.source.sxbet import (
    ODDS_SCALE,
    SxQuote,
    detect_within_market,
    quote_from_orders,
    taker_capacity,
    taker_price,
)

NOW = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)


def _order(backs_one: bool, odds: float, size: float, filled: float = 0.0) -> dict:
    return {
        "isMakerBettingOutcomeOne": backs_one,
        "percentageOdds": str(int(odds * ODDS_SCALE)),
        "totalBetSize": str(int(size * 1e6)),
        "fillAmount": str(int(filled * 1e6)),
        "orderStatus": "ACTIVE",
    }


MARKET = {
    "marketHash": "0xabc",
    "outcomeOneName": "Atlanta Braves",
    "outcomeTwoName": "Washington Nationals",
    "leagueLabel": "MLB",
    "teamOneName": "Atlanta Braves",
    "teamTwoName": "Washington Nationals",
    "gameTime": 1785700000,
}


# ── The inversion ─────────────────────────────────────────────────────────────

def test_taker_pays_the_complement_of_the_maker_odds() -> None:
    """A maker backing their side at 59.75% leaves the taker paying 40.25%."""
    assert taker_price(int(0.5975 * ODDS_SCALE)) == pytest.approx(0.4025)


def test_buying_an_outcome_requires_a_maker_on_the_OTHER_side() -> None:
    """
    The crossed lookup is the whole point. A maker backing outcome one creates
    liquidity to buy outcome TWO, not outcome one.
    """
    q = quote_from_orders(MARKET, [_order(True, 0.5975, 1840)], NOW)
    assert q.two_ask == pytest.approx(0.4025)
    assert q.one_ask is None, "a maker on outcome one does not let you buy outcome one"


def test_best_price_is_the_cheapest_takeable_not_the_first_seen() -> None:
    orders = [
        _order(False, 0.3700, 460),    # -> buy outcome one at 0.63
        _order(False, 0.3788, 699),    # -> buy outcome one at 0.6212, better
        _order(True, 0.5925, 926),     # -> buy outcome two at 0.4075
        _order(True, 0.5988, 1277),    # -> buy outcome two at 0.4012, better
    ]
    q = quote_from_orders(MARKET, orders, NOW)
    assert q.one_ask == pytest.approx(0.6212, abs=1e-4)
    assert q.two_ask == pytest.approx(0.4012, abs=1e-4)


def test_a_normal_book_has_an_overround_above_one() -> None:
    """Sanity check on the direction of the conversion."""
    orders = [_order(False, 0.3788, 699), _order(True, 0.5988, 1277)]
    q = quote_from_orders(MARKET, orders, NOW)
    assert q.overround > 1.0


# ── Size conversion ───────────────────────────────────────────────────────────

def test_taker_capacity_scales_by_the_odds_ratio() -> None:
    """
    The maker risks their stake to win the taker's. At 59.75% the taker's
    maximum stake is size * (1-p)/p, well below the maker's size. Using the
    maker's number directly overstates fillable volume on every favourite.
    """
    cap = taker_capacity(int(1840 * 1e6), int(0.5975 * ODDS_SCALE))
    assert cap == pytest.approx(1840 * 0.4025 / 0.5975, rel=1e-9)
    assert cap < 1840


def test_taker_capacity_exceeds_maker_size_on_an_underdog() -> None:
    cap = taker_capacity(int(100 * 1e6), int(0.25 * ODDS_SCALE))
    assert cap == pytest.approx(300.0, rel=1e-9)


def test_partially_filled_orders_only_offer_the_remainder() -> None:
    q = quote_from_orders(MARKET, [_order(True, 0.50, 1000, filled=900)], NOW)
    assert q.two_ask == pytest.approx(0.50)
    assert q.two_ask_size == pytest.approx(100.0, rel=1e-9)


def test_fully_filled_orders_are_ignored() -> None:
    q = quote_from_orders(MARKET, [_order(True, 0.50, 1000, filled=1000)], NOW)
    assert q.two_ask is None


def test_size_accumulates_across_orders_at_the_same_price() -> None:
    q = quote_from_orders(
        MARKET, [_order(True, 0.50, 100), _order(True, 0.50, 250)], NOW
    )
    assert q.two_ask_size == pytest.approx(350.0, rel=1e-9)


def test_degenerate_odds_are_skipped() -> None:
    q = quote_from_orders(
        MARKET,
        [{"isMakerBettingOutcomeOne": True, "percentageOdds": "0",
          "totalBetSize": "1000000", "fillAmount": "0"}],
        NOW,
    )
    assert q.two_ask is None


# ── Within-market arbitrage ───────────────────────────────────────────────────

def test_detects_a_sub_dollar_book() -> None:
    """
    Exactly one outcome occurs, so both sides for under $1 is a lock — and with
    no taker fee the gross gap is the net gap.
    """
    orders = [_order(False, 0.55, 1000), _order(True, 0.55, 1000)]
    q = quote_from_orders(MARKET, orders, NOW)
    assert q.overround == pytest.approx(0.90)

    hit = detect_within_market(q)
    assert hit is not None
    assert hit["profit_per_set"] == pytest.approx(0.10)
    assert hit["fillable"] > 0


def test_a_normal_vig_book_does_not_fire() -> None:
    orders = [_order(False, 0.3788, 699), _order(True, 0.5988, 1277)]
    q = quote_from_orders(MARKET, orders, NOW)
    assert detect_within_market(q) is None


def test_one_sided_book_cannot_lock() -> None:
    q = quote_from_orders(MARKET, [_order(True, 0.55, 1000)], NOW)
    assert detect_within_market(q) is None


def test_game_time_is_parsed_to_utc() -> None:
    q = quote_from_orders(MARKET, [_order(True, 0.5, 100)], NOW)
    assert q.game_time is not None
    assert q.game_time.tzinfo is timezone.utc
