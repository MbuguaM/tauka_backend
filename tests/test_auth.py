"""
Tests for the get_current_user JWT dependency.
Uses POST /messages/send as a representative authenticated endpoint.
"""
from unittest.mock import patch, AsyncMock
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


# ---------------------------------------------------------------------------
# Audience handling — regression guard
# ---------------------------------------------------------------------------

def test_accepts_real_supabase_token_shape(client):
    """Supabase issues user JWTs with aud="authenticated".

    PyJWT 2.x raises InvalidAudienceError when a token carries `aud` and the
    caller does not declare an expected audience. get_current_user omitted
    `audience=`, so every genuine Supabase token was rejected with 401
    "Invalid token" — breaking every authenticated route in the API. The old
    fixture minted tokens with NO aud claim, the one shape that passed, so the
    suite stayed green while production was fully broken.
    """
    from tests.conftest import make_token
    headers = {"Authorization": f"Bearer {make_token(aud='authenticated')}"}
    with patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")), \
         patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="ok"), \
         patch("app.routes.ai.log_usage"):
        resp = client.post("/ai/generate", json={"prompt": "hi"}, headers=headers)
    assert resp.status_code == 200, resp.text


def test_rejects_token_with_wrong_audience(client):
    from tests.conftest import make_token
    headers = {"Authorization": f"Bearer {make_token(aud='some-other-service')}"}
    resp = client.post("/ai/generate", json={"prompt": "hi"}, headers=headers)
    assert resp.status_code == 401
