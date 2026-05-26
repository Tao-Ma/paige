"""TokenBucket — async refill timing, burst, acquire-blocks."""

from __future__ import annotations

import asyncio

import pytest

from paige.infrastructure.token_bucket import TokenBucket

# ── construction ────────────────────────────────────────────────


def test_starts_full_at_burst() -> None:
    """Bursty start so an idle bot can fire up to `burst` immediately
    without waiting for the bucket to fill."""
    b = TokenBucket(rate_per_sec=10, burst=5)
    assert b.tokens == pytest.approx(5.0)


def test_burst_defaults_to_rate_per_sec() -> None:
    b = TokenBucket(rate_per_sec=7)
    assert b.tokens == pytest.approx(7.0)


def test_zero_rate_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(rate_per_sec=0)


def test_negative_rate_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(rate_per_sec=-1)


def test_burst_below_one_raises() -> None:
    with pytest.raises(ValueError, match="burst"):
        TokenBucket(rate_per_sec=5, burst=0.5)


# ── acquire (no blocking case) ──────────────────────────────────


async def test_acquire_within_burst_does_not_block() -> None:
    """When tokens >= n, acquire returns immediately (no wait)."""
    b = TokenBucket(rate_per_sec=10, burst=5)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await b.acquire(3)
    elapsed = loop.time() - start
    assert elapsed < 0.05  # essentially instant
    assert b.tokens == pytest.approx(2.0, abs=0.1)


async def test_acquire_drains_then_refills() -> None:
    b = TokenBucket(rate_per_sec=20, burst=2)
    await b.acquire()
    await b.acquire()
    # Bucket is now empty (or close to it).
    assert b.tokens < 0.2
    # Wait long enough for ~2 tokens to refill at 20/s.
    await asyncio.sleep(0.15)
    assert b.tokens >= 1.0


# ── acquire (blocking case) ─────────────────────────────────────


async def test_acquire_blocks_until_token_available() -> None:
    """Empty bucket → next acquire waits ~1/rate seconds."""
    b = TokenBucket(rate_per_sec=20, burst=1)  # 50ms per token
    await b.acquire()  # drains bucket to 0
    loop = asyncio.get_running_loop()
    start = loop.time()
    await b.acquire()
    elapsed = loop.time() - start
    # Should have waited roughly 1/20 = 0.05s; allow slack.
    assert 0.03 < elapsed < 0.20


async def test_serialized_acquires_smooth_to_rate() -> None:
    """N acquires after the burst is spent take ~ (N-burst) / rate
    seconds in aggregate."""
    rate = 50.0  # 20ms per token
    burst = 1.0
    b = TokenBucket(rate_per_sec=rate, burst=burst)
    n = 4  # 1 free + 3 paid
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(n):
        await b.acquire()
    elapsed = loop.time() - start
    expected = (n - burst) / rate
    # Allow generous slack for asyncio scheduling jitter.
    assert expected * 0.7 < elapsed < expected * 2.5


# ── over-burst guard ────────────────────────────────────────────


async def test_acquire_more_than_burst_raises() -> None:
    """Asking for more tokens than the bucket can ever hold would
    block forever; we catch it explicitly."""
    b = TokenBucket(rate_per_sec=10, burst=5)
    with pytest.raises(ValueError, match="exceeds bucket burst"):
        await b.acquire(10)


# ── refill cap ──────────────────────────────────────────────────


async def test_idle_does_not_overflow_burst() -> None:
    """A long idle period shouldn't let tokens exceed `burst`."""
    b = TokenBucket(rate_per_sec=100, burst=5)
    await b.acquire(5)
    await asyncio.sleep(0.20)  # at 100/s → would be 20 tokens uncapped
    assert b.tokens == pytest.approx(5.0, abs=0.5)
