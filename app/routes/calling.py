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
    try:
        result = await generate_call_token(user_id, req.room, vendor=req.vendor)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create call token: {e}")

    log_usage(user_id, "call_join", 1)

    return {
        "token": result["token"],
        "room": req.room,
        "room_url": result.get("room_url"),
        "vendor": result["vendor"],
    }
