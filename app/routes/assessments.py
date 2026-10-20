"""Exit assessments and certificates (enrolment plan Track 10, §138), behind
the exit_assessment flag.

Learner: `GET /assessments`, `POST /assessments/request`.
Rater (rung 4): `POST /assessments/{id}/rate`.
Ops: `GET /assessments/ops`, `POST /assessments/{id}/schedule`.
Public: `GET /public/certificates/{code}` (on the public router).
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import assessment_service as svc
from app.services.assessment_service import AssessmentRefused

router = APIRouter()
public_router = APIRouter()
_STATUS = {"not_enabled": 403, "not_allowed": 403, "not_your_assessment": 403, "already_open": 409,
           "not_open": 409, "already_rated": 409, "not_yet_eligible": 409}
Cefr = Literal["A1", "A2", "B1", "B2", "C1", "C2"]


def _refused(exc: AssessmentRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Schedule(BaseModel):
    at: datetime
    rater_1: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    rater_2: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


class Rating(BaseModel):
    listening: Cefr
    speaking: Cefr
    reading: Cefr
    writing: Cefr
    overall: Cefr


@router.get("")
async def mine(user_id: str = Depends(get_current_user)):
    return svc.mine(user_id)


@router.post("/request")
async def request(user_id: str = Depends(get_current_user)):
    try:
        return svc.request(user_id)
    except AssessmentRefused as exc:
        _refused(exc)


@router.get("/to-rate")
async def to_rate(user_id: str = Depends(get_current_user)):
    return {"assessments": svc.to_rate(user_id)}


@router.post("/{assessment_id}/rate")
async def rate(assessment_id: str, req: Rating, user_id: str = Depends(get_current_user)):
    try:
        return svc.rate(user_id, assessment_id, req.model_dump())
    except AssessmentRefused as exc:
        _refused(exc)


@router.get("/ops")
async def ops(admin_id: str = Depends(require_admin)):
    try:
        return svc.ops_list(admin_id)
    except AssessmentRefused as exc:
        _refused(exc)


@router.post("/{assessment_id}/schedule")
async def schedule(assessment_id: str, req: Schedule, admin_id: str = Depends(require_admin)):
    try:
        return svc.schedule(admin_id, assessment_id, req.at, req.rater_1, req.rater_2)
    except AssessmentRefused as exc:
        _refused(exc)


@public_router.get("/certificates/{code}")
async def verify(code: str):
    out = svc.verify(code)
    if not out:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    return out
