import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from app.core.followup import run_followup
from app.dependencies import get_current_user
from app.models.schemas import (
    ApproveRequest,
    OptInRequest,
    VisibilityToggleRequest,
)
from app.services import supporter_service

logger = logging.getLogger(__name__)
router = APIRouter()

_TRANSPARENT_GIF = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00"
    b"\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)


@router.post("/approve")
async def approve(req: ApproveRequest):
    """PUBLIC — supporter approves the platform for a referred student."""
    try:
        result = await supporter_service.approve_for_student(
            req.session_id, req.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Notify referral sender that their contact approved
    from app.services.supabase_client import supabase_admin as db
    session_data = db.schema("app").table("test_sessions").select("referral_code").eq(
        "id", req.session_id
    ).execute()
    if session_data.data and session_data.data[0].get("referral_code"):
        from app.services.referral_service import handle_approval
        code = session_data.data[0]["referral_code"]
        await run_followup(handle_approval(code), label=f"handle_approval {code}")

    return result


@router.post("/opt-in")
async def opt_in(req: OptInRequest):
    """PUBLIC — supporter agrees to receive milestone updates."""
    try:
        return await supporter_service.opt_in_supporter(req.supporter_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/opt-out/{supporter_id}", response_class=HTMLResponse)
async def opt_out(supporter_id: str):
    """PUBLIC — stop milestone emails (clicked from email link)."""
    await supporter_service.opt_out_supporter(supporter_id)
    return HTMLResponse(
        "<html><body><h2>You've been opted out</h2>"
        "<p>You will no longer receive milestone updates.</p></body></html>"
    )


@router.get("/unsubscribe/{supporter_id}", response_class=HTMLResponse)
async def unsubscribe(supporter_id: str):
    """PUBLIC — hard unsubscribe from all emails."""
    await supporter_service.unsubscribe_supporter(supporter_id)
    return HTMLResponse(
        "<html><body><h2>You've been unsubscribed</h2>"
        "<p>You will no longer receive any emails from Tauka.</p></body></html>"
    )


@router.get("/track/{supporter_id}/{event}")
async def track(supporter_id: str, event: str):
    """PUBLIC — tracking pixel/redirect for engagement scoring."""
    await supporter_service.track_engagement(supporter_id, event)

    if event in ("whatsapp_click", "email_click"):
        from fastapi.responses import RedirectResponse
        from app.config import settings
        return RedirectResponse(url=settings.web_base_url)

    # Return 1x1 transparent GIF for email open pixels
    return Response(content=_TRANSPARENT_GIF, media_type="image/gif")


@router.get("/my-supporters")
async def my_supporters(user_id: str = Depends(get_current_user)):
    """Return supporter list for the authenticated student."""
    return await supporter_service.get_student_supporters(user_id)


@router.patch("/visibility")
async def toggle_visibility(
    req: VisibilityToggleRequest,
    user_id: str = Depends(get_current_user),
):
    """Student toggles milestone sharing for a specific supporter."""
    try:
        return await supporter_service.toggle_supporter_visibility(
            user_id, req.supporter_id, req.visible
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
