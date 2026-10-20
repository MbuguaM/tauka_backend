"""Friend groups and private pods (enrolment plan 3.2, migration §108).

Learner: `GET /groups/mine`, `POST /groups` (create), `POST /groups/join`,
`POST /groups/leave`. Ops: `GET /groups`. The caller comes from the JWT.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, require_admin
from app.services import group_service
from app.services.group_service import GroupRefused

router = APIRouter()
_STATUS = {"not_allowed": 403, "no_such_group": 404, "already_in_group": 409, "full": 409, "closed": 409,
           "not_in_group": 409, "not_formable": 409, "too_few": 409, "not_ready": 409, "level_gap": 409,
           "mixed_plans": 409, "tutor_inactive": 409, "no_such_slot": 404}


def _refused(exc: GroupRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class GroupCreate(BaseModel):
    kind: Literal["friends", "private_pod"]
    course_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


class FormPod(BaseModel):
    tutor_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    slot_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")


class GroupJoin(BaseModel):
    code: str = Field(..., min_length=8, max_length=8)


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return group_service.mine(user_id)


@router.post("")
async def create(req: GroupCreate, user_id: str = Depends(get_current_user)):
    try:
        return group_service.create(user_id, req.kind, req.course_id)
    except GroupRefused as exc:
        _refused(exc)


@router.post("/join")
async def join(req: GroupJoin, user_id: str = Depends(get_current_user)):
    try:
        return group_service.join(user_id, req.code)
    except GroupRefused as exc:
        _refused(exc)


@router.post("/leave")
async def leave(user_id: str = Depends(get_current_user)):
    try:
        return group_service.leave(user_id)
    except GroupRefused as exc:
        _refused(exc)


@router.get("")
async def admin_list(admin_id: str = Depends(require_admin)):
    try:
        return {"groups": group_service.admin_list(admin_id)}
    except GroupRefused as exc:
        _refused(exc)


@router.post("/{group_id}/form")
async def form(group_id: str, req: FormPod, admin_id: str = Depends(require_admin)):
    """Ops forms a private pod (plan 3.2, §128): a tutor and a catalogue slot;
    each member gets an offer, and it goes live when all have accepted."""
    try:
        return await group_service.form_private_pod(admin_id, group_id, req.tutor_id, req.slot_id)
    except GroupRefused as exc:
        _refused(exc)
