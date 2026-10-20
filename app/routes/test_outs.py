"""Heritage test-outs of Phase 0 (enrolment plan 1.6 / 1.7, §2k, migration §99).

Who may do what is decided in `test_out_service` against the database's own
rules (capabilities, rungs, links); every route here takes the caller from the
JWT and never from the body. Refusals come back as a stable word.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import test_out_service
from app.services.test_out_service import TestOutRefused

logger = logging.getLogger(__name__)
router = APIRouter()

_UUID = r"^[0-9a-fA-F-]{36}$"


class TestOutCreate(BaseModel):
    # Optional for staff: the learner's only course is used. A learner asking
    # for their own test-out sends it (the app knows the course on screen).
    course_id: Optional[str] = Field(None, pattern=_UUID)
    # Omitted = the caller is the learner asking for their own test-out.
    student_id: Optional[str] = Field(None, pattern=_UUID)
    assessor_tutor_id: Optional[str] = Field(None, pattern=_UUID)


class AssessorRequest(BaseModel):
    tutor_id: str = Field(..., pattern=_UUID)


class ResultRequest(BaseModel):
    result: Literal["passed", "not_yet"]
    gap_codes: list[Literal["fidel_recognition", "vowel_orders", "ejectives", "gemination", "reading_fluency"]] = []


def _refused(exc: TestOutRefused):
    status = {"course_required": 400, "no_such_request": 404, "no_such_student": 404, "not_allowed": 403,
              "ops_required_for_new_link": 403, "already_open": 409, "closed": 409,
              "not_assigned": 409, "phase0_already_done": 409}.get(exc.reason, 400)
    detail = {"reason": exc.reason}
    if exc.detail:
        detail["blocking"] = exc.detail
    raise HTTPException(status_code=status, detail=detail)


@router.post("")
async def create(req: TestOutCreate, background: BackgroundTasks,
                 user_id: str = Depends(get_current_user)):
    """The learner, a rung-4 tutor, an owner or an ops admin sends a request.
    Staff-sent requests email the learner; an assignment emails the tutor."""
    try:
        row = await test_out_service.create(user_id, req.student_id or user_id, req.course_id,
                                            req.assessor_tutor_id)
    except TestOutRefused as exc:
        _refused(exc)
    background.add_task(test_out_service.notify_learner, row)
    if row and row.get("status") == "assigned":
        background.add_task(test_out_service.notify_assessor, row)
    return row


@router.get("")
async def mine(user_id: str = Depends(get_current_user)):
    """The caller's requests (with names) and what the caller may do."""
    return test_out_service.overview(user_id)


@router.get("/candidates")
async def candidates(user_id: str = Depends(get_current_user)):
    """Learners in Phase 0 a staff member could send a test-out to."""
    try:
        return {"learners": test_out_service.candidates(user_id)}
    except TestOutRefused as exc:
        _refused(exc)


@router.get("/{request_id}/assessors")
async def assessors(request_id: str, user_id: str = Depends(get_current_user)):
    """Tutors who could assess this request (rung >= 2, active)."""
    try:
        return {"tutors": test_out_service.assessors(user_id, request_id)}
    except TestOutRefused as exc:
        _refused(exc)


@router.post("/{request_id}/assessor")
async def assign(request_id: str, req: AssessorRequest, background: BackgroundTasks,
                 user_id: str = Depends(get_current_user)):
    try:
        row = await test_out_service.assign(user_id, request_id, req.tutor_id)
    except TestOutRefused as exc:
        _refused(exc)
    background.add_task(test_out_service.notify_assessor, row)
    return row


@router.post("/{request_id}/result")
async def result(request_id: str, req: ResultRequest, user_id: str = Depends(get_current_user)):
    try:
        return test_out_service.record_result(user_id, request_id, req.result, list(req.gap_codes))
    except TestOutRefused as exc:
        _refused(exc)


@router.post("/{request_id}/cancel")
async def cancel(request_id: str, user_id: str = Depends(get_current_user)):
    try:
        return test_out_service.cancel(user_id, request_id)
    except TestOutRefused as exc:
        _refused(exc)
