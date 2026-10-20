"""Make-up sessions (enrolment plan §2f/§2g, migrations §92/§93/§103).

Admin: `/proposed`, `/{id}/schedule`, `/dry-run`. Since §103 also `/mine`
(tutor or learner) and `/{id}/attendance` (the session's tutor or ops) — see
`makeup_delivery`.

The planner runs daily from `/tasks/daily` and PROPOSES a tutor and a time for
each group. Nothing is booked until an admin confirms here — the playbook's
"run it by hand first" rule. A confirmation, or an override, is held to the same
rules the planner follows: the tutor must be in every learner's own pool (home
or guest) and free at that time, and the session must end before its deadline.
"""

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_current_user, require_admin
from app.services import makeup_delivery, makeup_service
from app.services.makeup_delivery import MakeupRefused
from app.services.makeup_service import ScheduleRefused

router = APIRouter()


class ScheduleRequest(BaseModel):
    # Both omitted = confirm the planner's proposal as it stands.
    tutor_id: str | None = None
    starts_at: str | None = None   # ISO 8601 with offset


@router.get("/proposed")
async def proposed(_admin: str = Depends(require_admin)):
    """ADMIN — make-ups waiting for a tutor and time, deadline first, each with
    the planner's proposal (or the reason there is none) and its learners."""
    return {"sessions": makeup_service.proposed_sessions()}


class AttendanceRequest(BaseModel):
    # student_id → attended | absent | excused
    marks: dict[str, Literal["attended", "absent", "excused"]]


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    """A tutor's make-ups (with learners and seats), or a learner's next live
    session, those still being arranged and their credit balance (§2f items 2, 4)."""
    return makeup_delivery.mine(user_id)


@router.post("/{session_id}/attendance")
async def attendance(session_id: str, req: AttendanceRequest,
                     user_id: str = Depends(get_current_user)):
    """The session's tutor (or an ops admin) marks seats, once it has started."""
    try:
        return {"sessions": makeup_delivery.mark_attendance(user_id, session_id, dict(req.marks))}
    except MakeupRefused as exc:
        status = {"no_such_session": 404, "not_allowed": 403}.get(exc.reason, 409)
        raise HTTPException(status_code=status, detail={"reason": exc.reason})


@router.post("/{session_id}/schedule")
async def schedule(session_id: str, req: ScheduleRequest, background: BackgroundTasks,
                   _admin: str = Depends(require_admin)):
    """ADMIN — book a proposed make-up. The video room and the booked email
    follow in the background (§2f items 3, 5)."""
    try:
        booked = makeup_service.schedule(session_id, req.tutor_id, req.starts_at)
        background.add_task(makeup_delivery.after_booking, session_id)
        return booked
    except ScheduleRefused as exc:
        status = 404 if exc.reason == "no_such_session" else 400
        raise HTTPException(status_code=status,
                            detail={"reason": exc.reason, "message": exc.message})


@router.post("/dry-run")
async def dry_run(_admin: str = Depends(require_admin)):
    """ADMIN — what the daily run would propose now, writing nothing."""
    return makeup_service.run(dry_run=True)
