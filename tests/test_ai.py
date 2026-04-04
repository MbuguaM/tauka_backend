"""
Tests for POST /ai/generate
"""
import pytest
from unittest.mock import patch, AsyncMock
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
