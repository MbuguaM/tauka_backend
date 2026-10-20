"""Fidel approval (enrolment plan 1.4, migration §107).

Learner: `GET /fidel/mine?unit_id=`, `POST /fidel/consent`, `POST /fidel/submissions`
(after uploading the take through the speech pipeline). Reviewer (rung >= 1 tutor
or ops): `GET /fidel/queue`, `POST /fidel/{id}/review`. The caller comes from the
JWT; who may do what is decided in `fidel_service`.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import fidel_service
from app.services.fidel_service import FidelRefused

router = APIRouter()
_UUID = r"^[0-9a-fA-F-]{36}$"
_STATUS = {"not_allowed": 403, "consent_required": 403, "no_such_reading": 404, "no_such_submission": 404,
           "decided": 409}


def _refused(exc: FidelRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class ConsentRequest(BaseModel):
    granted: bool


class SubmissionRequest(BaseModel):
    unit_id: str = Field(..., pattern=_UUID)
    group_index: int = Field(..., ge=0, le=20)
    group_title: Optional[str] = Field(None, max_length=120)
    storage_path: str = Field(..., pattern=r"^speech/[0-9a-fA-F-]{36}/.+\.zip$", max_length=300)


class ReviewRequest(BaseModel):
    approved: bool
    gap_codes: list[Literal["fidel_recognition", "vowel_orders", "ejectives", "gemination", "reading_fluency"]] = []


@router.get("/mine")
async def mine(unit_id: str = Query(..., pattern=_UUID), user_id: str = Depends(get_current_user)):
    return fidel_service.mine(user_id, unit_id)


@router.post("/consent")
async def consent(req: ConsentRequest, user_id: str = Depends(get_current_user)):
    return {"consented": fidel_service.set_consent(user_id, req.granted)}


@router.post("/submissions")
async def submit(req: SubmissionRequest, user_id: str = Depends(get_current_user)):
    try:
        return fidel_service.submit(user_id, req.unit_id, req.group_index, req.group_title, req.storage_path)
    except FidelRefused as exc:
        _refused(exc)


@router.get("/queue")
async def queue(user_id: str = Depends(get_current_user)):
    try:
        return {"readings": fidel_service.queue(user_id)}
    except FidelRefused as exc:
        _refused(exc)


@router.post("/{approval_id}/review")
async def review(approval_id: str, req: ReviewRequest, user_id: str = Depends(get_current_user)):
    try:
        return fidel_service.review(user_id, approval_id, req.approved, list(req.gap_codes))
    except FidelRefused as exc:
        _refused(exc)
