"""Concern reports (enrolment plan 4.6, §117) and tutors' rating averages (§116).

Learner: `GET /concerns/people`, `POST /concerns`, `GET /concerns/mine`.
Safeguarding: `GET /concerns`, `POST /concerns/{id}/acknowledge`,
`POST /concerns/{id}/close`. Tutor: `GET /concerns/ratings/mine`.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import concern_service, rating_service
from app.services.concern_service import ConcernRefused

router = APIRouter()

_STATUS = {"not_allowed": 403, "no_such_concern": 404, "not_open": 409, "closed": 409,
           "unknown_person": 400, "not_your_session": 403}


def _refused(exc: ConcernRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class ConcernCreate(BaseModel):
    about_kind: Literal["tutor", "learner", "someone_else", "myself"]
    about_user_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    category: Literal["harassment", "contact_details", "inappropriate_content", "wellbeing", "safety", "other"]
    detail: Optional[str] = Field(None, max_length=1000)
    session_type: Optional[Literal["cohort_session", "makeup_session", "clinic", "one_to_one_session"]] = None
    session_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")


class Close(BaseModel):
    outcome: Literal["no_action", "advice_given", "warning_issued", "removed", "escalated"]


@router.get("/people")
async def people(user_id: str = Depends(get_current_user)):
    return {"people": concern_service.people(user_id)}


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return {"concerns": concern_service.mine(user_id)}


@router.post("")
async def create(req: ConcernCreate, user_id: str = Depends(get_current_user)):
    try:
        row = concern_service.create(user_id, req.about_kind, req.about_user_id, req.category, req.detail,
                                     req.session_type, req.session_id)
    except ConcernRefused as exc:
        _refused(exc)
    # The safeguarding alert, inline and bounded; the daily job resends a failure.
    from app.core.followup import run_followup
    await run_followup(concern_service.alert_new(row["id"]), label=f"concern alert {row['id']}")
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"]}


@router.get("/ratings/mine")
async def my_ratings(user_id: str = Depends(get_current_user)):
    """A tutor's last-30-day averages, only once 3+ learners have rated (plan 4.6)."""
    return rating_service.for_tutor(user_id)


@router.get("")
async def queue(admin_id: str = Depends(require_admin)):
    try:
        return {"concerns": concern_service.queue(admin_id)}
    except ConcernRefused as exc:
        _refused(exc)


@router.post("/{concern_id}/acknowledge")
async def acknowledge(concern_id: str, admin_id: str = Depends(require_admin)):
    try:
        return concern_service.acknowledge(admin_id, concern_id)
    except ConcernRefused as exc:
        _refused(exc)


@router.post("/{concern_id}/close")
async def close(concern_id: str, req: Close, admin_id: str = Depends(require_admin)):
    try:
        return concern_service.close(admin_id, concern_id, req.outcome)
    except ConcernRefused as exc:
        _refused(exc)
