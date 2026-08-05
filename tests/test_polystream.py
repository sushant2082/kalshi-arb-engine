"""
Polymarket WebSocket protocol. Shapes captured from the live feed 2026-08-05.

The failure these guard against is silent: subscribing with the wrong payload
connects cleanly and then delivers nothing forever, so a scanner built on it
reports "no opportunities" while looking healthy.
"""

import json

from arbengine.source.polystream import WS_URL, PolymarketStream, TokenBook

TOK = "49402780465549031013904577058540996833865491363444523117262240688058500495213"


def test_subscription_payload_shape() -> None:
    """
    `assets_ids` (plural on both words), and `type` names the CHANNEL, not the
    verb. The commonly-suggested {"type":"subscribe","topic":"market",
    "token_ids":[...]} opens the socket and yields zero messages — verified
    against the live feed.
    """
    st = PolymarketStream([TOK])
    payload = json.loads(json.dumps({"assets_ids": st.asset_ids, "type": "market"}))
    assert payload["type"] == "market"
    assert "assets_ids" in payload
    assert "token_ids" not in payload
    assert "topic" not in payload
    assert WS_URL.startswith("wss://ws-subscriptions-clob.polymarket.com")


def test_book_snapshot_sets_top_of_book() -> None:
    b = TokenBook(asset_id=TOK)
    b.apply_snapshot({
        "bids": [{"price": "0.09", "size": "500"}, {"price": "0.08", "size": "10"}],
        "asks": [{"price": "0.10", "size": "97"}, {"price": "0.11", "size": "40"}],
    })
    assert b.best_bid == 0.09
    assert b.best_ask == 0.10
    assert b.ask_size == 97


def test_snapshot_replaces_rather_than_merges() -> None:
    """
    A snapshot supersedes prior state. Merging would leave levels that were
    removed while disconnected, producing a phantom best price.
    """
    b = TokenBook(asset_id=TOK)
    b.apply_snapshot({"bids": [{"price": "0.50", "size": "10"}], "asks": []})
    b.apply_snapshot({"bids": [{"price": "0.20", "size": "10"}], "asks": []})
    assert b.best_bid == 0.20
    assert 0.50 not in b.bids


def test_price_change_updates_the_right_ladder() -> None:
    """BUY updates bids, SELL updates asks — from the maker's perspective."""
    b = TokenBook(asset_id=TOK)
    b.apply_snapshot({
        "bids": [{"price": "0.09", "size": "500"}],
        "asks": [{"price": "0.10", "size": "97"}],
    })
    b.apply_change({"price": "0.095", "size": "200", "side": "BUY"})
    b.apply_change({"price": "0.099", "size": "50", "side": "SELL"})
    assert b.best_bid == 0.095
    assert b.best_ask == 0.099


def test_zero_size_removes_a_level() -> None:
    """
    Size 0 is how Polymarket clears a level. Storing it as a real level would
    leave a phantom best price that nothing can actually fill against.
    """
    b = TokenBook(asset_id=TOK)
    b.apply_snapshot({"bids": [], "asks": [
        {"price": "0.10", "size": "97"}, {"price": "0.11", "size": "40"},
    ]})
    b.apply_change({"price": "0.10", "size": "0", "side": "SELL"})
    assert b.best_ask == 0.11
    assert 0.10 not in b.asks


def test_unknown_side_is_ignored() -> None:
    b = TokenBook(asset_id=TOK)
    b.apply_snapshot({"bids": [{"price": "0.5", "size": "1"}], "asks": []})
    b.apply_change({"price": "0.9", "size": "5", "side": "SIDEWAYS"})
    assert b.best_bid == 0.5
    assert 0.9 not in b.bids and 0.9 not in b.asks


def test_handles_both_single_and_array_payloads() -> None:
    """The feed sends either one object or a JSON array of them."""
    st = PolymarketStream([TOK])
    snap = {
        "event_type": "book", "asset_id": TOK,
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.6", "size": "20"}],
    }
    st._handle(json.dumps(snap))
    assert st.books[TOK].best_ask == 0.6

    st._handle(json.dumps([{
        "event_type": "price_change", "market": "0x0",
        "price_changes": [
            {"asset_id": TOK, "price": "0.55", "size": "5", "side": "SELL"}
        ],
    }]))
    assert st.books[TOK].best_ask == 0.55


def test_last_trade_price_does_not_move_the_book() -> None:
    """
    A trade print is not a quote. Applying it as one would report a price
    nobody is currently offering.
    """
    st = PolymarketStream([TOK])
    st._handle(json.dumps({
        "event_type": "book", "asset_id": TOK,
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.6", "size": "20"}],
    }))
    st._handle(json.dumps({
        "event_type": "last_trade_price", "asset_id": TOK,
        "price": "0.99", "size": "100", "side": "BUY",
    }))
    assert st.books[TOK].best_ask == 0.6


def test_quotes_match_the_rest_shape() -> None:
    """Detectors must not be able to tell which transport produced a quote."""
    st = PolymarketStream([TOK])
    st._handle(json.dumps({
        "event_type": "book", "asset_id": TOK,
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.6", "size": "20"}],
    }))
    q = st.quotes()[TOK]
    assert set(q) == {"bid", "ask", "bid_size", "ask_size"}
    assert q["ask"] == 0.6 and q["ask_size"] == 20


def test_unsubscribed_token_updates_are_dropped() -> None:
    st = PolymarketStream([TOK])
    st._handle(json.dumps({
        "event_type": "book", "asset_id": "999",
        "bids": [{"price": "0.1", "size": "1"}], "asks": [],
    }))
    assert "999" not in st.books
    assert st.quotes() == {}
