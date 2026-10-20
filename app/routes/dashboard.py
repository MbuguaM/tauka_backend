"""Ops dashboards (enrolment plan Track 9). Ops only; display only (E-16).

`GET /dashboard/formation?intake_id=`, `GET /dashboard/benchmarks`, `GET /dashboard/funnel`.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import require_admin
from app.services import dashboard_service as svc

router = APIRouter()


def _guard(fn):
    try:
        return fn()
    except svc.DashboardRefused as exc:
        raise HTTPException(status_code=403, detail={"reason": exc.reason})


@router.get("/formation")
async def formation(intake_id: Optional[str] = None, admin_id: str = Depends(require_admin)):
    return _guard(lambda: svc.formation(admin_id, intake_id))


@router.get("/benchmarks")
async def benchmarks(admin_id: str = Depends(require_admin)):
    return _guard(lambda: svc.benchmarks(admin_id))


@router.get("/funnel")
async def funnel(admin_id: str = Depends(require_admin)):
    return _guard(lambda: svc.funnel(admin_id))
