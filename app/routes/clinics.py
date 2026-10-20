"""Phase 0 clinics (enrolment plan 1.5, migration §105).

Learner: `GET /clinics/mine` (their band's upcoming clinics, their bookings and
allowance), `POST /clinics/{id}/book`, `POST /clinics/{id}/cancel-booking`.
Tutor: `GET /clinics/mine` (their clinics with learners), `POST
/clinics/{id}/attendance`. Ops: `GET /clinics`, `POST /clinics`, `POST
/clinics/{id}/cancel`. The caller always comes from the JWT; who may do what is
decided in `clinic_service` and the database.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import clinic_service
from app.services.clinic_service import ClinicRefused
from app.services.supabase_client import supabase_admin as db

router = APIRouter()

_STATUS = {"no_such_clinic": 404, "not_allowed": 403, "payment_lapsed": 402, "full": 409, "closed": 409, "started": 409,
           "already_booked": 409, "allowance_used": 409, "not_booked": 409, "not_started": 409,
           # Drop-ins (plan 3.7, §115).
           "week_used": 409, "not_paying": 403, "in_a_pod": 403, "paused": 403}


def _refused(exc: ClinicRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class ClinicCreate(BaseModel):
    band: Literal["americas", "europe", "ea_gulf", "asia"]
    tutor_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    starts_at: str                              # ISO 8601 with offset
    repeat_weeks: int = Field(1, ge=1, le=12)
    # Default by kind: 15 for a Phase 0 clinic, 10 for a drop-in (§115).
    capacity: Optional[int] = Field(None, ge=1, le=15)
    kind: Literal["phase0", "drop_in"] = "phase0"


class ClinicAttendance(BaseModel):
    marks: dict[str, Literal["attended", "absent"]]


@router.get("/mine")
async def mine(band: Optional[str] = Query(None, pattern=r"^(americas|europe|ea_gulf|asia)$"),
               user_id: str = Depends(get_current_user)):
    is_tutor = bool((db.schema("app").table("tutor").select("id").eq("id", user_id)
                     .limit(1).execute()).data)
    if is_tutor:
        return {"as_tutor": clinic_service.for_tutor(user_id), "as_learner": None}
    return {"as_tutor": None, "as_learner": clinic_service.for_learner(user_id, band)}


@router.get("/drop-ins")
async def drop_ins(band: Optional[str] = Query(None, pattern=r"^(americas|europe|ea_gulf|asia)$"),
                   user_id: str = Depends(get_current_user)):
    """Wait-pool drop-ins (plan 3.7): the band's upcoming ones, the learner's
    own, eligibility, and which weeks are already used. Book and cancel through
    the same /{id}/book and /{id}/cancel-booking as clinics."""
    return clinic_service.drop_ins_for_learner(user_id, band)


@router.post("/{clinic_id}/book")
async def book(clinic_id: str, user_id: str = Depends(get_current_user)):
    try:
        return clinic_service.book(user_id, clinic_id)
    except ClinicRefused as exc:
        _refused(exc)


@router.post("/{clinic_id}/cancel-booking")
async def cancel_booking(clinic_id: str, user_id: str = Depends(get_current_user)):
    try:
        return clinic_service.cancel_booking(user_id, clinic_id)
    except ClinicRefused as exc:
        _refused(exc)


@router.post("/{clinic_id}/attendance")
async def attendance(clinic_id: str, req: ClinicAttendance, user_id: str = Depends(get_current_user)):
    try:
        return {"clinics": clinic_service.mark(user_id, clinic_id, dict(req.marks))}
    except ClinicRefused as exc:
        _refused(exc)


@router.get("")
async def admin_list(admin_id: str = Depends(require_admin)):
    try:
        return {"clinics": clinic_service.admin_list(admin_id)}
    except ClinicRefused as exc:
        _refused(exc)


@router.post("")
async def create(req: ClinicCreate, admin_id: str = Depends(require_admin)):
    try:
        return {"clinics": await clinic_service.create(admin_id, req.band, req.tutor_id, req.starts_at,
                                                       req.repeat_weeks, req.capacity, req.kind)}
    except ClinicRefused as exc:
        _refused(exc)


@router.post("/{clinic_id}/cancel")
async def cancel(clinic_id: str, admin_id: str = Depends(require_admin)):
    try:
        return clinic_service.cancel(admin_id, clinic_id)
    except ClinicRefused as exc:
        _refused(exc)
