from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user
from app.models.schemas import CallRequest
from app.services.calling_service import generate_call_token
from app.services.usage_service import log_usage

router = APIRouter()


@router.post("/token")
async def create_call_token(
    req: CallRequest,
    user_id: str = Depends(get_current_user),
):
    # Plan 2.3a (§126): a learner whose missed payment is past its 7-day grace
    # gets no room token until it's paid. Fails open on a read error.
    from app.services.grace_service import is_lapsed
    if is_lapsed(user_id):
        raise HTTPException(status_code=402, detail={"reason": "payment_lapsed"})
    try:
        result = await generate_call_token(user_id, req.room, vendor=req.vendor)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create call token: {e}")

    # PY-002: usage logging must never raise (per CLAUDE.md).
    try:
        log_usage(user_id, "call_join", 1)
    except Exception:
        pass

    return {
        "token": result["token"],
        "room": req.room,
        "room_url": result.get("room_url"),
        "vendor": result["vendor"],
    }
