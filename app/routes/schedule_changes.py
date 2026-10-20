"""Moving a pod's time with everyone's consent (enrolment plan 4.5, §113).

Learner: `GET /schedule-changes/mine`, `POST /schedule-changes/{id}/answer`.
Ops: `GET /schedule-changes`, `POST /schedule-changes`,
`POST /schedule-changes/{id}/withdraw`.
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import schedule_service
from app.services.schedule_service import ScheduleRefused

router = APIRouter()

_STATUS = {"not_allowed": 403, "no_such_pod": 404, "no_such_change": 404, "not_your_pod": 403,
           "already_proposed": 409, "already_answered": 409, "closed": 409, "not_open": 409}


def _refused(exc: ScheduleRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Proposal(BaseModel):
    cohort_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    kind: Literal["permanent", "temporary"]
    day_of_week: int = Field(..., ge=0, le=6)
    time: str = Field(..., pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    starts_on: date
    ends_on: Optional[date] = None
    reason: Literal["ramadan", "hudade", "tutor", "members", "other"]


class Answer(BaseModel):
    agree: bool


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return {"changes": schedule_service.mine(user_id)}


@router.post("/{change_id}/answer")
async def answer(change_id: str, req: Answer, user_id: str = Depends(get_current_user)):
    try:
        return schedule_service.answer(user_id, change_id, req.agree)
    except ScheduleRefused as exc:
        _refused(exc)


@router.get("")
async def queue(admin_id: str = Depends(require_admin)):
    try:
        return schedule_service.queue(admin_id)
    except ScheduleRefused as exc:
        _refused(exc)


@router.post("")
async def propose(req: Proposal, admin_id: str = Depends(require_admin)):
    try:
        return await schedule_service.propose(admin_id, req.cohort_id, req.kind, req.day_of_week, req.time,
                                              req.starts_on, req.ends_on, req.reason)
    except ScheduleRefused as exc:
        _refused(exc)


@router.post("/{change_id}/withdraw")
async def withdraw(change_id: str, admin_id: str = Depends(require_admin)):
    try:
        return schedule_service.withdraw(admin_id, change_id)
    except ScheduleRefused as exc:
        _refused(exc)
