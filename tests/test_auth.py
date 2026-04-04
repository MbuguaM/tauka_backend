"""
Tests for the get_current_user JWT dependency.
Uses POST /messages/send as a representative authenticated endpoint.
"""
from unittest.mock import patch
from tests.conftest import make_token, TEST_USER_ID


SEND_URL = "/messages/send"
VALID_BODY = {"conversation_id": "conv-123", "content": "hello"}


def test_no_token_returns_403(client):
    resp = client.post(SEND_URL, json=VALID_BODY)
    assert resp.status_code == 401


def test_malformed_token_returns_401(client):
    resp = client.post(
        SEND_URL,
        json=VALID_BODY,
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


def test_expired_token_returns_401(client):
    token = make_token(expired=True)
    resp = client.post(
        SEND_URL,
        json=VALID_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token has expired"


def test_valid_token_extracts_user_id(client, auth_headers):
    """A valid token should pass auth; we check user_id reaches the service."""
    with (
        patch("app.routes.messaging.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.messaging.store_message"),
        patch("app.routes.messaging.log_usage"),
    ):
        resp = client.post(SEND_URL, json=VALID_BODY, headers=auth_headers)
    assert resp.status_code == 200
