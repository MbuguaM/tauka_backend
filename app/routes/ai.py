import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from app.core.followup import run_followup
from app.dependencies import get_current_user
from app.models.schemas import AIRequest
from app.services.ai_prompts import FRAMED_MODES, build_framed_prompt
from app.services.ai_services import call_ai
from app.services.token_service import count_tokens
from app.services.rate_limit_service import check_rate_limit, get_rate_limit_status
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
    """
    return get_rate_limit_status(user_id, provider=provider)


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
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    try:
        response = await call_ai(
            prompt=user_prompt,
            provider=req.provider,
            mode=req.mode,
            target_language=req.target_language,
            image_url=req.image_url,
            system=system_prompt,
        )
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

    actual = count_tokens(billable, model=model) + count_tokens(response, model=model)
    # Inline: usage feeds the daily/monthly token caps, so losing these writes
    # silently raises every user's effective limit. One insert against Supabase.
    await run_followup(
        log_usage(user_id, f"ai_tokens:{req.provider}:{req.mode}", actual),
        label=f"log_usage {user_id} {req.provider}",
    )

    return {"response": response, "tokens": actual, "provider": req.provider, "mode": req.mode}
