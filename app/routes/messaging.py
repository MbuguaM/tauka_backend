from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user
from app.models.schemas import MessageRequest
from app.services.messaging_service import store_message
from app.services.rate_limit_service import check_rate_limit
from app.services.usage_service import log_usage

router = APIRouter()


@router.post("/send")
def send_message(
    req: MessageRequest,
    user_id: str = Depends(get_current_user),
):
    tokens = len(req.content)

    allowed, reason = check_rate_limit(user_id, tokens, provider="messaging")
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    store_message(req.conversation_id, user_id, req.content)
    log_usage(user_id, "message", tokens)

    return {"status": "sent"}
