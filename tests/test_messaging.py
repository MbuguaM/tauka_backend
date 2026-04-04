"""
Tests for POST /messages/send
"""
from unittest.mock import patch, call
from tests.conftest import TEST_USER_ID

URL = "/messages/send"
VALID_BODY = {"conversation_id": "conv-uuid-123", "content": "Hello there"}


def _post(client, headers, body=VALID_BODY):
    return client.post(URL, json=body, headers=headers)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_requires_auth(client):
    resp = client.post(URL, json=VALID_BODY)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_send_message_success(client, auth_headers):
    with (
        patch("app.routes.messaging.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.messaging.store_message") as mock_store,
        patch("app.routes.messaging.log_usage"),
    ):
        resp = _post(client, auth_headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "sent"}
    mock_store.assert_called_once_with("conv-uuid-123", TEST_USER_ID, "Hello there")


def test_sender_id_comes_from_jwt_not_body(client, auth_headers):
    """Confirm store_message is called with the JWT user_id."""
    captured = {}

    def fake_store(conversation_id, sender_id, content):
        captured["sender_id"] = sender_id

    with (
        patch("app.routes.messaging.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.messaging.store_message", side_effect=fake_store),
        patch("app.routes.messaging.log_usage"),
    ):
        _post(client, auth_headers)

    assert captured["sender_id"] == TEST_USER_ID


def test_usage_logged_with_character_count(client, auth_headers):
    content = "Hello there"
    with (
        patch("app.routes.messaging.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.messaging.store_message"),
        patch("app.routes.messaging.log_usage") as mock_log,
    ):
        _post(client, auth_headers, {"conversation_id": "conv-1", "content": content})

    mock_log.assert_called_once_with(TEST_USER_ID, "message", len(content))


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limited_returns_429(client, auth_headers):
    with patch(
        "app.routes.messaging.check_rate_limit",
        return_value=(False, "Too many requests"),
    ):
        resp = _post(client, auth_headers)

    assert resp.status_code == 429
    assert resp.json()["detail"] == "Too many requests"


def test_rate_limit_uses_messaging_provider(client, auth_headers):
    captured = {}

    def fake_rate_limit(user_id, tokens, provider="openai"):
        captured["provider"] = provider
        return True, "OK"

    with (
        patch("app.routes.messaging.check_rate_limit", side_effect=fake_rate_limit),
        patch("app.routes.messaging.store_message"),
        patch("app.routes.messaging.log_usage"),
    ):
        _post(client, auth_headers)

    assert captured["provider"] == "messaging"
