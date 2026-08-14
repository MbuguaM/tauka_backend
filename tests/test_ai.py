"""
Tests for POST /ai/generate and GET /ai/balance
"""
import httpx
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from tests.conftest import TEST_USER_ID

URL = "/ai/generate"


def _post(client, auth_headers, body):
    return client.post(URL, json=body, headers=auth_headers)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_requires_auth(client):
    resp = client.post(URL, json={"prompt": "hi"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_chat_default_provider(client, auth_headers):
    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="Hello back"),
        patch("app.routes.ai.log_usage"),
    ):
        resp = _post(client, auth_headers, {"prompt": "Hello"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["response"] == "Hello back"
    assert data["provider"] == "deepseek"
    assert data["mode"] == "chat"
    assert isinstance(data["tokens"], int)


def test_translation_mode(client, auth_headers):
    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="Bonjour"),
        patch("app.routes.ai.log_usage"),
    ):
        resp = _post(client, auth_headers, {
            "prompt": "Hello",
            "mode": "translation",
            "target_language": "French",
        })

    assert resp.status_code == 200
    assert resp.json()["response"] == "Bonjour"
    assert resp.json()["mode"] == "translation"


def test_image_translation_mode(client, auth_headers):
    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="Translated text"),
        patch("app.routes.ai.log_usage"),
    ):
        resp = _post(client, auth_headers, {
            "prompt": "Translate this",
            "provider": "gemini",
            "mode": "image_translation",
            "target_language": "Spanish",
            "image_url": "https://example.com/image.jpg",
        })

    assert resp.status_code == 200
    assert resp.json()["response"] == "Translated text"


def test_call_ai_receives_correct_user_id(client, auth_headers):
    """Rate limit and usage logging must use the JWT user_id, not a body field."""
    captured = {}

    def fake_rate_limit(user_id, tokens, provider, mode="chat"):
        captured["user_id"] = user_id
        return True, "OK"

    with (
        patch("app.routes.ai.check_rate_limit", side_effect=fake_rate_limit),
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="ok"),
        patch("app.routes.ai.log_usage"),
    ):
        _post(client, auth_headers, {"prompt": "hi"})

    assert captured["user_id"] == TEST_USER_ID


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

def test_image_translation_requires_image_url(client, auth_headers):
    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch("app.routes.ai.call_ai", new_callable=AsyncMock),
    ):
        resp = _post(client, auth_headers, {
            "prompt": "Translate",
            "mode": "image_translation",
        })

    assert resp.status_code == 422
    assert "image_url" in resp.json()["detail"]


def test_unsupported_provider_mode_combination(client, auth_headers):
    """deepseek does not support image_translation — call_ai raises ValueError."""
    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch(
            "app.routes.ai.call_ai",
            new_callable=AsyncMock,
            side_effect=ValueError("image_translation is not supported for provider 'deepseek'"),
        ),
        patch("app.routes.ai.log_usage"),
    ):
        resp = _post(client, auth_headers, {
            "prompt": "Translate",
            "provider": "deepseek",
            "mode": "image_translation",
            "image_url": "https://example.com/image.jpg",
        })

    assert resp.status_code == 400
    assert "deepseek" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,expected_detail", [
    ("Too many requests", "Too many requests"),
    ("Daily token limit exceeded", "Daily token limit exceeded"),
    ("Monthly token limit exceeded", "Monthly token limit exceeded"),
])
def test_rate_limit_returns_429(client, auth_headers, reason, expected_detail):
    with patch("app.routes.ai.check_rate_limit", return_value=(False, reason)):
        resp = _post(client, auth_headers, {"prompt": "hi"})

    assert resp.status_code == 429
    assert resp.json()["detail"] == expected_detail


# ---------------------------------------------------------------------------
# GET /ai/balance
# ---------------------------------------------------------------------------

BALANCE_URL = "/ai/balance"


def test_balance_requires_auth(client):
    assert client.get(BALANCE_URL).status_code == 401


