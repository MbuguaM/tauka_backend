"""Tutor rungs — admin routes (enrolment plan §2k, migration §99).

`require_admin` only proves the caller is some admin. Which admin may set which
rung (ops: 1–3, owner: 4 and anything off 4, nobody their own) is enforced by
`app.set_tutor_rung` in the database and surfaces here as a 403 with a reason.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import hierarchy_service as hs
from app.services.hierarchy_service import HierarchyRefused

router = APIRouter()


class RungRequest(BaseModel):
    rung: int = Field(..., ge=1, le=4)
    reason: str = Field(..., min_length=1, max_length=500)


@router.get("/tutors")
async def list_tutors(_admin: str = Depends(require_admin)):
    return {"tutors": hs.tutors(), "names": hs.RUNGS}


@router.get("/tutors/{tutor_id}/rung")
async def get_rung(tutor_id: str, _admin: str = Depends(require_admin)):
    return {"tutor_id": tutor_id, "rung": hs.rung_of(tutor_id), "names": hs.RUNGS,
            "history": hs.rung_history(tutor_id)}


@router.post("/tutors/{tutor_id}/rung")
async def set_rung(tutor_id: str, req: RungRequest, admin_id: str = Depends(require_admin)):
    try:
        return hs.set_rung(admin_id, tutor_id, req.rung, req.reason)
    except HierarchyRefused as exc:
        status = {"no_such_tutor": 404, "invalid_rung": 400, "reason_required": 400}.get(exc.reason, 403)
        raise HTTPException(status_code=status, detail={"reason": exc.reason})


# ── Tutor status (plan §2j J4, §122; CONFLICT-057) ───────────────────────────

class StatusRequest(BaseModel):
    status: Literal["active", "suspended", "offboarded"]
    reason: Literal["onboarding_complete", "returning", "leave", "conduct", "reliability", "left", "other"]


class CheckRequest(BaseModel):
    step: Literal["id_check", "references", "contract", "training", "calibration"]
    done: bool = True
    score: Optional[int] = Field(None, ge=0, le=100)
    ref_code: Optional[str] = Field(None, max_length=40)


class DetailsRequest(BaseModel):
    language: Optional[str] = Field(None, max_length=60)
    country_code: Optional[str] = Field(None, max_length=2)


def _status_refused(exc):
    code = 404 if exc.reason == "no_such_tutor" else 403 if exc.reason == "not_allowed" else 409         if exc.reason.startswith("not_allowed_from_") or exc.reason == "checks_incomplete" else 400
    raise HTTPException(status_code=code, detail={"reason": exc.reason})


@router.get("/me")
async def my_status(user_id: str = Depends(get_current_user)):
    """A tutor's own status and, while onboarding, what's left."""
    from app.services import tutor_status_service
    return tutor_status_service.me(user_id)


@router.post("/tutors/{tutor_id}/status")
async def set_status(tutor_id: str, req: StatusRequest, admin_id: str = Depends(require_admin)):
    from app.services import tutor_status_service as ts
    try:
        return ts.set_status(admin_id, tutor_id, req.status, req.reason)
    except ts.TutorStatusRefused as exc:
        _status_refused(exc)


@router.post("/tutors/{tutor_id}/details")
async def set_details(tutor_id: str, req: DetailsRequest, admin_id: str = Depends(require_admin)):
    from app.services import tutor_status_service as ts
    try:
        return ts.set_details(admin_id, tutor_id, req.language, req.country_code)
    except ts.TutorStatusRefused as exc:
        _status_refused(exc)


@router.get("/tutors/{tutor_id}/status-history")
async def status_history(tutor_id: str, admin_id: str = Depends(require_admin)):
    from app.services import tutor_status_service as ts
    try:
        return {"events": ts.history(admin_id, tutor_id)}
    except ts.TutorStatusRefused as exc:
        _status_refused(exc)


@router.get("/tutors/{tutor_id}/onboarding")
async def onboarding_checklist(tutor_id: str, admin_id: str = Depends(require_admin)):
    """The J5 checklist (§124): each step's latest state."""
    from app.services import tutor_status_service as ts
    try:
        ts._require_ops(admin_id)
        return {"tutor_id": tutor_id, "checklist": list(ts.checklist(tutor_id).values())}
    except ts.TutorStatusRefused as exc:
        _status_refused(exc)


@router.post("/tutors/{tutor_id}/onboarding")
async def mark_check(tutor_id: str, req: CheckRequest, admin_id: str = Depends(require_admin)):
    """Ops ticks or unticks one step. Calibration takes a score; under 85 is recorded as not passed."""
    from app.services import tutor_status_service as ts
    try:
        return ts.mark_check(admin_id, tutor_id, req.step, req.done, req.score, req.ref_code)
    except ts.TutorStatusRefused as exc:
        _status_refused(exc)
