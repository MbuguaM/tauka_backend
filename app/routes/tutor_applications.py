"""`POST /tutor-applications` — the /teach form (enrolment plan §2j J2, §120).

J0 emailed the application to the team and stored nothing. Since §120 it is
STORED in `app.tutor_application`, the applicant gets a receipt with a withdraw
link, and the team still gets a copy by email.

  * The page says "received" only when the application was STORED; a failure
    to store is a 503 so the page can show the address to write to instead.
  * Otherwise the answer is always `{ok: true}`, whatever happened to the
    emails and whether the email had applied before, so the form can't be
    used to learn who has applied.
  * Rate-limited per IP and per email (a receipt goes to the address given, so
    the per-email limit is what stops the form being used to mail someone
    repeatedly). A filled honeypot field is accepted and dropped.

`POST /tutor-applications/withdraw {token}` — the receipt's link, via the web
page `/teach/withdraw`, which acts only on a button press (mail scanners).
"""

import hashlib
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies import require_admin
from app.models.schemas import TutorApplicationRequest, WaitlistTokenRequest
from app.routes.auth import _ip_hash
from app.services import tutor_application_service as apps
from app.services.rate_limit_service import check_anonymous_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("")
async def submit_tutor_application(req: TutorApplicationRequest, request: Request):
    """PUBLIC — store a tutor application and send the receipt."""
    allowed, reason = check_anonymous_rate_limit(_ip_hash(request), "tutor_application", max_requests=3)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)
    email_key = hashlib.sha256(str(req.email).strip().lower().encode()).hexdigest()
    allowed, reason = check_anonymous_rate_limit(email_key, "tutor_application_email", max_requests=3)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)
    if req.website:
        # The honeypot: a person never sees this field.
        return {"ok": True}

    fields = req.model_dump()
    try:
        _, token = apps.submit(fields)
    except Exception as exc:  # noqa: BLE001
        logger.error("tutor application not stored: %s", exc)
        raise HTTPException(status_code=503, detail="Your application didn't send.")
    from app.core.followup import run_followup
    await run_followup(apps.notify(fields, token), label="tutor application receipt and team copy")
    return {"ok": True}


@router.post("/withdraw")
async def withdraw_tutor_application(req: WaitlistTokenRequest, request: Request):
    """PUBLIC — withdraw with the receipt's token. Same answer either way."""
    allowed, reason = check_anonymous_rate_limit(_ip_hash(request), "tutor_application_withdraw", max_requests=10)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)
    return {"ok": apps.withdraw(req.token)}


# ── J3: admin review (ops) ───────────────────────────────────────────────────

_STATUS = {"not_allowed": 403, "no_such_application": 404, "not_submitted": 409, "decided": 409,
           "live_subscription": 409, "admin_account": 409, "bad_reason": 400}


def _refused(exc: apps.ApplicationRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Reject(BaseModel):
    reason_code: Literal["language_not_needed", "fluency", "demo", "capacity", "other"]
    note: Optional[str] = Field(None, max_length=2000)


class Approve(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


@router.get("/admin")
async def admin_list(status: Optional[str] = None, admin_id: str = Depends(require_admin)):
    try:
        return {"applications": apps.admin_list(admin_id, status)}
    except apps.ApplicationRefused as exc:
        _refused(exc)


@router.get("/admin/{app_id}")
async def admin_detail(app_id: str, admin_id: str = Depends(require_admin)):
    try:
        return apps.admin_detail(admin_id, app_id)
    except apps.ApplicationRefused as exc:
        _refused(exc)


@router.post("/admin/{app_id}/review")
async def mark_review(app_id: str, admin_id: str = Depends(require_admin)):
    try:
        return apps.mark_under_review(admin_id, app_id)
    except apps.ApplicationRefused as exc:
        _refused(exc)


@router.post("/admin/{app_id}/reject")
async def reject(app_id: str, req: Reject, admin_id: str = Depends(require_admin)):
    try:
        return await apps.reject(admin_id, app_id, req.reason_code, req.note)
    except apps.ApplicationRefused as exc:
        _refused(exc)


@router.post("/admin/{app_id}/approve")
async def approve(app_id: str, req: Approve, admin_id: str = Depends(require_admin)):
    """Makes an ONBOARDING tutor (CONFLICT-057): invites or promotes, writes the
    tutor role and the app.tutor row. Idempotent."""
    try:
        return await apps.approve(admin_id, app_id, req.note)
    except apps.ApplicationRefused as exc:
        _refused(exc)
