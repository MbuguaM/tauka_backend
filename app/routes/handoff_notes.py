"""Handoff notes (enrolment plan 4.3, §119).

Learner: `GET /handoff-notes/mine`. Tutor: `GET /handoff-notes/to-write`,
`GET /handoff-notes/guest-sessions`, `GET /handoff-notes/{student_id}`,
`PUT`-style `POST /handoff-notes/{student_id}`. Who may read or write is
decided in `handoff_note_service` (and `app.can_read_handoff_note`).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import handoff_note_service as hn
from app.services.handoff_note_service import HandoffNoteRefused

router = APIRouter()

_STATUS = {"not_allowed": 403, "not_home_tutor": 403}


def _refused(exc: HandoffNoteRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Note(BaseModel):
    recurring_errors: Optional[str] = Field(None, max_length=280)
    goals: Optional[str] = Field(None, max_length=280)
    confidence: Literal["hesitant", "building", "confident"]


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    """The learner's own note: what their tutor wrote about them."""
    return {"note": hn.get(user_id, user_id)}


@router.get("/to-write")
async def to_write(user_id: str = Depends(get_current_user)):
    return {"learners": hn.to_write(user_id)}


@router.get("/guest-sessions")
async def guest_sessions(user_id: str = Depends(get_current_user)):
    return {"sessions": hn.guest_sessions(user_id)}


@router.get("/{student_id}")
async def read(student_id: str, user_id: str = Depends(get_current_user)):
    try:
        return {"note": hn.get(user_id, student_id)}
    except HandoffNoteRefused as exc:
        _refused(exc)


@router.post("/{student_id}")
async def save(student_id: str, req: Note, user_id: str = Depends(get_current_user)):
    try:
        return {"note": hn.save(user_id, student_id, req.recurring_errors, req.goals, req.confidence)}
    except HandoffNoteRefused as exc:
        _refused(exc)
