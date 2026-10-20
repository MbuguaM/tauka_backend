"""Session complaints (enrolment plan §2f item 7, migration §106).

Learner: `GET /complaints/sessions` (what they can complain about),
`POST /complaints`, `GET /complaints/mine`. Admin (ops or safeguarding, by
routing): `GET /complaints`, `POST /complaints/{id}/decide`.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import complaint_service
from app.services.complaint_service import ComplaintRefused

router = APIRouter()

_STATUS = {"no_such_complaint": 404, "not_allowed": 403, "already_complained": 409, "decided": 409,
           "not_your_session": 403}


def _refused(exc: ComplaintRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class ComplaintCreate(BaseModel):
    session_type: Literal["cohort_session", "makeup_session", "clinic", "one_to_one_session"]
    session_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    reason_code: Literal["tutor_absent", "tutor_late", "cut_short", "connection", "conduct", "other"]
    detail: Optional[str] = Field(None, max_length=1000)


class Decision(BaseModel):
    upheld: bool


@router.get("/sessions")
async def sessions(user_id: str = Depends(get_current_user)):
    return {"sessions": complaint_service.recent_sessions(user_id)}


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return {"complaints": complaint_service.mine(user_id)}


@router.post("")
async def create(req: ComplaintCreate, user_id: str = Depends(get_current_user)):
    try:
        return complaint_service.create(user_id, req.session_type, req.session_id, req.reason_code, req.detail)
    except ComplaintRefused as exc:
        _refused(exc)


@router.get("")
async def queue(admin_id: str = Depends(require_admin)):
    try:
        return {"complaints": complaint_service.queue(admin_id)}
    except ComplaintRefused as exc:
        _refused(exc)


@router.post("/{complaint_id}/decide")
async def decide(complaint_id: str, req: Decision, admin_id: str = Depends(require_admin)):
    try:
        return complaint_service.decide(admin_id, complaint_id, req.upheld)
    except ComplaintRefused as exc:
        _refused(exc)
