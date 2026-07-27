import json
from pathlib import Path

from arbengine.source.kalshi import parse_book

FIXTURE = Path(__file__).parent / "fixtures" / "orderbook.json"


def test_yes_ask_is_derived_from_the_no_bid() -> None:
    """
    Kalshi publishes two bid ladders and no ask ladder. A resting NO bid at 45c
    is an offer to sell YES at 55c. Getting this inversion backwards silently
    turns every arbitrage check into nonsense, so it is pinned here.
    """
    book = parse_book({
        "orderbook": {
            "yes": [[42, 300], [41, 150]],
            "no": [[45, 200], [44, 90]],
        }
    })
    assert book["bid"] == 0.42        # best YES bid: where you can sell YES
    assert book["bid_size"] == 300
    assert book["ask"] == 0.55        # 100 - 45: where you can buy YES
    assert book["ask_size"] == 200    # size resting on that NO bid


def test_bid_never_exceeds_ask_in_a_sane_book() -> None:
    book = parse_book({
        "orderbook": {"yes": [[42, 300]], "no": [[45, 200]]}
    })
    assert book["bid"] < book["ask"]


def test_empty_side_yields_none_and_zero_size() -> None:
    book = parse_book({"orderbook": {"yes": [], "no": [[45, 200]]}})
    assert book["bid"] is None
    assert book["bid_size"] == 0
    assert book["ask"] == 0.55

    book = parse_book({"orderbook": {"yes": [[42, 10]], "no": []}})
    assert book["ask"] is None
    assert book["ask_size"] == 0


def test_completely_empty_book() -> None:
    book = parse_book({"orderbook": {}})
    assert book == {"bid": None, "ask": None, "bid_size": 0, "ask_size": 0}
    assert parse_book({}) == {"bid": None, "ask": None, "bid_size": 0, "ask_size": 0}


def test_crossed_book_is_reported_as_quoted() -> None:
    """
    A YES bid of 60 with a NO bid of 45 implies YES bid 0.60 > YES ask 0.55.
    That IS the arbitrage signal, so the parser must pass it through untouched
    rather than "correcting" it.
    """
    book = parse_book({
        "orderbook": {"yes": [[60, 100]], "no": [[45, 100]]}
    })
    assert book["bid"] == 0.60
    assert book["ask"] == 0.55
    assert book["bid"] > book["ask"]


def test_parses_captured_fixture() -> None:
    data = json.loads(FIXTURE.read_text())
    book = parse_book(data)
    assert 0.0 <= book["ask"] <= 1.0
    assert 0.0 <= book["bid"] <= 1.0
    assert book["bid_size"] > 0
    assert book["ask_size"] > 0
