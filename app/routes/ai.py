import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from app.core.flags import is_ready
from app.core.followup import run_followup
from app.dependencies import get_current_user
from app.models.schemas import AIRequest
from app.services import ai_token_service
from app.services.ai_prompts import FRAMED_MODES, build_framed_prompt
from app.services.ai_services import call_ai, take_last_usage
from app.services.ai_usage_service import log_ai_usage
from app.services.token_service import count_tokens
from app.services.rate_limit_service import IMAGE_TOKEN_SURCHARGE, check_rate_limit, get_rate_limit_status
from app.services.usage_service import log_usage

logger = logging.getLogger(__name__)

router = APIRouter()

# Model names used for token counting per provider
_PROVIDER_MODEL = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",   # cl100k_base fallback
    "gemini": "gemini-1.5-flash",  # cl100k_base fallback
}


@router.get("/balance")
async def balance(
    provider: str = "deepseek",
    user_id: str = Depends(get_current_user),
):
    """
    Remaining daily AI token budget for the caller.

    The Flutter client (ApiClient.checkAiBalance) calls this before every AI
    request and raises AiTokenLimitException locally when `remaining <= 0`,
    so the user gets "you're out for today" instead of a 429 mid-conversation.

    Identity comes from the JWT. The client also sends `?userId=` — it is
    ignored on purpose: a caller must never be able to read another user's
    budget by changing a query string.

    `purchased` is what they have left of any AI tokens they bought (§154), used once the
    allowance runs out. `tokens_on_sale` lets a desktop app show its buy button.
    """
    return {**get_rate_limit_status(user_id, provider=provider),
            "purchased": ai_token_service.balance(user_id),
            "tokens_on_sale": is_ready("ai_tokens")}


@router.post("/generate")
async def generate(
    req: AIRequest,
    user_id: str = Depends(get_current_user),
):
    if req.mode == "image_translation" and not req.image_url:
        raise HTTPException(status_code=422, detail="image_url is required for image_translation mode")

    model = _PROVIDER_MODEL.get(req.provider, "deepseek-chat")

    # Framed modes: the instructions are built here, not accepted from the
    # client, and the client's text is fenced inside them. See ai_prompts.
    system_prompt: str | None = None
    user_prompt = req.prompt
    if req.mode in FRAMED_MODES:
        system_prompt, user_prompt = build_framed_prompt(
            req.mode,
            req.prompt,
            passage=req.passage,
            context=req.context,
        )

    # Bill what is actually sent. Counting req.prompt alone would have let the
    # framed modes — which add a system prompt and can carry a whole passage —
    # spend several times the tokens they were charged for.
    billable = f"{system_prompt or ''}{user_prompt}"
    estimated = count_tokens(billable, model=model) + 500

    allowed, reason = check_rate_limit(
        user_id,
        estimated,
        provider=req.provider,
        mode=req.mode,
    )
    # Past the allowance, bought tokens pay for the call (§154). The per-minute limit still
    # applies, and without enough tokens the learner sees the same 429 as before.
    surcharge = IMAGE_TOKEN_SURCHARGE if req.mode == "image_translation" else 0
    held = 0
    if not allowed:
        if "token limit" not in (reason or "") or not ai_token_service.hold(user_id, estimated + surcharge):
            raise HTTPException(status_code=429, detail=reason)
        held = estimated + surcharge

    delivered = False
    try:
        response = await call_ai(
            prompt=user_prompt,
            provider=req.provider,
            mode=req.mode,
            target_language=req.target_language,
            image_url=req.image_url,
            system=system_prompt,
        )
        delivered = True
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPStatusError as e:
        # The provider rejected us — bad/missing API key, its own rate limit,
        # or an outage. res.raise_for_status() in ai_services raises this, and
        # it is NOT a ValueError, so before this clause it escaped as an
        # unhandled 500. The Flutter client maps 500 to a generic
        # "unexpected error" but maps 502 to a real "upstream vendor error",
        # so misconfiguration was indistinguishable from a client-side bug.
        logger.error(
            "AI provider %s returned %s for mode %s: %s",
            req.provider, e.response.status_code, req.mode,
            e.response.text[:500],
        )
        raise HTTPException(
            status_code=502,
            detail=f"AI provider '{req.provider}' rejected the request "
                   f"({e.response.status_code}). Check that its API key is configured.",
        )
    except httpx.RequestError as e:
        logger.error("AI provider %s unreachable: %s", req.provider, e)
        raise HTTPException(
            status_code=502,
            detail=f"AI provider '{req.provider}' is unreachable.",
        )
    finally:
        # Any failure, including one not handled above, spent nothing.
        if held and not delivered:
            ai_token_service.give_back(user_id, held)

    # Taken here, immediately, while it is still this call's value. It is None
    # whenever the provider did not report usage — including every path where
    # call_ai is stubbed — and the ledger row then says `source='estimate'`
    # rather than passing a local count off as a measurement.
    usage = take_last_usage()

    actual = count_tokens(billable, model=model) + count_tokens(response, model=model)
    if held:
        await run_followup(
            ai_token_service.settle_call(user_id, held, actual + surcharge),
            label=f"settle_ai_tokens {user_id}",
        )
    # Inline: usage feeds the daily/monthly token caps, so losing these writes
    # silently raises every user's effective limit. One insert against Supabase.
    await run_followup(
        log_usage(user_id, f"ai_tokens:{req.provider}:{req.mode}", actual),
        label=f"log_usage {user_id} {req.provider}",
    )

    # The durable ledger (phase 9). Read the provider's own count BEFORE the
    # followup above can start another call in this context — take_last_usage()
    # consumes, so reading it late risks reading nothing.
    #
    # Deliberately AFTER a successful response: a call that raised charged the
    # student nothing and must not appear in their usage. `actual` is only the
    # fallback, and a row built from it says so.
    await run_followup(
        log_ai_usage(
            user_id=user_id,
            provider=req.provider,
            mode=req.mode,
            usage=usage,
            estimated_total=actual,
            model=model,
        ),
        label=f"log_ai_usage {user_id} {req.provider}",
    )

    return {"response": response, "tokens": actual, "provider": req.provider, "mode": req.mode}
