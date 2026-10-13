"""Redis-backed state: response cache, idempotency, rate limiting, token budgets."""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.logging_conf import logger

_redis: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global _redis
    settings = get_settings()
    if settings.redis_url:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis.ping()
        logger.info("redis connected", extra={"url": settings.redis_url})
    else:
        import fakeredis

        _redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        logger.warning("REDIS_URL not set; using in-process fakeredis")
    return _redis


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("redis not initialised")
    return _redis


async def close_redis() -> None:
    if _redis is not None:
        await _redis.aclose()


# ---------- Cache ----------

def cache_key(tenant_id: str, schema_name: str, text: str) -> str:
    # Tenant-scoped so one tenant can never read another's cached data.
    digest = hashlib.sha256(f"{schema_name}\x00{text}".encode()).hexdigest()
    return f"cache:{tenant_id}:{digest}"


async def cache_get(key: str) -> dict[str, Any] | None:
    raw = await get_redis().get(key)
    return json.loads(raw) if raw else None


async def cache_set(key: str, value: dict[str, Any]) -> None:
    await get_redis().set(key, json.dumps(value), ex=get_settings().cache_ttl_seconds)


# ---------- Idempotency ----------

async def idempotency_get(tenant_id: str, idem_key: str) -> dict[str, Any] | None:
    raw = await get_redis().get(f"idem:{tenant_id}:{idem_key}")
    return json.loads(raw) if raw else None


async def idempotency_set(tenant_id: str, idem_key: str, value: dict[str, Any]) -> None:
    await get_redis().set(
        f"idem:{tenant_id}:{idem_key}", json.dumps(value), ex=get_settings().idempotency_ttl_seconds
    )


async def idempotency_lock(tenant_id: str, idem_key: str, ttl: int = 60) -> bool:
    """Returns False if another request with the same key is in flight."""
    return bool(await get_redis().set(f"idem-lock:{tenant_id}:{idem_key}", "1", nx=True, ex=ttl))


async def idempotency_unlock(tenant_id: str, idem_key: str) -> None:
    await get_redis().delete(f"idem-lock:{tenant_id}:{idem_key}")


# ---------- Rate limiting (sliding window log) ----------

async def check_rate_limit(tenant_id: str, limit: int, window_s: int = 60) -> tuple[bool, int]:
    r = get_redis()
    key = f"rl:{tenant_id}"
    now = time.time()
    async with r.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, now - window_s)
        pipe.zadd(key, {f"{now}:{uuid.uuid4().hex}": now})
        pipe.zcard(key)
        pipe.expire(key, window_s)
        _, _, count, _ = await pipe.execute()
    return count <= limit, max(0, limit - count)


# ---------- Token budgets (per tenant, per UTC day) ----------

def _budget_key(tenant_id: str) -> str:
    return f"budget:{tenant_id}:{datetime.now(timezone.utc):%Y-%m-%d}"


async def tokens_used_today(tenant_id: str) -> int:
    return int(await get_redis().get(_budget_key(tenant_id)) or 0)


async def consume_tokens(tenant_id: str, tokens: int) -> int:
    r = get_redis()
    key = _budget_key(tenant_id)
    used = await r.incrby(key, tokens)
    await r.expire(key, 2 * 86400)
    return int(used)
