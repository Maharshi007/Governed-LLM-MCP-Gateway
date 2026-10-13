"""Redis-backed circuit breaker, shared across all gateway replicas.

CLOSED    -> calls flow; consecutive failures are counted.
OPEN      -> calls are rejected instantly until reset timeout elapses.
HALF_OPEN -> exactly one probe call is let through; success closes, failure re-opens.
"""

import time
from enum import Enum

from app.config import get_settings
from app.metrics import BREAKER_OPEN
from app.store import get_redis


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, provider: str):
        self.provider = provider
        s = get_settings()
        self.threshold = s.breaker_failure_threshold
        self.reset_seconds = s.breaker_reset_seconds
        self._fail_key = f"cb:{provider}:failures"
        self._open_key = f"cb:{provider}:open_until"
        self._probe_key = f"cb:{provider}:probe"

    async def state(self) -> BreakerState:
        open_until = await get_redis().get(self._open_key)
        if open_until is None:
            return BreakerState.CLOSED
        return BreakerState.OPEN if time.time() < float(open_until) else BreakerState.HALF_OPEN

    async def allow_request(self) -> bool:
        state = await self.state()
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN:
            # Only one replica/request gets to probe the recovering provider.
            return bool(await get_redis().set(self._probe_key, "1", nx=True, ex=self.reset_seconds))
        return False

    async def record_success(self) -> None:
        await get_redis().delete(self._fail_key, self._open_key, self._probe_key)
        BREAKER_OPEN.labels(self.provider).set(0)

    async def record_failure(self) -> None:
        r = get_redis()
        if await self.state() is BreakerState.HALF_OPEN:
            await self._trip()
            return
        failures = await r.incr(self._fail_key)
        await r.expire(self._fail_key, self.reset_seconds * 4)
        if failures >= self.threshold:
            await self._trip()

    async def _trip(self) -> None:
        r = get_redis()
        await r.set(self._open_key, str(time.time() + self.reset_seconds))
        await r.delete(self._fail_key, self._probe_key)
        BREAKER_OPEN.labels(self.provider).set(1)
