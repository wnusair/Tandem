import pytest
import fakeredis
import sys

# Create a fake Redis instance before any app imports
_fake_redis = fakeredis.FakeStrictRedis()

# Replace the real redis_client in app.extensions with our fake one
import app.extensions
app.extensions.redis_client = _fake_redis

# Also update it in any already-imported modules
if "app" in sys.modules and hasattr(sys.modules["app"], "extensions"):
    sys.modules["app"].extensions.redis_client = _fake_redis


@pytest.fixture(autouse=True)
def reset_fake_redis():
    """Reset fake Redis state between tests."""
    _fake_redis.flushdb()
    yield
    _fake_redis.flushdb()
