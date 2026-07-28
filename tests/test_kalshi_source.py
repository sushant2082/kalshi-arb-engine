import json
from pathlib import Path

from arbengine.source.kalshi import parse_book, quote_from_market

FIXTURE = Path(__file__).parent / "fixtures" / "orderbook.json"
FIXTURE_FP = Path(__file__).parent / "fixtures" / "orderbook_fp.json"
FIXTURE_MARKET = Path(__file__).parent / "fixtures" / "market.json"


# ── orderbook_fp: the format the live API actually returns ────────────────────

def test_fp_format_reads_the_best_bid_from_the_END_of_the_ladder() -> None:
    """
    orderbook_fp levels are sorted ASCENDING, so the best bid is the last entry.
    Reading index 0 instead yields the worst quote on the book — a plausible
    number that is silently wrong, which is the worst kind of bug here.
    """
    book = parse_book({
        "orderbook_fp": {
            "yes_dollars": [["0.3000", "50.00"], ["0.3100", "420.00"]],
            "no_dollars": [["0.6300", "1750.00"], ["0.6600", "380.00"]],
        }
    })
    assert book["bid"] == 0.31        # highest yes bid, not 0.30
    assert book["bid_size"] == 420
    assert book["ask"] == 0.34        # 1 - 0.66, not 1 - 0.63
    assert book["ask_size"] == 380


def test_fp_format_handles_empty_sides() -> None:
    book = parse_book({
        "orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.9900", "103732.00"]]}
    })
    assert book["bid"] is None
    assert book["bid_size"] == 0
    assert book["ask"] == 0.01
    assert book["ask_size"] == 103732


def test_fp_fixture_matches_the_markets_endpoint_quote() -> None:
    """
    The order book and the market metadata must agree on top of book. If they
    diverge, one of the two parsers is wrong and every downstream check is too.
    """
    book = parse_book(json.loads(FIXTURE_FP.read_text()))
    quote = quote_from_market(json.loads(FIXTURE_MARKET.read_text()))
    assert book["ask"] == quote["ask"]
    assert book["ask_size"] == quote["ask_size"]


# ── market metadata quotes ────────────────────────────────────────────────────

def test_quote_from_market_parses_string_numerics() -> None:
    quote = quote_from_market({
        "yes_bid_dollars": "0.4200", "yes_ask_dollars": "0.5500",
        "yes_bid_size_fp": "300.00", "yes_ask_size_fp": "200.00",
    })
    assert quote == {"bid": 0.42, "ask": 0.55, "bid_size": 300, "ask_size": 200}


def test_zero_size_quote_is_treated_as_absent() -> None:
    """
    Kalshi reports 0.00/0 for an empty side rather than omitting it. Treating
    that as a real $0.00 bid would let the LP "sell" into a book that isn't
    there and report free money.
    """
    quote = quote_from_market({
        "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0100",
        "yes_bid_size_fp": "0.00", "yes_ask_size_fp": "103732.00",
    })
    assert quote["bid"] is None
    assert quote["bid_size"] == 0
    assert quote["ask"] == 0.01


def test_missing_quote_fields_yield_none() -> None:
    quote = quote_from_market({})
    assert quote == {"bid": None, "ask": None, "bid_size": 0, "ask_size": 0}


# ── legacy cents format ───────────────────────────────────────────────────────


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
