from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from app.dependencies import get_current_user
from app.services import off_desktop_service, session_join_service
from app.services.session_join_service import JoinRefused

router = APIRouter()


_JOIN_STATUS = {"no_such_session": 404, "not_in_session": 403, "not_open": 409, "closed": 409, "not_livekit": 409,
                "not_configured": 503}


@router.post("/sessions/{video_session_id}/join")
async def join_session(video_session_id: str, user_id: str = Depends(get_current_user)):
    """This person's way into a scheduled LiveKit session: a token, for someone in it, near its time."""
    from app.services.grace_service import is_lapsed
    if is_lapsed(user_id):
        raise HTTPException(status_code=402, detail={"reason": "payment_lapsed"})
    try:
        return session_join_service.join(user_id, video_session_id)
    except JoinRefused as exc:
        raise HTTPException(status_code=_JOIN_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class OffDesktopRequest(BaseModel):
    platform: str


@router.post("/sessions/{video_session_id}/off-desktop")
async def report_off_desktop(video_session_id: str, req: OffDesktopRequest, background: BackgroundTasks,
                             user_id: str = Depends(get_current_user)):
    """A tutor chose to teach this session from a phone (D16). Told once per session."""
    try:
        row = off_desktop_service.record(user_id, video_session_id, req.platform)
    except off_desktop_service.OffDesktopRefused as exc:
        status = {"no_such_session": 404, "not_the_tutor": 403}.get(exc.reason, 400)
        raise HTTPException(status_code=status, detail={"reason": exc.reason})
    if row:
        background.add_task(off_desktop_service.notify, row)
    return {"recorded": bool(row)}
