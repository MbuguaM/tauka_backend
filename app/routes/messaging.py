from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user
from app.models.schemas import MessageRequest
from app.services.messaging_service import store_message
from app.services.rate_limit_service import check_rate_limit
from app.services.usage_service import log_usage

router = APIRouter()


@router.post("/send")
async def send_message(
    req: MessageRequest,
    user_id: str = Depends(get_current_user),
):
    tokens = len(req.content)

    allowed, reason = check_rate_limit(user_id, tokens, provider="messaging")
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    message = await store_message(req.conversation_id, user_id, req.content)

    # PY-002: usage logging must never raise (per CLAUDE.md). The service guards
    # internally, but wrap here too so a transient failure cannot 500 the client.
    try:
        log_usage(user_id, "message", tokens)
    except Exception:
        pass

    return {"status": "sent", "message": message}
