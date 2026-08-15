"""
Construction smoke tests.

These exist because the whole suite once passed while KalshiClient was
un-constructible — a constructor parameter had been added to the wrong
signature, so every test still passed and the first real run died with a
NameError. Nothing had ever built the object.

Cheap to run, and they catch the class of error that unit tests structurally
miss: code that is never exercised because the tests stub around it.
"""

import inspect

import pytest

from arbengine.source.kalshi import KalshiClient
from arbengine.source.polymarket import PolymarketClient
from arbengine.source.polystream import PolymarketStream


class _FakeKey:
    """Stands in for an RSA key; construction must not touch it."""


def test_kalshi_client_constructs_with_defaults() -> None:
    c = KalshiClient("https://example.test", "wss://example.test", "kid", _FakeKey())
    assert c._request_cost > 0
    assert c._connect_retry_delay > 0
    assert c._connect_retry_max >= c._connect_retry_delay


def test_kalshi_every_constructor_param_is_assigned() -> None:
    """
    Guards the exact failure above: a parameter declared but never stored, or
    an attribute assigned from a name that is not a parameter.
    """
    sig = inspect.signature(KalshiClient.__init__)
    params = {p for p in sig.parameters if p != "self"}
    c = KalshiClient("https://example.test", "wss://example.test", "kid", _FakeKey())
    src = inspect.getsource(KalshiClient.__init__)
    for name in params:
        assert name in src, f"constructor parameter {name!r} is never referenced"
    assert c is not None


def test_polymarket_client_constructs() -> None:
    c = PolymarketClient()
    assert c.PAGE_SIZE == 100


def test_polymarket_stream_constructs_and_dedupes() -> None:
    s = PolymarketStream(["a", "b", "a"])
    assert s.asset_ids == ["a", "b"]
    assert set(s.books) == {"a", "b"}
    assert s.quotes() == {}, "no books until the feed delivers one"


def test_client_factory_wires_settings_through() -> None:
    """The CLI's _client() must pass config into the client, not defaults."""
    from arbengine.config import Settings
    from arbengine.main import _client

    s = Settings(
        kalshi_api_key_id="test",
        kalshi_read_budget=600.0,
        kalshi_request_cost=25.0,
        kalshi_rate_safety=0.8,
    )
    c = _client(s, _FakeKey())
    assert c._request_cost == 25.0
    assert c._bucket.refill_rate == pytest.approx(600.0 * 0.8)
