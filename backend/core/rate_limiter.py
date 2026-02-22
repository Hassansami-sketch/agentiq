"""
core/rate_limiter.py
Dual-mode rate limiter — Redis-backed (production) with in-memory fallback (dev).

How it works:
  - Production (REDIS_URL set): uses Redis INCR with TTL per IP+minute bucket
    → works correctly across multiple Gunicorn/Uvicorn workers
  - Development (no Redis): falls back to in-memory defaultdict
    → works correctly for single-process dev server

Usage in main.py middleware:
    from core.rate_limiter import check_rate_limit

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        ip = request.client.host if request.client else "unknown"
        allowed, retry_after = await check_rate_limit(ip, settings.RATE_LIMIT_PER_MINUTE)
        if not allowed:
            return Response(
                content=f'{{"detail":"Rate limit exceeded — max {settings.RATE_LIMIT_PER_MINUTE} req/min"}}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

Common errors + fixes:
  redis.exceptions.ConnectionError → Redis URL wrong or Redis not running
    Fix: Check REDIS_URL in .env. Test: redis-cli -u $REDIS_URL ping
  Memory leak in dev mode:
    Fixed: old buckets are pruned in _cleanup_old_buckets()
"""
import time
import logging
from collections import defaultdict
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# ── In-memory fallback (single-process dev only) ──────────────────────────────
_mem_buckets: dict = defaultdict(int)


def _cleanup_old_buckets(ip: str) -> None:
    """Prune buckets older than 2 minutes to prevent unbounded memory growth."""
    current_min = int(time.time() // 60)
    stale = [k for k in list(_mem_buckets.keys())
             if k.startswith(ip) and int(k.split(":")[1]) < current_min - 2]
    for k in stale:
        _mem_buckets.pop(k, None)


def _mem_rate_check(ip: str, limit: int) -> Tuple[bool, int]:
    bucket = f"{ip}:{int(time.time() // 60)}"
    _mem_buckets[bucket] += 1
    _cleanup_old_buckets(ip)
    allowed = _mem_buckets[bucket] <= limit
    return allowed, 60 if not allowed else 0


# ── Redis-backed (multi-worker production) ────────────────────────────────────
_redis_client = None
_redis_failed = False  # don't spam logs after first failure


def _get_redis():
    global _redis_client, _redis_failed
    if _redis_client is not None:
        return _redis_client
    if _redis_failed:
        return None
    try:
        import redis.asyncio as aioredis
        from core.config import settings
        if not settings.REDIS_URL:
            return None
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=1,
            decode_responses=True,
        )
        logger.info("Rate limiter: Redis backend initialized")
        return _redis_client
    except Exception as e:
        _redis_failed = True
        logger.warning("Rate limiter: Redis unavailable (%s) — falling back to in-memory", e)
        return None


async def _redis_rate_check(ip: str, limit: int) -> Tuple[bool, int]:
    redis = _get_redis()
    if redis is None:
        return _mem_rate_check(ip, limit)
    try:
        key = f"rl:{ip}:{int(time.time() // 60)}"
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, 120)
        results = await pipe.execute()
        count = results[0]
        allowed = count <= limit
        return allowed, 60 if not allowed else 0
    except Exception as e:
        logger.warning("Redis rate check failed (%s) — falling back to in-memory", e)
        return _mem_rate_check(ip, limit)


async def check_rate_limit(ip: str, limit: int) -> Tuple[bool, int]:
    """
    Check if this IP is within the rate limit.
    Returns (allowed: bool, retry_after_seconds: int).

    Debug:
      # Manually check Redis bucket:
      redis-cli get "rl:127.0.0.1:<current_minute>"
    """
    return await _redis_rate_check(ip, limit)
