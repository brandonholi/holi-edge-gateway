import logging
from redis.asyncio import Redis, ConnectionPool
from ..core.config import settings

_logger = logging.getLogger(__name__)

_cache_pool: ConnectionPool | None = None
_state_pool: ConnectionPool | None = None


def init_redis_pools():
    """Initializes the two physically separated Redis connection pools."""
    global _cache_pool, _state_pool

    _logger.info("Initializing Redis Cache Pool (holi-cache)")
    _cache_pool = ConnectionPool.from_url(
        settings.REDIS_CACHE_URL,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=30,
        retry_on_timeout=True,
        decode_responses=True,
    )

    _logger.info("Initializing Redis State Pool (holi-estado)")
    _state_pool = ConnectionPool.from_url(
        settings.REDIS_STATE_URL,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=30,
        retry_on_timeout=True,
        decode_responses=True,
    )


async def close_redis_pools():
    """Closes all Redis pools on shutdown."""
    global _cache_pool, _state_pool
    if _cache_pool:
        await _cache_pool.disconnect()
    if _state_pool:
        await _state_pool.disconnect()


def get_cache_redis() -> Redis:
    """Returns Redis client for holi-cache (read operations)."""
    global _cache_pool
    if _cache_pool is None:
        init_redis_pools()
    return Redis(connection_pool=_cache_pool)


def get_state_redis() -> Redis:
    """Returns Redis client for holi-estado (carts, idempotency, sessions)."""
    global _state_pool
    if _state_pool is None:
        init_redis_pools()
    return Redis(connection_pool=_state_pool)
