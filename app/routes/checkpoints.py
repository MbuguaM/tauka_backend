"""Monthly pod checkpoints (enrolment plan 4.2, §2c, migration §109). Ops only.

`GET /checkpoints` (open; `?all=true` includes reviewed),
`POST /checkpoints/{id}/review`.
"""

from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import require_admin
from app.services import checkpoint_service
from app.services.checkpoint_service import CheckpointRefused

router = APIRouter()

_STATUS = {"no_such_checkpoint": 404, "not_allowed": 403, "reviewed": 409, "not_a_member": 400,
           "move_needs_adjust": 400, "bad_outcome": 400}


def _refused(exc: CheckpointRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Review(BaseModel):
    outcome: Literal["continue", "adjust", "pause", "dissolve"]
    rota_reviewed: bool = False
    move_student_ids: List[str] = Field(default_factory=list, max_length=8)


@router.get("")
async def queue(all: bool = False, admin_id: str = Depends(require_admin)):
    try:
        return {"checkpoints": checkpoint_service.queue(admin_id, include_reviewed=all)}
    except CheckpointRefused as exc:
        _refused(exc)


@router.post("/{checkpoint_id}/review")
async def review(checkpoint_id: str, req: Review, admin_id: str = Depends(require_admin)):
    try:
        return checkpoint_service.review(admin_id, checkpoint_id, req.outcome, req.rota_reviewed,
                                         req.move_student_ids)
    except CheckpointRefused as exc:
        _refused(exc)
