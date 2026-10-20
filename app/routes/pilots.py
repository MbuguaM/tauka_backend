"""Track 10 pilots and the plan's last remainders (§140).

Anyone signed in: `GET /pilots/holiday`, `GET /pilots/gatherings`, `POST /pilots/gatherings/{id}/rsvp`,
`GET /pilots/clinic-mates`, `POST /pilots/mentor`.
Reviewers rung >= 2: `GET /pilots/fidel/second`, `POST /pilots/fidel/second/{id}`.
Ops: `POST /pilots/holiday-lessons`, `POST /pilots/gatherings`, `POST /pilots/gatherings/{id}/done`,
`POST /pilots/intakes/mid-month`, `POST /pilots/clinics/{id}/co-tutor`, `GET /pilots/fidel/calibration`.
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import pilots_service as svc
from app.services.pilots_service import PilotRefused

router = APIRouter()
_STATUS = {"not_allowed": 403, "full": 409, "not_open": 409, "too_soon": 409, "no_such_gathering": 404}
Uuid = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


def _do(fn):
    try:
        return fn()
    except PilotRefused as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class HolidayLesson(BaseModel):
    holiday: Literal["enkutatash", "meskel", "genna", "timket", "fasika"]
    title: str = Field(..., min_length=3, max_length=120)
    body: str = Field(..., min_length=10, max_length=4000)


class Gathering(BaseModel):
    kind: Literal["meetup", "gathering"]
    title: str = Field(..., min_length=3, max_length=120)
    city: str = Field(..., min_length=2, max_length=80)
    starts_at: datetime
    capacity: int = Field(..., ge=2, le=500)
    host_tutor_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    season_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")


class Rsvp(BaseModel):
    going: bool


class MidMonth(BaseModel):
    band: Literal["americas", "europe", "ea_gulf", "asia"]
    ec_year: int = Field(..., ge=2018, le=2100)
    ec_month: int = Field(..., ge=1, le=12)


class CoTutor(BaseModel):
    tutor_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")


class Flag(BaseModel):
    on: bool


class Second(BaseModel):
    approved: bool


@router.get("/holiday")
async def holiday(user_id: str = Depends(get_current_user)):
    return {"lesson": svc.holiday_lesson()}


@router.post("/holiday-lessons")
async def add_holiday(req: HolidayLesson, admin_id: str = Depends(require_admin)):
    return _do(lambda: svc.add_holiday_lesson(admin_id, req.holiday, req.title, req.body))


@router.get("/gatherings")
async def gatherings(user_id: str = Depends(get_current_user)):
    return {"gatherings": svc.gatherings(user_id)}


@router.post("/gatherings")
async def create_gathering(req: Gathering, admin_id: str = Depends(require_admin)):
    return _do(lambda: svc.create_gathering(admin_id, req.kind, req.title, req.city, req.starts_at, req.capacity,
                                            req.host_tutor_id, req.season_id))


@router.post("/gatherings/{gathering_id}/rsvp")
async def rsvp(gathering_id: str, req: Rsvp, user_id: str = Depends(get_current_user)):
    return _do(lambda: svc.rsvp(user_id, gathering_id, req.going))


@router.post("/gatherings/{gathering_id}/done")
async def done(gathering_id: str, admin_id: str = Depends(require_admin)):
    return _do(lambda: svc.finish_gathering(admin_id, gathering_id))


@router.post("/intakes/mid-month")
async def mid_month(req: MidMonth, admin_id: str = Depends(require_admin)):
    return _do(lambda: svc.open_mid_month(admin_id, req.band, req.ec_year, req.ec_month))


@router.post("/clinics/{clinic_id}/co-tutor")
async def co_tutor(clinic_id: str, req: CoTutor, admin_id: str = Depends(require_admin)):
    return _do(lambda: svc.set_co_tutor(admin_id, clinic_id, req.tutor_id))


@router.get("/clinic-mates")
async def clinic_mates(user_id: str = Depends(get_current_user)):
    return {"mates": svc.clinic_mates(user_id)}


@router.post("/mentor")
async def mentor(req: Flag, user_id: str = Depends(get_current_user)):
    return svc.set_mentor(user_id, req.on)


@router.get("/fidel/second")
async def second_queue(user_id: str = Depends(get_current_user)):
    return {"readings": _do(lambda: svc.second_queue(user_id))}


@router.post("/fidel/second/{approval_id}")
async def second(approval_id: str, req: Second, user_id: str = Depends(get_current_user)):
    return _do(lambda: svc.second_review(user_id, approval_id, req.approved))


@router.get("/fidel/calibration")
async def calibration(admin_id: str = Depends(require_admin)):
    return {"reviewers": _do(lambda: svc.calibration(admin_id))}
