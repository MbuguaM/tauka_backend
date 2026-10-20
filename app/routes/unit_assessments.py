"""Unit assessments (§141): one per block of ten lessons, a quiz and a speaking check.

Learner: `GET /unit-assessments` (opens any block reached), `POST /unit-assessments/{id}/quiz`.
Tutor (linked or pod tutor) or ops: `GET /unit-assessments/to-rate`, `POST /unit-assessments/{id}/speaking`.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import unit_assessment_service as svc
from app.services.unit_assessment_service import UnitAssessmentRefused

router = APIRouter()
_STATUS = {"not_found": 404, "not_your_assessment": 403, "not_allowed": 403, "already_taken": 409,
           "already_rated": 409, "bad_score": 422}


def _do(fn):
    try:
        return fn()
    except UnitAssessmentRefused as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Quiz(BaseModel):
    correct: int = Field(..., ge=0, le=40)
    total: int = Field(..., ge=5, le=40)
    weak_lessons: list[int] = Field(default_factory=list, max_length=10)


class Speaking(BaseModel):
    pronunciation: int = Field(..., ge=1, le=4)
    accuracy: int = Field(..., ge=1, le=4)
    fluency: int = Field(..., ge=1, le=4)
    comprehension: int = Field(..., ge=1, le=4)


@router.get("")
async def mine(user_id: str = Depends(get_current_user)):
    return svc.mine(user_id)


@router.post("/{assessment_id}/quiz")
async def quiz(assessment_id: str, req: Quiz, user_id: str = Depends(get_current_user)):
    return _do(lambda: svc.submit_quiz(user_id, assessment_id, req.correct, req.total, req.weak_lessons))


@router.get("/to-rate")
async def to_rate(user_id: str = Depends(get_current_user)):
    return {"assessments": svc.to_rate(user_id)}


@router.post("/{assessment_id}/speaking")
async def speaking(assessment_id: str, req: Speaking, user_id: str = Depends(get_current_user)):
    return _do(lambda: svc.rate_speaking(user_id, assessment_id, req.model_dump()))
