# app/core/redis_client.py
import logging
import time

import redis

from app.config import settings

logger = logging.getLogger(__name__)

# How long to stay in degraded mode before trying to reconnect. Without this,
# every request retried the connection: a refused connect is NOT free on Windows
# (`localhost` resolves to both ::1 and 127.0.0.1, and each attempt burns SYN
# retries), which measured at ~4s per request against a stopped local Redis and
# showed up as the placement test appearing to hang on "Preparing your
# assessment…". Rate limiting is best-effort, so paying that on every call to
# rediscover a service we already know is down is never worth it.
_RECONNECT_COOLDOWN_SECONDS = 30.0

# Bound a single attempt, so an unreachable *remote* Redis (DNS blackhole,
# dropped packets) cannot stall a request for the OS-default TCP timeout. These
# calls are synchronous inside async handlers, so a stall blocks the event loop
# for every other in-flight request, not just this one.
_CONNECT_TIMEOUT_SECONDS = 1.5

_last_attempt_at: float = 0.0
_warned: bool = False


def _init_client():
    """
    PY-004: lazily/defensively construct the Redis client so an unreachable
    Redis URL cannot crash the whole FastAPI process at import time. On failure
    we return None and the app starts in a degraded (no-rate-limit) mode.
    """
    global _warned
    try:
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=_CONNECT_TIMEOUT_SECONDS,
        )
        client.ping()
        if _warned:
            logger.info("Redis reconnected at %s — rate limiting active", settings.REDIS_URL)
            _warned = False
        return client
    except Exception as exc:
        # Log the first failure only. This runs on a cooldown, but a long dev
        # session would still repeat this line indefinitely otherwise.
        if not _warned:
            logger.warning(
                "Redis unavailable at %s (%s) — rate limiting will be skipped; "
                "retrying at most every %.0fs",
                settings.REDIS_URL, exc, _RECONNECT_COOLDOWN_SECONDS,
            )
            _warned = True
        return None


redis_client = _init_client()
_last_attempt_at = time.monotonic()


def get_redis():
    """
    Return the live client, reconnecting at most once per cooldown window.

    Returns None while degraded — callers must treat that as "skip the check"
    (see rate_limit_service, which fails open per PY-005).
    """
    global redis_client, _last_attempt_at

    if redis_client is not None:
        return redis_client

    now = time.monotonic()
    if now - _last_attempt_at < _RECONNECT_COOLDOWN_SECONDS:
        return None

    _last_attempt_at = now
    redis_client = _init_client()
    return redis_client
