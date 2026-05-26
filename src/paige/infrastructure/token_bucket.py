"""TokenBucket — async-friendly token-bucket rate limiter.

Refills at `rate_per_sec` tokens/second, capped at `burst`. Each
caller `await bucket.acquire(n=1)`s before doing rate-limited work;
the call sleeps just long enough for `n` tokens to be available,
then deducts them.

Why a bucket: smooth average rate AND tolerate brief spikes up to
`burst`. Plain "sleep 1/N seconds between calls" can't tolerate
spikes; pure `asyncio.Semaphore(N)` doesn't refill.

Lock-free is fine because asyncio is single-threaded — the
acquire path reads, sleeps, then writes without an intervening
await between the math and the deduction (we re-derive on wake).

Used by `paige.adapters.feishu.lark_client.LarkClientWrapper` to
stay under Feishu's 50/s app-wide cap.
"""

from __future__ import annotations

import asyncio
from time import monotonic


class TokenBucket:
    """`rate_per_sec` tokens replenished per second, max `burst`.

    Construct once and share across coroutines that should compete
    for the same budget.
    """

    __slots__ = ("_rate", "_burst", "_tokens", "_last")

    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be positive: {rate_per_sec}")
        self._rate = float(rate_per_sec)
        self._burst = float(burst) if burst is not None else float(rate_per_sec)
        if self._burst < 1.0:
            raise ValueError(f"burst must be >= 1: {self._burst}")
        # Start full so the first burst doesn't pay a refill wait.
        self._tokens: float = self._burst
        self._last: float = monotonic()

    async def acquire(self, n: float = 1.0) -> None:
        """Block until `n` tokens are available, then deduct them.

        Calling with `n > burst` would block forever; we raise
        instead — the caller is asking for more tokens than the
        bucket can ever hold.
        """
        if n > self._burst:
            raise ValueError(f"acquire(n={n}) exceeds bucket burst {self._burst}")
        while True:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return
            # How long until enough tokens accrue.
            deficit = n - self._tokens
            wait = deficit / self._rate
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        """Add tokens earned since the last update. Cap at burst."""
        now = monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)

    @property
    def tokens(self) -> float:
        """Current token balance — useful for tests + diagnostics."""
        self._refill()
        return self._tokens


__all__ = ["TokenBucket"]
