"""The slot catalogue editor — ops admin routes (enrolment plan 3.1, §104).

Learners read and choose slots through `/enrolment/slots`. These routes are the
catalogue's own maintenance: list (with how many learners chose each), add a
slot, and retire/restore, reorder or relabel one. A slot's time is never edited
(that would move learners' choices under them): add the new one, retire the old.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import require_admin
from app.services import slot_service
from app.services.slot_service import SlotRefused

router = APIRouter()

_STATUS = {"not_allowed": 403, "no_such_slot": 404, "exists": 409}


def _refused(exc: SlotRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class SlotCreate(BaseModel):
    band: Literal["americas", "europe", "ea_gulf", "asia"]
    day_of_week: int = Field(..., ge=0, le=6)          # 0 = Monday, in the band's zone
    start_time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    kind: Literal["weekend", "weekday"]
    sort_order: int = 0


class SlotUpdate(BaseModel):
    active: Optional[bool] = None
    sort_order: Optional[int] = None
    kind: Optional[Literal["weekend", "weekday"]] = None


@router.get("")
async def list_slots(admin_id: str = Depends(require_admin)):
    try:
        return {"slots": slot_service.admin_list(admin_id), "anchors": slot_service.ANCHOR}
    except SlotRefused as exc:
        _refused(exc)


@router.post("")
async def create_slot(req: SlotCreate, admin_id: str = Depends(require_admin)):
    try:
        return slot_service.admin_create(admin_id, req.band, req.day_of_week, req.start_time, req.kind,
                                         req.sort_order)
    except SlotRefused as exc:
        _refused(exc)


@router.post("/{slot_id}")
async def update_slot(slot_id: str, req: SlotUpdate, admin_id: str = Depends(require_admin)):
    try:
        return slot_service.admin_update(admin_id, slot_id, req.active, req.sort_order, req.kind)
    except SlotRefused as exc:
        _refused(exc)
