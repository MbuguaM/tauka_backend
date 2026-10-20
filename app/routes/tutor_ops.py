"""The tutor side (enrolment plan 8.3, 8.4, 8.7, 8.8; §134).

Tutor: `GET /tutor-ops/me`, `POST /tutor-ops/commit`, `POST /tutor-ops/holidays`,
`GET /tutor-ops/qa`, `POST /tutor-ops/qa/{id}`, `GET|POST /tutor-ops/profile`.
Ops: `GET /tutor-ops/holidays/pending`, `POST /tutor-ops/holidays/{id}/decide`,
`POST /tutor-ops/work`.
Public (no sign-in): `GET /public/tutors`, `GET /public/tutors/{slug}` — the
`public_router`, mounted at /public.
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import tutor_ops_service as svc
from app.services.tutor_ops_service import TutorOpsRefused

router = APIRouter()
public_router = APIRouter()
_STATUS = {"not_a_tutor": 403, "not_allowed": 403, "not_yours": 403, "not_enabled": 403, "not_pending": 409,
           "not_open": 409, "overlaps": 409, "slug_taken": 409}


def _refused(exc: TutorOpsRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Commit(BaseModel):
    starts_on: date
    hours_per_week: int = Field(..., ge=1, le=60)


class Holiday(BaseModel):
    starts_on: date
    ends_on: date


class Decide(BaseModel):
    approve: bool


class Work(BaseModel):
    tutor_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    kind: Literal["gathering", "other"]
    occurred_on: date
    quantity: int = Field(1, ge=1, le=50)
    ref_code: Optional[str] = Field(None, max_length=60)


class QaScores(BaseModel):
    structure: int = Field(..., ge=1, le=4)
    speaking_time: int = Field(..., ge=1, le=4)
    correction: int = Field(..., ge=1, le=4)
    warmth: int = Field(..., ge=1, le=4)
    target_language: int = Field(..., ge=1, le=4)


class Profile(BaseModel):
    slug: str = Field(..., min_length=3, max_length=60)
    headline: Optional[str] = Field(None, max_length=120)
    bio: Optional[str] = Field(None, max_length=1200)
    published: bool = False


@router.get("/me")
async def me(user_id: str = Depends(get_current_user)):
    return svc.mine(user_id)


@router.post("/commit")
async def commit(req: Commit, user_id: str = Depends(get_current_user)):
    try:
        return svc.commit(user_id, req.starts_on, req.hours_per_week)
    except TutorOpsRefused as exc:
        _refused(exc)


@router.post("/holidays")
async def holiday(req: Holiday, user_id: str = Depends(get_current_user)):
    try:
        return svc.declare_holiday(user_id, req.starts_on, req.ends_on)
    except TutorOpsRefused as exc:
        _refused(exc)


@router.get("/holidays/pending")
async def pending(admin_id: str = Depends(require_admin)):
    try:
        return {"holidays": svc.pending_holidays(admin_id)}
    except TutorOpsRefused as exc:
        _refused(exc)


@router.post("/holidays/{holiday_id}/decide")
async def decide(holiday_id: str, req: Decide, admin_id: str = Depends(require_admin)):
    try:
        return svc.decide_holiday(admin_id, holiday_id, req.approve)
    except TutorOpsRefused as exc:
        _refused(exc)


@router.post("/work")
async def work(req: Work, admin_id: str = Depends(require_admin)):
    try:
        return svc.add_work(admin_id, req.tutor_id, req.kind, req.occurred_on, req.quantity, req.ref_code)
    except TutorOpsRefused as exc:
        _refused(exc)


@router.get("/qa")
async def qa(user_id: str = Depends(get_current_user)):
    return svc.my_qa(user_id)


@router.post("/qa/{review_id}")
async def qa_submit(review_id: str, req: QaScores, user_id: str = Depends(get_current_user)):
    try:
        return svc.submit_qa(user_id, review_id, req.model_dump())
    except TutorOpsRefused as exc:
        _refused(exc)


@router.get("/profile")
async def my_profile(user_id: str = Depends(get_current_user)):
    from app.services.supabase_client import supabase_admin as db
    rows = (db.schema("app").table("tutor_public_profile").select("slug").eq("tutor_id", user_id).limit(1)
            .execute()).data or []
    return svc.public_profile(rows[0]["slug"], include_unpublished_for=user_id) if rows else {}


@router.post("/profile")
async def save_profile(req: Profile, user_id: str = Depends(get_current_user)):
    try:
        return svc.save_profile(user_id, req.slug, req.headline, req.bio, req.published)
    except TutorOpsRefused as exc:
        _refused(exc)


@public_router.get("/tutors")
async def public_tutors():
    return {"tutors": svc.public_index()}


@public_router.get("/tutors/{slug}")
async def public_tutor(slug: str):
    p = svc.public_profile(slug)
    if not p:
        raise HTTPException(status_code=404)
    return p
