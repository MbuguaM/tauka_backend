from fastapi import APIRouter, Depends, HTTPException
from app.core.followup import run_followup
from app.dependencies import get_current_user
from app.models.schemas import AIRequest
from app.services.ai_services import call_ai
from app.services.token_service import count_tokens
from app.services.rate_limit_service import check_rate_limit
from app.services.usage_service import log_usage

router = APIRouter()

# Model names used for token counting per provider
_PROVIDER_MODEL = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",   # cl100k_base fallback
    "gemini": "gemini-1.5-flash",  # cl100k_base fallback
}


@router.post("/generate")
async def generate(
    req: AIRequest,
    user_id: str = Depends(get_current_user),
):
    if req.mode == "image_translation" and not req.image_url:
        raise HTTPException(status_code=422, detail="image_url is required for image_translation mode")

    model = _PROVIDER_MODEL.get(req.provider, "deepseek-chat")
    estimated = count_tokens(req.prompt, model=model) + 500

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
            prompt=req.prompt,
            provider=req.provider,
            mode=req.mode,
            target_language=req.target_language,
            image_url=req.image_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    actual = count_tokens(req.prompt, model=model) + count_tokens(response, model=model)
    # Inline: usage feeds the daily/monthly token caps, so losing these writes
    # silently raises every user's effective limit. One insert against Supabase.
    await run_followup(
        log_usage(user_id, f"ai_tokens:{req.provider}:{req.mode}", actual),
        label=f"log_usage {user_id} {req.provider}",
    )

    return {"response": response, "tokens": actual, "provider": req.provider, "mode": req.mode}
