"""Per-API-key compute-time quota enforcement using Redis.

Each API key gets a rolling 24-hour budget of compute *seconds* -- the wall-clock
time the server watched a node spend on that key's tasks -- tracked as a Redis
counter with a TTL. We meter time the server measured itself rather than any
figure a node reports, since a node signs its own receipt with its own key and
could put any number there. The limit can be overridden per-key.
"""

from __future__ import annotations

from app.extensions import redis_client

QUOTA_DEFAULT_LIMIT = 6 * 3600  # 6 hours of compute per rolling 24h
QUOTA_RESET_SECONDS = 86400  # 24 hours


def get_quota_key(api_key: str) -> str:
    """Redis key for the compute-seconds-used counter."""
    return f"quota:{api_key}:seconds_used"


def get_quota_limit_key(api_key: str) -> str:
    """Redis key for the per-key compute-seconds limit."""
    return f"quota:{api_key}:seconds_limit"


def check_quota(api_key: str) -> tuple[bool, dict]:
    """Check whether *api_key* is still under its compute-time quota.

    Returns ``(True, info_dict)`` when under quota and
    ``(False, info_dict)`` when over.  *info_dict* always has keys
    ``used``, ``limit``, and ``remaining``.
    """
    used_raw = redis_client.get(get_quota_key(api_key))
    limit_raw = redis_client.get(get_quota_limit_key(api_key))

    used = int(used_raw) if used_raw is not None else 0
    limit = int(limit_raw) if limit_raw is not None else QUOTA_DEFAULT_LIMIT
    remaining = max(limit - used, 0)

    info = {"used": used, "limit": limit, "remaining": remaining}
    return (used < limit, info)


def record_usage(api_key: str, seconds: int) -> None:
    """Add *seconds* of measured compute to the rolling quota counter.

    If the counter doesn't yet have a TTL, one is set so the window
    automatically resets after ``QUOTA_RESET_SECONDS``.
    """
    key = get_quota_key(api_key)
    redis_client.incrby(key, seconds)

    # Only set TTL if the key has no expiry yet (ttl returns -1 for no expiry)
    if redis_client.ttl(key) < 0:
        redis_client.expire(key, QUOTA_RESET_SECONDS)
