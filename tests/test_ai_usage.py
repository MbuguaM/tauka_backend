"""
Phase 9 — the durable AI token ledger.

Two things are being tested and they fail differently.

The **parsers** decide whether a number is real. Every provider returns usage
alongside the text and, until this phase, every one of those numbers was parsed
and discarded — `call_ai` returns a `str`. If a parser is wrong the ledger looks
populated and is fiction.

The **route wiring** decides whether a row is written at all, and whether it is
honest about where its number came from. `source` is the column that separates a
measured 1,200 from a guessed 1,200; without it the ledger silently inherits the
defect §9 exists to fix.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import ai_services
from app.services.ai_services import (
    ProviderUsage,
    _record_gemini_usage,
    _record_openai_usage,
    take_last_usage,
)


@pytest.fixture(autouse=True)
def _clean_usage():
    """A value left in the ContextVar would be billed to the next test."""
    take_last_usage()
    yield
    take_last_usage()


# ---------------------------------------------------------------------------
# Reading the provider's own numbers
# ---------------------------------------------------------------------------

def test_openai_usage_is_read_from_the_response():
    _record_openai_usage(
        {"model": "gpt-4o-mini",
         "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}},
        "gpt-4o-mini",
    )
    usage = take_last_usage()
    assert usage == ProviderUsage(120, 30, 150, "gpt-4o-mini")


def test_deepseek_shares_the_openai_shape():
    _record_openai_usage(
        {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
        "deepseek-chat",
    )
    usage = take_last_usage()
    assert usage.total_tokens == 10
    assert usage.model == "deepseek-chat"


def test_gemini_names_every_field_differently():
    """The bug this prevents: reading `usage` on a Gemini body and getting None."""
    _record_gemini_usage(
        {"modelVersion": "gemini-1.5-flash",
         "usageMetadata": {"promptTokenCount": 200,
                           "candidatesTokenCount": 50,
                           "totalTokenCount": 250}},
        "gemini-1.5-flash",
    )
    usage = take_last_usage()
    assert usage == ProviderUsage(200, 50, 250, "gemini-1.5-flash")


def test_a_body_with_no_usage_reports_nothing():
    """
    None must mean "the provider did not say", never zero. A zero would be
    written to the ledger as a measured zero.
    """
    _record_openai_usage({"choices": []}, "gpt-4o-mini")
    assert take_last_usage() is None

    _record_gemini_usage({"candidates": []}, "gemini-1.5-flash")
    assert take_last_usage() is None


def test_a_missing_total_is_derived_from_the_halves():
    _record_openai_usage(
        {"usage": {"prompt_tokens": 40, "completion_tokens": 60}}, "m")
    assert take_last_usage().total_tokens == 100


def test_a_total_with_no_halves_is_kept_whole():
    _record_openai_usage({"usage": {"total_tokens": 90}}, "m")
    usage = take_last_usage()
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (0, 0, 90)


def test_usage_is_consumed_on_read():
    """
    A context is copied when a task is spawned, so a value left behind could be
    read by a later request and billed to the wrong person.
    """
    _record_openai_usage({"usage": {"total_tokens": 5}}, "m")
    assert take_last_usage() is not None
    assert take_last_usage() is None


# ---------------------------------------------------------------------------
# Writing the row
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(monkeypatch):
    from app.services import ai_usage_service
    fake = MagicMock()
    monkeypatch.setattr(ai_usage_service, "db", fake)
    return fake


def _written(ledger):
    return ledger.schema.return_value.table.return_value.insert.call_args[0][0]


@pytest.mark.asyncio
async def test_a_reported_number_is_marked_as_measured(ledger):
    from app.services.ai_usage_service import log_ai_usage

    await log_ai_usage(
        user_id="u1", provider="openai", mode="chat",
        usage=ProviderUsage(100, 20, 120, "gpt-4o-mini"),
        estimated_total=999, model="gpt-4o-mini",
    )

    row = _written(ledger)
    assert row["source"] == "provider"
    assert row["total_tokens"] == 120, "the estimate must not win over a real number"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (100, 20)
    ledger.schema.assert_called_with("app")


@pytest.mark.asyncio
async def test_an_unreported_number_falls_back_and_says_so(ledger):
    from app.services.ai_usage_service import log_ai_usage

    await log_ai_usage(
        user_id="u1", provider="gemini", mode="image_translation",
        usage=None, estimated_total=430, model="gemini-1.5-flash",
    )

    row = _written(ledger)
    assert row["source"] == "estimate"
    assert row["total_tokens"] == 430
    # The split is genuinely unknown; inventing halves would fabricate precision.
    assert (row["prompt_tokens"], row["completion_tokens"]) == (0, 0)


@pytest.mark.asyncio
async def test_a_negative_estimate_cannot_violate_the_check_constraint(ledger):
    from app.services.ai_usage_service import log_ai_usage

    await log_ai_usage(user_id="u1", provider="deepseek", mode="chat",
                       usage=None, estimated_total=-5)
    assert _written(ledger)["total_tokens"] == 0


@pytest.mark.asyncio
async def test_a_failed_write_never_reaches_the_student(ledger):
    """They got their answer; the accounting is ours to fix."""
    from app.services.ai_usage_service import log_ai_usage

    ledger.schema.return_value.table.return_value.insert.return_value.execute.side_effect = (
        RuntimeError("insert failed")
    )
    await log_ai_usage(user_id="u1", provider="openai", mode="chat",
                       usage=None, estimated_total=10)  # must not raise


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def test_the_route_writes_a_ledger_row(client, auth_headers):
    with (
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="hi"),
        patch("app.routes.ai.check_rate_limit", return_value=(True, None)),
        patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock) as ledger,
    ):
        res = client.post("/ai/generate",
                          json={"prompt": "hello", "provider": "deepseek", "mode": "chat"},
                          headers=auth_headers)

    assert res.status_code == 200
    ledger.assert_awaited_once()
    kwargs = ledger.await_args.kwargs
    assert kwargs["provider"] == "deepseek"
    assert kwargs["mode"] == "chat"
    assert kwargs["estimated_total"] > 0


def test_a_stubbed_provider_yields_an_estimate_row_not_a_fake_measurement(
        client, auth_headers):
    """
    call_ai patched means no provider usage was recorded, so `usage` must arrive
    as None — which is what makes the row honest rather than a local count
    dressed up as the model's.
    """
    with (
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="hi"),
        patch("app.routes.ai.check_rate_limit", return_value=(True, None)),
        patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock) as ledger,
    ):
        client.post("/ai/generate",
                    json={"prompt": "hello", "provider": "deepseek", "mode": "chat"},
                    headers=auth_headers)

    assert ledger.await_args.kwargs["usage"] is None


def test_the_providers_number_reaches_the_ledger(client, auth_headers):
    """End to end: a real usage block on the response becomes a measured row."""
    async def _call(**kwargs):
        ai_services._record_openai_usage(
            {"model": "deepseek-chat",
             "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}},
            "deepseek-chat",
        )
        return "hi"

    with (
        patch("app.routes.ai.call_ai", side_effect=_call),
        patch("app.routes.ai.check_rate_limit", return_value=(True, None)),
        patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock) as ledger,
    ):
        client.post("/ai/generate",
                    json={"prompt": "hello", "provider": "deepseek", "mode": "chat"},
                    headers=auth_headers)

    assert ledger.await_args.kwargs["usage"] == ProviderUsage(11, 4, 15, "deepseek-chat")


def test_a_failed_call_is_not_billed(client, auth_headers):
    """A call that raised charged the student nothing and must not appear."""
    with (
        patch("app.routes.ai.call_ai", new_callable=AsyncMock,
              side_effect=ValueError("unsupported")),
        patch("app.routes.ai.check_rate_limit", return_value=(True, None)),
        patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock) as ledger,
    ):
        res = client.post("/ai/generate",
                          json={"prompt": "x", "provider": "deepseek",
                                "mode": "image_translation", "image_url": "https://e/i.png"},
                          headers=auth_headers)

    assert res.status_code == 400
    ledger.assert_not_awaited()


def test_a_rate_limited_call_is_not_billed(client, auth_headers):
    with (
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="hi"),
        patch("app.routes.ai.check_rate_limit", return_value=(False, "daily cap")),
        patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock) as ledger,
    ):
        res = client.post("/ai/generate",
                          json={"prompt": "hello", "provider": "deepseek", "mode": "chat"},
                          headers=auth_headers)

    assert res.status_code == 429
    ledger.assert_not_awaited()


def test_redis_remains_the_enforcer_and_is_still_written(client, auth_headers):
    """
    The ledger is an addition, not a replacement. If this stops being called the
    daily caps silently stop working.
    """
    with (
        patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="hi"),
        patch("app.routes.ai.check_rate_limit", return_value=(True, None)) as limiter,
        patch("app.routes.ai.log_usage", new_callable=AsyncMock) as redis_log,
        patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock),
    ):
        client.post("/ai/generate",
                    json={"prompt": "hello", "provider": "deepseek", "mode": "chat"},
                    headers=auth_headers)

    limiter.assert_called_once()
    redis_log.assert_called_once()
