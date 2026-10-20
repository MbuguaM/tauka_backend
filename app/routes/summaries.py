"""Learner-language lesson summaries (features addendum B, §143).

Learner: `GET /summaries/prefs` (the app writes its own user_profiles row), `GET /summaries/lesson/{unit_id}`,
`POST /summaries/lesson/{unit_id}/event {event}`, `POST /summaries/{id}/flag {reason?}`,
`POST /summaries/lesson/{unit_id}/ask {question}` (behind summary_questions).
Reviewers (content / ops / rung >= 3): `GET /summaries/review`, `GET /summaries/review/{id}`,
`POST /summaries/review/{id} {action, bullets?, glosses?}`. Ops: `GET /summaries/dashboard`.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import summary_service as svc
from app.services.summary_service import SummaryRefused

router = APIRouter()
_STATUS = {"not_enabled": 403, "not_allowed": 403, "no_such_lesson": 404, "not_found": 404,
           "limit_reached": 429, "unsupported_language": 409, "generation_rejected": 502}


def _refused(exc: SummaryRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Event(BaseModel):
    event: Literal["view", "expand"]


class Flag(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


class Ask(BaseModel):
    question: str = Field(..., min_length=1, max_length=600)


class Review(BaseModel):
    action: Literal["approve", "edit", "regenerate"]
    bullets: Optional[list[str]] = Field(None, max_length=6)
    glosses: Optional[dict[str, str]] = None


@router.get("/prefs")
async def get_prefs(user_id: str = Depends(get_current_user)):
    return svc.prefs(user_id)


@router.get("/lesson/{unit_id}")
async def lesson(unit_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await svc.for_learner(user_id, unit_id)
    except SummaryRefused as exc:
        _refused(exc)


@router.post("/lesson/{unit_id}/event")
async def event(unit_id: str, req: Event, user_id: str = Depends(get_current_user)):
    lang = svc.prefs(user_id).get("summary_language")
    if lang:
        svc.record(user_id, unit_id, lang, req.event)
    return {"ok": True}


@router.post("/lesson/{unit_id}/ask")
async def ask(unit_id: str, req: Ask, user_id: str = Depends(get_current_user)):
    try:
        return await svc.ask(user_id, unit_id, req.question)
    except SummaryRefused as exc:
        _refused(exc)


@router.post("/{summary_id}/flag")
async def flag(summary_id: str, req: Flag, user_id: str = Depends(get_current_user)):
    try:
        return svc.flag(user_id, summary_id, req.reason)
    except SummaryRefused as exc:
        _refused(exc)


@router.get("/review")
async def review_queue(user_id: str = Depends(get_current_user)):
    try:
        return svc.queue(user_id)
    except SummaryRefused as exc:
        _refused(exc)


@router.get("/review/{summary_id}")
async def review_one(summary_id: str, user_id: str = Depends(get_current_user)):
    try:
        return svc.get(user_id, summary_id)
    except SummaryRefused as exc:
        _refused(exc)


@router.post("/review/{summary_id}")
async def review(summary_id: str, req: Review, user_id: str = Depends(get_current_user)):
    try:
        return svc.review(user_id, summary_id, req.action, req.bullets, req.glosses)
    except SummaryRefused as exc:
        _refused(exc)


@router.get("/dashboard")
async def dashboard(user_id: str = Depends(get_current_user)):
    try:
        return svc.dashboard(user_id)
    except SummaryRefused as exc:
        _refused(exc)
