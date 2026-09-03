import time
import logging
from redis.asyncio import Redis

_logger = logging.getLogger(__name__)


async def check_rate_limit(redis_state: Redis, scope: str, identifier: str, max_requests: int, window_seconds: int = 60) -> bool:
    """
    Increments and checks fixed window rate limit in holi-estado.
    Key pattern: rl:{scope}:{id}:{window_timestamp}
    Returns True if allowed, False if exceeded.
    """
    try:
        current_window = int(time.time()) // window_seconds
        key = f"rl:{scope}:{identifier}:{current_window}"
        
        pipe = redis_state.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds * 2)
        results = await pipe.execute()
        count = results[0]
        
        return count <= max_requests
    except Exception as e:
        _logger.warning("Rate limit check failed (failing open): %s", e)
        return True  # Fail open to not block legitimate clients during Redis degradation
