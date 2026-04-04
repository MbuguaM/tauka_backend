"""
Tests for POST /calls/token
"""
from unittest.mock import patch, AsyncMock
from tests.conftest import TEST_USER_ID

URL = "/calls/token"
VALID_BODY = {"room": "test-room"}


def _post(client, headers, body=VALID_BODY):
    return client.post(URL, json=body, headers=headers)


DAILY_RESULT = {"token": "daily-token-abc", "room_url": "https://app.daily.co/test-room", "vendor": "daily"}
LIVEKIT_RESULT = {"token": "livekit-jwt-xyz", "room_url": None, "vendor": "livekit"}


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_requires_auth(client):
    resp = client.post(URL, json=VALID_BODY)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_daily_token_response(client, auth_headers):
    with (
        patch("app.routes.calling.generate_call_token", new_callable=AsyncMock, return_value=DAILY_RESULT),
        patch("app.routes.calling.log_usage"),
    ):
        resp = _post(client, auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["token"] == "daily-token-abc"
    assert data["room"] == "test-room"
    assert data["room_url"] == "https://app.daily.co/test-room"
    assert data["vendor"] == "daily"


def test_livekit_token_response(client, auth_headers):
    with (
        patch("app.routes.calling.generate_call_token", new_callable=AsyncMock, return_value=LIVEKIT_RESULT),
        patch("app.routes.calling.log_usage"),
    ):
        resp = _post(client, auth_headers, {"room": "test-room", "vendor": "livekit"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["token"] == "livekit-jwt-xyz"
    assert data["room_url"] is None
    assert data["vendor"] == "livekit"


def test_vendor_override_passed_to_service(client, auth_headers):
    captured = {}

    async def fake_generate(user_id, room, vendor=None):
        captured["vendor"] = vendor
        return LIVEKIT_RESULT

    with (
        patch("app.routes.calling.generate_call_token", side_effect=fake_generate),
        patch("app.routes.calling.log_usage"),
    ):
        _post(client, auth_headers, {"room": "r", "vendor": "livekit"})

    assert captured["vendor"] == "livekit"


def test_no_vendor_override_passes_none(client, auth_headers):
    """Omitting vendor lets the service resolve it from DB config."""
    captured = {}

    async def fake_generate(user_id, room, vendor=None):
        captured["vendor"] = vendor
        return DAILY_RESULT

    with (
        patch("app.routes.calling.generate_call_token", side_effect=fake_generate),
        patch("app.routes.calling.log_usage"),
    ):
        _post(client, auth_headers, {"room": "r"})

    assert captured["vendor"] is None


def test_user_id_comes_from_jwt(client, auth_headers):
    captured = {}

    async def fake_generate(user_id, room, vendor=None):
        captured["user_id"] = user_id
        return DAILY_RESULT

    with (
        patch("app.routes.calling.generate_call_token", side_effect=fake_generate),
        patch("app.routes.calling.log_usage"),
    ):
        _post(client, auth_headers)

    assert captured["user_id"] == TEST_USER_ID


def test_usage_logged_on_success(client, auth_headers):
    with (
        patch("app.routes.calling.generate_call_token", new_callable=AsyncMock, return_value=DAILY_RESULT),
        patch("app.routes.calling.log_usage") as mock_log,
    ):
        _post(client, auth_headers)

    mock_log.assert_called_once_with(TEST_USER_ID, "call_join", 1)


# ---------------------------------------------------------------------------
# Upstream failure
# ---------------------------------------------------------------------------

def test_vendor_failure_returns_502(client, auth_headers):
    with patch(
        "app.routes.calling.generate_call_token",
        new_callable=AsyncMock,
        side_effect=Exception("Daily API unreachable"),
    ):
        resp = _post(client, auth_headers)

    assert resp.status_code == 502
    assert "Daily API unreachable" in resp.json()["detail"]
