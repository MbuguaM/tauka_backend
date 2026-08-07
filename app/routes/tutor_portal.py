import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import require_tutor
from app.models.schemas import UpdatePayoutRequest
from app.services import tutor_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/earnings")
async def get_earnings(
    user_id: str = Depends(require_tutor),
    period: str = Query(default="current_month"),
):
    """Tutor earnings for current_month / last_month / all."""
    if period not in ("current_month", "last_month", "all"):
        raise HTTPException(status_code=400, detail="period must be current_month, last_month, or all")
    return await tutor_service.get_earnings(user_id, period)


@router.get("/earnings/history")
async def earnings_history(
    user_id: str = Depends(require_tutor),
    limit: int = Query(default=12, ge=1, le=36),
    offset: int = Query(default=0, ge=0),
):
    """Monthly earnings history, paginated."""
    return await tutor_service.get_earnings_history(user_id, limit, offset)


@router.get("/payout-settings")
async def get_payout_settings(user_id: str = Depends(require_tutor)):
    """Masked payout settings (last4 only — never exposes raw details)."""
    result = await tutor_service.get_payout_settings(user_id)
    return result or {"method": None, "details": None}


@router.post("/payout-settings")
async def update_payout_settings(
    req: UpdatePayoutRequest,
    user_id: str = Depends(require_tutor),
):
    """Update encrypted payout method (bank or M-Pesa)."""
    raw = req.model_dump(exclude_none=True)
    raw.pop("method", None)
    try:
        return await tutor_service.update_payout_settings(user_id, req.method, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/students")
async def get_assigned_students(user_id: str = Depends(require_tutor)):
    """Students assigned to this tutor with aggregated stats."""
    return await tutor_service.get_assigned_students(user_id)


@router.get("/students/{student_id}")
async def get_student_detail(
    student_id: str,
    user_id: str = Depends(require_tutor),
):
    """Detailed profile for one assigned student."""
    try:
        return await tutor_service.get_student_detail(user_id, student_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
