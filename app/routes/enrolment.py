"""The learner's own enrolment state (enrolment plan 1.3; §98).

`GET /enrolment/me` reads, after reconciling the row with what other tables
already say (paid, Phase 0 finished, target cutoff missed). `POST
/enrolment/start` begins Phase 0 or updates the pace pledge and band. The caller
comes from the JWT, never the body. Logic: `phase0_service`.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from typing import Optional

from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.models.schemas import Phase0StartRequest, SlotChoicesRequest
from app.services import phase0_service, slot_service
from app.services.slot_service import SlotRefused
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/me")
async def my_enrolment(
    course_id: str = Query(..., pattern=r"^[0-9a-fA-F-]{36}$"),
    user_id: str = Depends(get_current_user),
):
    try:
        return phase0_service.status(user_id, course_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("enrolment status failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Couldn't load your enrolment.")


@router.post("/start")
async def start_phase0(req: Phase0StartRequest, user_id: str = Depends(get_current_user)):
    try:
        return phase0_service.start(
            user_id, req.course_id,
            pace_pledge_hours=req.pace_pledge_hours, band=req.band,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("phase 0 start failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Couldn't start Phase 0. Try again.")


@router.get("/slots")
async def my_slots(
    band: str | None = Query(None, pattern=r"^(americas|europe|ea_gulf|asia)$"),
    user_id: str = Depends(get_current_user),
):
    """The slot catalogue for a band (default: the band of the learner's profile
    timezone) with the learner's choices; `next_at` is each slot's next start in
    UTC so the app shows it in local time (plan 3.1, §104)."""
    tz = None
    if band is None:
        # app.user_profiles (tauka-flutter): timezone, IANA.
        rows = (db.schema("app").table("user_profiles").select("timezone")
                .eq("id", user_id).limit(1).execute()).data or []
        tz = (rows[0] if rows else {}).get("timezone")
    try:
        return slot_service.view(user_id, band, tz)
    except Exception as exc:  # noqa: BLE001
        logger.error("slot catalogue failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Couldn't load the session times.")


@router.post("/slots")
async def save_slots(req: SlotChoicesRequest, user_id: str = Depends(get_current_user)):
    """Replace the learner's slot choices. Three or more earns the guarantee."""
    try:
        return slot_service.save(user_id, req.band, [p.model_dump() for p in req.slots])
    except SlotRefused as exc:
        raise HTTPException(status_code=400, detail={"reason": exc.reason})


# ── Pausing (plan 4.4, §111) ─────────────────────────────────────────────────

_PAUSE_STATUS = {"already_paused": 409, "nothing_to_withdraw": 409}


def _pause_refused(exc):
    raise HTTPException(status_code=_PAUSE_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class ReadAheadRequest(BaseModel):
    course_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


@router.get("/read-ahead")
async def my_read_ahead(course_id: str, user_id: str = Depends(get_current_user)):
    """Plan 2.2 / §2c: one lesson from next month, and the upgrade offer once it's done."""
    from app.services import lesson_service
    return lesson_service.read_ahead(user_id, course_id)


@router.post("/read-ahead")
async def read_ahead(req: ReadAheadRequest, user_id: str = Depends(get_current_user)):
    from app.services import lesson_service
    try:
        return lesson_service.request_read_ahead(user_id, req.course_id)
    except lesson_service.ReadAheadRefused as exc:
        raise HTTPException(status_code=409, detail={"reason": exc.reason})


class GenderPrefs(BaseModel):
    gender: Optional[str] = Field(None, max_length=20)
    same_gender_tutor: str = Field("none", max_length=20)
    same_gender_pod: str = Field("none", max_length=20)


@router.get("/gender")
async def my_gender(user_id: str = Depends(get_current_user)):
    """Plan B8 (§132): `{enabled: false}` until the gender_matching flag is on."""
    from app.services import gender_service
    return gender_service.mine(user_id)


@router.post("/gender")
async def set_gender(req: GenderPrefs, user_id: str = Depends(get_current_user)):
    from app.services import gender_service
    try:
        return gender_service.set_mine(user_id, req.gender, req.same_gender_tutor, req.same_gender_pod)
    except gender_service.GenderRefused as exc:
        raise HTTPException(status_code=403 if exc.reason == "not_enabled" else 400, detail={"reason": exc.reason})


@router.get("/reminders/off", response_class=HTMLResponse)
async def reminders_off(s: str = Query(..., max_length=64), t: str = Query(..., max_length=64)):
    """The one-click link in a Phase 0 reminder (plan 1.3, §127). No sign-in."""
    from app.services import phase0_reminder_service
    ok = phase0_reminder_service.turn_off(s, t)
    body = ("Done. You won't get Phase 0 reminder emails any more." if ok
            else "That link didn't work. Write to support@tauka.io and we'll stop them.")
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Tauka</title>"
                        f"<p style='font-family:sans-serif;max-width:480px;margin:48px auto'>{body}</p>",
                        status_code=200 if ok else 400)


@router.get("/grace")
async def my_grace(user_id: str = Depends(get_current_user)):
    """A missed payment (plan 2.3a, §126): none | grace (until) | lapsed."""
    from app.services import grace_service
    return grace_service.view(user_id)


@router.get("/pause")
async def my_pause(user_id: str = Depends(get_current_user)):
    """The open pause, or whether one can be asked for (and why not)."""
    from app.services import pause_service
    return pause_service.view(user_id)


@router.post("/pause")
async def pause(user_id: str = Depends(get_current_user)):
    """From the end of the paid month, for up to 3 months, at Free access."""
    from app.services import pause_service
    try:
        return {"pause": await pause_service.request(user_id)}
    except pause_service.PauseRefused as exc:
        _pause_refused(exc)


@router.post("/pause/withdraw")
async def withdraw_pause(user_id: str = Depends(get_current_user)):
    """Before it starts only; billing carries on as before."""
    from app.services import pause_service
    try:
        return {"pause": await pause_service.withdraw(user_id)}
    except pause_service.PauseRefused as exc:
        _pause_refused(exc)
