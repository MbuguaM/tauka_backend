"""Cover requests (enrolment plan §2h, §123; CONFLICT-058).

Tutor: `POST /cover/request`, `POST /cover/{id}/withdraw`, `GET /cover/offers`,
`POST /cover/offers/{id}/accept`, `POST /cover/offers/{id}/decline`.
Ops: `GET /cover/open`, `POST /cover/{id}/assign`.
No-shows and standby (plan 8.6, §125): `POST /cover/no-show` (a learner in the
session, or ops), `GET|POST /cover/standby`, `POST /cover/standby/{id}/remove`.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import cover_service
from app.services.cover_service import CoverRefused

router = APIRouter()

_STATUS = {"not_allowed": 403, "not_your_session": 403, "no_such_session": 404, "no_such_request": 404,
           "already_requested": 409, "not_open": 409, "started": 409, "no_offer": 409, "not_upcoming": 409,
           "not_in_pool": 400, "not_now": 409, "no_lead": 409, "bad_window": 400, "too_many": 409,
           "no_such_window": 404, "not_a_tutor": 403}


def _refused(exc: CoverRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class CoverRequest(BaseModel):
    session_type: Literal["cohort_session", "makeup_session"]
    session_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    reason_code: Literal["ill", "emergency", "clash", "other"]


class NoShow(BaseModel):
    session_type: Literal["cohort_session", "makeup_session"]
    session_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


class StandbyWindow(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")


class Assign(BaseModel):
    tutor_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


@router.post("/request")
async def request(req: CoverRequest, user_id: str = Depends(get_current_user)):
    try:
        return await cover_service.request(user_id, req.session_type, req.session_id, req.reason_code)
    except CoverRefused as exc:
        _refused(exc)


@router.post("/{request_id}/withdraw")
async def withdraw(request_id: str, user_id: str = Depends(get_current_user)):
    try:
        return cover_service.withdraw(user_id, request_id)
    except CoverRefused as exc:
        _refused(exc)


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    """The tutor's next 14 days of sessions they lead, with any open cover request."""
    return {"sessions": cover_service.mine(user_id)}


@router.get("/offers")
async def offers(user_id: str = Depends(get_current_user)):
    return {"offers": cover_service.offers_for(user_id)}


@router.post("/offers/{request_id}/accept")
async def accept(request_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await cover_service.accept(user_id, request_id)
    except CoverRefused as exc:
        _refused(exc)


@router.post("/offers/{request_id}/decline")
async def decline(request_id: str, user_id: str = Depends(get_current_user)):
    cover_service.decline(user_id, request_id)
    return {"ok": True}


@router.get("/open")
async def open_requests(admin_id: str = Depends(require_admin)):
    try:
        return {"requests": cover_service.open_requests(admin_id)}
    except CoverRefused as exc:
        _refused(exc)


@router.post("/{request_id}/assign")
async def assign(request_id: str, req: Assign, admin_id: str = Depends(require_admin)):
    try:
        return await cover_service.assign(admin_id, request_id, req.tutor_id)
    except CoverRefused as exc:
        _refused(exc)


@router.post("/no-show")
async def no_show(req: NoShow, user_id: str = Depends(get_current_user)):
    """My tutor isn't here: from the start until 15 minutes after."""
    try:
        return await cover_service.report_no_show(user_id, req.session_type, req.session_id)
    except CoverRefused as exc:
        _refused(exc)


@router.get("/standby")
async def standby_windows(user_id: str = Depends(get_current_user)):
    from app.services import standby_service
    return {"windows": standby_service.windows(user_id)}


@router.post("/standby")
async def add_standby(req: StandbyWindow, user_id: str = Depends(get_current_user)):
    from app.services import standby_service
    try:
        return standby_service.add_window(user_id, req.day_of_week, req.start_time, req.end_time)
    except standby_service.StandbyRefused as exc:
        _refused(exc)


@router.post("/standby/{window_id}/remove")
async def remove_standby(window_id: str, user_id: str = Depends(get_current_user)):
    from app.services import standby_service
    try:
        standby_service.remove_window(user_id, window_id)
        return {"ok": True}
    except standby_service.StandbyRefused as exc:
        _refused(exc)
