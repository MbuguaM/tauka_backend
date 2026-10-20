"""Peer sessions (enrolment plan Track 5, §135), behind the peer_sessions flag.

Learner: `GET /peer` ({enabled:false} until on), `POST /peer/opt-in {on}`,
`POST /peer/sessions/{id}/join`, `POST /peer/sessions/{id}/mark {partner_came}`,
`POST /peer/sessions/{id}/report {category, detail?}`, `POST /peer/sessions/{id}/block`,
`POST /peer/now`, `GET /peer/now`, `DELETE /peer/now`.
Tutors rung >= 3 / ops: `GET /peer/tasks`, `POST /peer/tasks`, `POST /peer/tasks/draft`,
`POST /peer/tasks/{id}/review` (X-13, §142: partner-side cards, AI drafts).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import peer_service as svc
from app.services.peer_service import PeerRefused

router = APIRouter()
_STATUS = {"not_enabled": 403, "not_allowed": 403, "not_your_session": 403, "not_now": 409, "not_started": 409,
           "already_marked": 409, "no_room": 503, "no_pod": 409, "not_in_pilot": 409,
           "not_found": 404, "no_such_lesson": 404, "draft_rejected": 502}


def _refused(exc: PeerRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class OptIn(BaseModel):
    on: bool


class Mark(BaseModel):
    partner_came: bool


class Report(BaseModel):
    category: Literal["harassment", "contact_details", "inappropriate_content", "wellbeing", "safety", "other"]
    detail: Optional[str] = Field(None, max_length=1000)


class Task(BaseModel):
    lesson_number: Optional[int] = Field(None, ge=0, le=200)
    kind: Literal["information_gap", "role_play", "question_set"]
    title: str = Field(..., min_length=3, max_length=120)
    prompt_a: str = Field(..., min_length=3, max_length=2000)
    prompt_b: Optional[str] = Field(None, max_length=2000)
    course_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    # X-13 (§142): partner-side cards are per English level; corrections = "useful corrections".
    side: Literal["learner", "partner"] = "learner"
    english_level: Optional[Literal["A2", "B1", "B2", "C1"]] = None
    corrections: Optional[str] = Field(None, max_length=1000)


class Draft(BaseModel):
    lesson_number: int = Field(..., ge=1, le=200)


class TaskReview(BaseModel):
    activate: bool
    title: Optional[str] = Field(None, max_length=120)
    prompt_a: Optional[str] = Field(None, max_length=2000)
    prompt_b: Optional[str] = Field(None, max_length=2000)
    corrections: Optional[str] = Field(None, max_length=1000)


@router.get("")
async def mine(user_id: str = Depends(get_current_user)):
    return svc.mine(user_id)


@router.post("/opt-in")
async def opt_in(req: OptIn, user_id: str = Depends(get_current_user)):
    try:
        return svc.set_optin(user_id, req.on)
    except PeerRefused as exc:
        _refused(exc)


@router.post("/sessions/{session_id}/join")
async def join(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await svc.join(user_id, session_id)
    except PeerRefused as exc:
        _refused(exc)


@router.post("/sessions/{session_id}/mark")
async def mark(session_id: str, req: Mark, user_id: str = Depends(get_current_user)):
    try:
        return svc.mark(user_id, session_id, req.partner_came)
    except PeerRefused as exc:
        _refused(exc)


@router.post("/sessions/{session_id}/report")
async def report(session_id: str, req: Report, user_id: str = Depends(get_current_user)):
    from app.core.followup import run_followup
    from app.services import concern_service
    try:
        row = svc.report(user_id, session_id, req.category, req.detail)
    except PeerRefused as exc:
        _refused(exc)
    await run_followup(concern_service.alert_new(row["id"]), label=f"concern alert {row['id']}")
    return row


@router.post("/sessions/{session_id}/block")
async def block(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return svc.block(user_id, session_id)
    except PeerRefused as exc:
        _refused(exc)


@router.post("/now")
async def now(user_id: str = Depends(get_current_user)):
    try:
        return svc.practise_now(user_id)
    except PeerRefused as exc:
        _refused(exc)


@router.get("/now")
async def now_status(user_id: str = Depends(get_current_user)):
    return svc.now_status(user_id)


@router.delete("/now")
async def leave(user_id: str = Depends(get_current_user)):
    svc.leave_queue(user_id)
    return {"ok": True}


@router.post("/now/leave")
async def leave_post(user_id: str = Depends(get_current_user)):
    """The same as DELETE /peer/now, for clients without a DELETE helper."""
    svc.leave_queue(user_id)
    return {"ok": True}


@router.get("/tasks")
async def tasks(user_id: str = Depends(get_current_user)):
    """Authors see every card (both sides, drafts too); anyone else the live learner cards."""
    try:
        return {"tasks": svc.all_tasks(user_id)}
    except PeerRefused:
        return {"tasks": svc.tasks()}


@router.post("/tasks")
async def add_task(req: Task, user_id: str = Depends(get_current_user)):
    try:
        return svc.add_task(user_id, req.lesson_number, req.kind, req.title, req.prompt_a, req.prompt_b, req.course_id,
                            req.side, req.english_level, req.corrections)
    except PeerRefused as exc:
        _refused(exc)


@router.post("/tasks/draft")
async def draft_tasks(req: Draft, user_id: str = Depends(get_current_user)):
    """X-13: three AI drafts for a lesson, inactive until reviewed."""
    try:
        return {"tasks": await svc.draft_tasks(user_id, req.lesson_number)}
    except PeerRefused as exc:
        _refused(exc)


@router.post("/tasks/{task_id}/review")
async def review_task(task_id: str, req: TaskReview, user_id: str = Depends(get_current_user)):
    try:
        edits = {k: v for k, v in req.model_dump().items() if k != "activate" and v is not None}
        return svc.review_task(user_id, task_id, req.activate, edits or None)
    except PeerRefused as exc:
        _refused(exc)
