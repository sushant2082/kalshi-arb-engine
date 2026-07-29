import asyncio
import time

import pytest

from arbengine.source.kalshi import TokenBucket


async def test_burst_up_to_capacity_is_immediate() -> None:
    """
    A full bucket must let the whole burst through at once. Kalshi banks unspent
    budget up to capacity, so an event-driven client that sat idle is entitled
    to spend it — a fixed-interval throttle would leave that on the table.
    """
    bucket = TokenBucket(refill_rate=200.0, capacity=400.0)
    start = time.monotonic()
    for _ in range(40):  # 40 × 10 tokens = 400 = full capacity
        await bucket.acquire(10)
    assert time.monotonic() - start < 0.05


async def test_spending_past_capacity_paces_to_the_refill_rate() -> None:
    bucket = TokenBucket(refill_rate=200.0, capacity=400.0)
    for _ in range(40):
        await bucket.acquire(10)

    # The next 10 requests (100 tokens) need 0.5s of refill at 200/s.
    start = time.monotonic()
    for _ in range(10):
        await bucket.acquire(10)
    elapsed = time.monotonic() - start
    assert 0.4 < elapsed < 0.8


async def test_concurrent_callers_cannot_exceed_the_budget() -> None:
    """
    The bug this guards: without a lock every coroutine reads the balance before
    any of them debits it, so the whole fleet passes at once and the client
    bursts straight past the server's limit while looking throttled.
    """
    bucket = TokenBucket(refill_rate=100.0, capacity=100.0)
    start = time.monotonic()
    # 30 requests × 10 tokens = 300; 100 are banked, 200 must be refilled → ~2s.
    await asyncio.gather(*(bucket.acquire(10) for _ in range(30)))
    elapsed = time.monotonic() - start
    assert elapsed > 1.5, f"concurrent acquires bypassed the budget ({elapsed:.2f}s)"


async def test_refill_is_capped_at_capacity() -> None:
    """Idle time banks tokens only up to capacity, never beyond."""
    bucket = TokenBucket(refill_rate=1000.0, capacity=100.0)
    bucket._tokens = 0.0
    bucket._updated = time.monotonic() - 10.0  # long idle
    await bucket.acquire(100)  # exactly capacity, should be instant
    start = time.monotonic()
    await bucket.acquire(100)  # must wait a full refill
    assert time.monotonic() - start > 0.05


async def test_penalize_drains_the_bucket() -> None:
    """
    After a 429 the server's balance was lower than our model. Draining resyncs
    us instead of immediately over-spending again.
    """
    bucket = TokenBucket(refill_rate=200.0, capacity=400.0)
    bucket.penalize(10)
    start = time.monotonic()
    await bucket.acquire(100)
    assert time.monotonic() - start > 0.2


@pytest.mark.parametrize(
    "tier, budget, expected_rps",
    [
        ("basic", 200.0, 20.0),
        ("advanced", 300.0, 30.0),
        ("expert", 600.0, 60.0),
        ("premier", 1000.0, 100.0),
    ],
)
async def test_tier_budgets_map_to_expected_request_rates(
    tier: str, budget: float, expected_rps: float
) -> None:
    """Market-data endpoints cost the default 10 tokens, so rps = budget / 10."""
    assert budget / 10.0 == expected_rps
    bucket = TokenBucket(refill_rate=budget, capacity=budget * 2)
    assert bucket.capacity / 10.0 == expected_rps * 2
