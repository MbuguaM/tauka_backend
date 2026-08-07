from app.core.redis_client import get_redis
from datetime import datetime
import time

# Per-provider default plans.  Keys: requests_per_minute, daily_token_limit,
# monthly_token_limit.  Image translation counts tokens at a higher rate
# (images are billed as ~1000 tokens each regardless of size).
_PLANS: dict[str, dict] = {
    "openai": {
        "requests_per_minute": 10,
        "daily_token_limit": 5_000,
        "monthly_token_limit": 100_000,
    },
    "deepseek": {
        "requests_per_minute": 15,
        "daily_token_limit": 8_000,
        "monthly_token_limit": 150_000,
    },
    "gemini": {
        "requests_per_minute": 15,
        "daily_token_limit": 10_000,
        "monthly_token_limit": 200_000,
    },
    # Messaging uses character count as a proxy for "tokens"
    "messaging": {
        "requests_per_minute": 20,
        "daily_token_limit": 10_000,
        "monthly_token_limit": 200_000,
    },
}

# Image requests cost more — apply a multiplier to the token estimate.
_IMAGE_TOKEN_SURCHARGE = 1_000


def check_rate_limit(
    user_id: str,
    tokens: int,
    provider: str = "openai",
    mode: str = "chat",
) -> tuple[bool, str]:
    """
    Check and update rate-limit counters in Redis.

    Counters are namespaced per (provider, user_id) so each provider has
    independent limits.  Image translation adds a fixed surcharge to the
    token count to reflect higher API cost.

    Returns (allowed, reason).
    """
    plan = _PLANS.get(provider, _PLANS["deepseek"])
    now = datetime.utcnow()

    if mode == "image_translation":
        tokens += _IMAGE_TOKEN_SURCHARGE

    ns = f"{provider}:{user_id}"
    minute_key = f"rate:{ns}:{now.strftime('%Y%m%d%H%M')}"
    day_key = f"tokens:{ns}:day:{now.strftime('%Y%m%d')}"
    month_key = f"tokens:{ns}:month:{now.strftime('%Y%m')}"

    # PY-005: fail open — if Redis is unavailable or a call errors mid-request,
    # skip the rate-limit check rather than returning a 500 to the client.
    redis_client = get_redis()
    if redis_client is None:
        return True, "OK"

    try:
        # requests / minute
        reqs = redis_client.incr(minute_key)
        if reqs == 1:
            redis_client.expire(minute_key, 60)
        if reqs > plan["requests_per_minute"]:
            return False, "Too many requests"

        # tokens / day
        daily = redis_client.incrby(day_key, tokens)
        if daily == tokens:
            redis_client.expire(day_key, 86_400)
        if daily > plan["daily_token_limit"]:
            return False, "Daily token limit exceeded"

        # tokens / month
        monthly = redis_client.incrby(month_key, tokens)
        if monthly == tokens:
            redis_client.expire(month_key, 2_592_000)
        if monthly > plan["monthly_token_limit"]:
            return False, "Monthly token limit exceeded"
    except Exception:
        return True, "OK"

    return True, "OK"


def check_anonymous_rate_limit(
    ip_hash: str,
    endpoint: str,
    max_requests: int = 5,
    window_seconds: int = 3600,
) -> tuple[bool, str]:
    """Rate limits anonymous requests by hashed IP and endpoint."""
    window = int(time.time() // window_seconds)
    key = f"anon_rate:{endpoint}:{ip_hash}:{window}"

    # PY-005: fail open when Redis is down.
    redis_client = get_redis()
    if redis_client is None:
        return True, "OK"

    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, window_seconds)
        if count > max_requests:
            return False, "Too many requests from this IP"
    except Exception:
        return True, "OK"
    return True, "OK"