def test_balance_reports_remaining_from_redis(client, auth_headers):
    # 1,200 of the deepseek plan's 8,000 daily tokens already consumed.
    mock_redis = MagicMock()
    mock_redis.mget.return_value = ["1200", "40000"]

    with patch("app.services.rate_limit_service.get_redis", return_value=mock_redis):
        resp = client.get(BALANCE_URL, headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 8_000
    assert data["used"] == 1_200
    assert data["remaining"] == 6_800
    assert data["monthly_remaining"] == 110_000
    assert data["provider"] == "deepseek"


def test_balance_never_goes_negative(client, auth_headers):
    mock_redis = MagicMock()
    mock_redis.mget.return_value = ["9999999", "9999999"]

    with patch("app.services.rate_limit_service.get_redis", return_value=mock_redis):
        resp = client.get(BALANCE_URL, headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["remaining"] == 0
    assert resp.json()["monthly_remaining"] == 0


def test_balance_does_not_consume_budget(client, auth_headers):
    """Polling the endpoint must never incr a counter — see PY-005 note."""
    mock_redis = MagicMock()
    mock_redis.mget.return_value = [None, None]

    with patch("app.services.rate_limit_service.get_redis", return_value=mock_redis):
        client.get(BALANCE_URL, headers=auth_headers)

    mock_redis.incr.assert_not_called()
    mock_redis.incrby.assert_not_called()


def test_balance_fails_open_when_redis_down(client, auth_headers):
    with patch("app.services.rate_limit_service.get_redis", return_value=None):
        resp = client.get(BALANCE_URL, headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["remaining"] == data["limit"] == 8_000


def test_balance_honours_provider_param(client, auth_headers):
    mock_redis = MagicMock()
    mock_redis.mget.return_value = [None, None]

    with patch("app.services.rate_limit_service.get_redis", return_value=mock_redis):
        resp = client.get(BALANCE_URL, params={"provider": "gemini"}, headers=auth_headers)

    assert resp.json()["limit"] == 10_000
    assert resp.json()["provider"] == "gemini"


def test_balance_ignores_spoofed_userid_query_param(client, auth_headers):
    """Identity comes from the JWT — ?userId= must not be able to target
    another user's counters."""
    mock_redis = MagicMock()
    mock_redis.mget.return_value = [None, None]

    with patch("app.services.rate_limit_service.get_redis", return_value=mock_redis):
        client.get(
            BALANCE_URL,
            params={"userId": "11111111-1111-1111-1111-111111111111"},
            headers=auth_headers,
        )

    day_key, month_key = mock_redis.mget.call_args[0]
    assert TEST_USER_ID in day_key
    assert "11111111-1111-1111-1111-111111111111" not in day_key
    assert TEST_USER_ID in month_key


# ---------------------------------------------------------------------------
# Upstream provider failures -> 502, not an unhandled 500
# ---------------------------------------------------------------------------

def test_provider_rejects_request_returns_502(client, auth_headers):
    """A bad/missing provider API key must surface as 502, not 500.

    ai_services calls res.raise_for_status(), which raises HTTPStatusError —
    NOT a ValueError. Before this was handled it escaped as an unhandled 500,
    and the Flutter client maps 500 to a generic "unexpected error" while it
    maps 502 to a real upstream-vendor message. A placeholder OPENAI_API_KEY
    was therefore indistinguishable from a client bug.
    """
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(401, text='{"error":"Incorrect API key provided"}', request=request)

    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch(
            "app.routes.ai.call_ai",
            new_callable=AsyncMock,
            side_effect=httpx.HTTPStatusError("401", request=request, response=response),
        ),
        patch("app.routes.ai.log_usage"),
    ):
        resp = _post(client, auth_headers, {"prompt": "hi", "provider": "openai"})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "openai" in detail
    assert "401" in detail


def test_provider_unreachable_returns_502(client, auth_headers):
    with (
        patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")),
        patch(
            "app.routes.ai.call_ai",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("name resolution failed"),
        ),
        patch("app.routes.ai.log_usage"),
    ):
        resp = _post(client, auth_headers, {"prompt": "hi"})

    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]
