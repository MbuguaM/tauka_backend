import time
import jwt
import pytest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out infrastructure clients BEFORE app modules are imported.
# Both supabase and redis connect at module load time; patching here prevents
# the tests from needing a live database or cache.
# ---------------------------------------------------------------------------
_mock_supabase = MagicMock()
_supabase_patcher = patch("supabase.create_client", return_value=_mock_supabase)
_supabase_patcher.start()

_mock_redis = MagicMock()
_redis_patcher = patch("redis.Redis.from_url", return_value=_mock_redis)
_redis_patcher.start()

# Now it is safe to import the app.
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
TEST_JWT_SECRET = "test-secret-key-that-is-long-enough-for-hs256"
TEST_USER_ID = "00000000-0000-0000-0000-000000000001"


def make_token(user_id: str = TEST_USER_ID, expired: bool = False) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "exp": now - 10 if expired else now + 3600,
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_jwt_secret(monkeypatch):
    """Point the JWT dependency at the test secret for every test."""
    monkeypatch.setattr("app.dependencies.settings.supabase_jwt_secret", TEST_JWT_SECRET)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {make_token()}"}
