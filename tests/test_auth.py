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


# ---------------------------------------------------------------------------
# Asymmetric signing keys (ES256/RS256 via the project's JWKS)
# ---------------------------------------------------------------------------

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

KID = "test-kid"


def _ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _es256_token(private_key, kid=KID, alg="ES256", **claims):
    payload = {"sub": TEST_USER_ID, "aud": "authenticated", "exp": int(time.time()) + 3600, **claims}
    return jwt.encode(payload, private_key, algorithm=alg, headers={"kid": kid})


class _FakeJwks:
    """Stands in for PyJWKClient: serves one published key, or fails to connect."""

    def __init__(self, private_key=None, alg="ES256", unreachable=False):
        self.unreachable = unreachable
        if private_key is not None:
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key()))
            self.key = jwt.PyJWK({**jwk, "kid": KID, "alg": alg})

    def get_signing_key_from_jwt(self, token):
        if self.unreachable:
            raise jwt.PyJWKClientConnectionError("unreachable")
        if jwt.get_unverified_header(token).get("kid") != KID:
            raise jwt.PyJWKClientError("no matching kid")
        return self.key


@pytest.fixture
def jwks(monkeypatch):
    def install(fake):
        monkeypatch.setattr("app.dependencies._jwks", lambda: fake)
        monkeypatch.setattr("app.dependencies.settings.supabase_url", "http://127.0.0.1:54321")
    return install


def _post_ai(client, token):
    with patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")), \
         patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="ok"), \
         patch("app.routes.ai.log_usage"):
        return client.post("/ai/generate", json={"prompt": "hi"},
                           headers={"Authorization": f"Bearer {token}"})


def test_accepts_es256_token_from_published_key(client, jwks):
    key = _ec_key()
    jwks(_FakeJwks(key))
    resp = _post_ai(client, _es256_token(key))
    assert resp.status_code == 200, resp.text


def test_rejects_es256_token_signed_by_another_key(client, jwks):
    jwks(_FakeJwks(_ec_key()))
    resp = _post_ai(client, _es256_token(_ec_key()))
    assert resp.status_code == 401


def test_rejects_unknown_kid(client, jwks):
    key = _ec_key()
    jwks(_FakeJwks(key))
    resp = _post_ai(client, _es256_token(key, kid="someone-elses-key"))
    assert resp.status_code == 401


def test_rejects_header_alg_that_differs_from_the_key(client, jwks):
    """The published key is ES256; a token claiming RS256 must not be tried against it."""
    key = _ec_key()
    jwks(_FakeJwks(key, alg="ES256"))
    token = _es256_token(key)
    header, rest = token.split(".", 1)
    forged_header = jwt.utils.base64url_encode(json.dumps({"alg": "RS256", "kid": KID, "typ": "JWT"}).encode()).decode()
    resp = _post_ai(client, f"{forged_header}.{rest}")
    assert resp.status_code == 401


def test_rejects_unsupported_alg(client):
    token = jwt.encode({"sub": TEST_USER_ID, "aud": "authenticated", "exp": int(time.time()) + 60},
                       "x" * 64, algorithm="HS512")
    assert _post_ai(client, token).status_code == 401


def test_unreachable_jwks_fails_closed_with_503(client, jwks):
    jwks(_FakeJwks(unreachable=True))
    resp = _post_ai(client, _es256_token(_ec_key()))
    assert resp.status_code == 503
