"""Pod norms and the two-week fit survey (enrolment plan 4.1, §118).

Tutor: `GET /pods/norms/pending`, `POST /pods/{cohort_id}/norms`.
Learner: `GET /pods/norms/mine`, `GET /pods/fit-survey/due`,
`POST /pods/{cohort_id}/fit-survey`.
Ops: `GET /pods/fit-surveys`, `POST /pods/{cohort_id}/fit-surveys/{student_id}/review`.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_current_user, require_admin
from app.services import pod_fit_service
from app.services.pod_fit_service import PodFitRefused

router = APIRouter()

_STATUS = {"not_allowed": 403, "not_your_pod": 403, "not_due": 409, "reviewed": 409, "no_such_survey": 404}


def _refused(exc: PodFitRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Norms(BaseModel):
    camera: Literal["on", "when_possible", "optional"]
    lateness: Literal["on_time", "door_5", "door_10"]
    language: Literal["amharic_only", "amharic_first", "either"]


class Survey(BaseModel):
    level_fit: Literal["too_easy", "right", "too_hard"]
    time_fit: Literal["works", "sometimes", "doesnt_work"]
    group_fit: Literal["comfortable", "mostly", "not_comfortable"]
    staying: Literal["stay", "not_sure", "move"]


class Review(BaseModel):
    action: Literal["none", "moved"]


@router.get("/norms/pending")
async def pending(user_id: str = Depends(get_current_user)):
    return {"pods": pod_fit_service.pending_for_tutor(user_id)}


@router.get("/norms/mine")
async def my_norms(user_id: str = Depends(get_current_user)):
    return {"norms": pod_fit_service.for_member(user_id)}


@router.post("/{cohort_id}/norms")
async def set_norms(cohort_id: str, req: Norms, user_id: str = Depends(get_current_user)):
    try:
        return pod_fit_service.set_norms(user_id, cohort_id, req.camera, req.lateness, req.language)
    except PodFitRefused as exc:
        _refused(exc)


@router.get("/fit-survey/due")
async def survey_due(user_id: str = Depends(get_current_user)):
    return {"due": pod_fit_service.due(user_id)}


@router.post("/{cohort_id}/fit-survey")
async def survey(cohort_id: str, req: Survey, user_id: str = Depends(get_current_user)):
    try:
        return pod_fit_service.submit(user_id, cohort_id, req.model_dump())
    except PodFitRefused as exc:
        _refused(exc)


@router.get("/fit-surveys")
async def queue(admin_id: str = Depends(require_admin)):
    try:
        return {"surveys": pod_fit_service.queue(admin_id)}
    except PodFitRefused as exc:
        _refused(exc)


@router.post("/{cohort_id}/fit-surveys/{student_id}/review")
async def review(cohort_id: str, student_id: str, req: Review, admin_id: str = Depends(require_admin)):
    try:
        return pod_fit_service.review(admin_id, cohort_id, student_id, req.action)
    except PodFitRefused as exc:
        _refused(exc)
