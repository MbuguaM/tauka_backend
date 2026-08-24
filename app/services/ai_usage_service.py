"""
The durable AI token ledger — `app.ai_usage`.

Phase 9 of `activity-telemetry-plan.md`.

WHAT THIS IS NOT
----------------
It is not the rate limiter, and it must never become one. Redis is the
**enforcer**: an estimate (`count_tokens(prompt) + 500`), charged *before* the
call, expiring after a day or a month, and failing open on an outage. Every one
of those is the right choice for stopping a runaway bill in the moment, and the
wrong one for answering "what did I use". This is the **ledger**: one row per
call, written *after* the response, from what the model said it charged, kept.

The two numbers will differ. That is the design, not a bug to reconcile — the
stats page reads this table, the 429 comes from Redis.

OWNERSHIP
---------
`app.ai_usage` lives in the `ai` domain, whose DDL tauka-flutter owns.
tauka-python is the SOLE writer, via service role. That is the `app.student.tier`
pattern, chosen over inventing a second one for the same problem. There is no
INSERT policy for `authenticated` at all: a client that can write its own usage
rows can under-report its own spend.

WHY `source` EXISTS
-------------------
Not every provider path reports usage — an image translation that falls back, a
provider that omits `usage` on an error-shaped 200. When the number is a local
estimate the row says so. Without that column a measured 1,200 and a guessed
1,200 are indistinguishable, and the ledger silently inherits the exact defect
§9 was written to fix.
"""
import logging

from app.services.ai_services import ProviderUsage
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


async def log_ai_usage(
    *,
    user_id: str,
    provider: str,
    mode: str,
    usage: ProviderUsage | None,
    estimated_total: int,
    model: str | None = None,
) -> None:
    """
    Write one ledger row.

    `usage` is what the provider reported, or None if it reported nothing. When
    it is None the row is written anyway, from `estimated_total`, and marked
    `source='estimate'` — a missing row would understate the student's usage,
    which is worse than an honestly-labelled approximation.

    Never raises. A ledger write failing must not fail the AI call the student
    is waiting on: they got their answer, and the accounting is ours to fix.
    """
    if usage is not None:
        row = {
            "user_id": user_id,
            "provider": provider,
            "model": usage.model or model,
            "mode": mode,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "source": "provider",
        }
    else:
        # The split is genuinely unknown here — inventing halves would be
        # fabricating precision the number does not have. Same call made for
        # `ai_request` on the client in session 29.
        row = {
            "user_id": user_id,
            "provider": provider,
            "model": model,
            "mode": mode,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": max(0, int(estimated_total)),
            "source": "estimate",
        }

    try:
        db.schema("app").table("ai_usage").insert(row).execute()
    except Exception as exc:
        logger.warning(
            "ai_usage ledger write failed for %s (%s/%s): %s",
            user_id, provider, mode, exc,
        )
