import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import require_admin, require_tutor
from app.models.schemas import (
    TriageFeedbackRequest,
    TutorReferralReportResponse,
    UpdatePayoutRequest,
)
from app.services import (
    feedback_service,
    progress_service,
    standing_service,
    tutor_referral_service,
    tutor_service,
)

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


# ── Referrals (plan Phase 1, spec §1.1) ───────────────────────────────────────

@router.get("/referrals", response_model=TutorReferralReportResponse)
async def get_referral_report(user_id: str = Depends(require_tutor)):
    """Referral performance for the calling tutor — signups, retention, vested
    share. Spec §1.1's `GET /tutor/{id}/referrals`, with the id taken from the
    verified token instead of the path (see `GET /referrals/tutor/link`).

    **No student is named in the response.** Referring someone does not make you
    their tutor, and retention is a count rather than a roster. The link/rotate
    routes live under `/referrals/tutor/*`; this one lives here because it is
    the tutor portal's read model, alongside earnings.
    """
    return await tutor_referral_service.get_referral_report(user_id)


# ── Standing (plan Phase 6, spec §1.6) ────────────────────────────────────────

@router.get("/standing")
async def get_standing(user_id: str = Depends(require_tutor)):
    """The calling tutor's own standing, what it gates, and the distance to the
    next rung.

    The id comes from the verified token, never the path — same rule as
    `/referrals`. A tutor reading another tutor's standing would be reading a
    colleague's performance review.

    Returns `pending` when a live recomputation disagrees with the stored row,
    i.e. the tutor has crossed a threshold since the monthly run. Showing a
    stale standing for up to a month with no explanation is the failure this
    avoids.
    """
    return standing_service.standing_report(user_id)


@router.get("/standing/history")
async def get_standing_history(
    user_id: str = Depends(require_tutor),
    limit: int = Query(default=24, ge=1, le=120),
):
    """Standing over time, newest first — the audit trail §4.6 exists for."""
    return {"history": standing_service.history(user_id, limit)}


# ── Student progress (plan Phase 6, spec §1.8) ────────────────────────────────

@router.get("/students/{student_id}/progress")
async def get_student_progress(
    student_id: str,
    user_id: str = Depends(require_tutor),
):
    """One student's learning history, for their tutor.

    **Scoped to data a tutor can already read** — unit progress, exercise
    results, attendance, pace, and tier-gated activity. Voice and speech
    submissions are excluded pending `legal.md` L-002 (the published privacy
    policy does not mention tutors at all). The payload says so rather than
    returning an empty pronunciation section, which would read as "nothing
    recorded" instead of "not shown".

    403, never 404, on refusal: the student exists and the answer is no.
    """
    try:
        return progress_service.student_progress(user_id, student_id)
    except progress_service.ProgressDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ── Feedback triage (plan Phase 6, spec §1.6 third bullet) ────────────────────
#
# Submission is NOT here — a tutor inserts their own row directly under
# `tutor_feedback_insert_own`, the way `course_correction` works. These two
# routes are the admin half: the review queue §1.6 asks feedback to be "routed
# to", and the verdict that closes it.

@router.get("/feedback/queue")
async def feedback_queue(
    _admin: str = Depends(require_admin),
    status: str = Query(default="new"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """The internal review queue. Admin only."""
    if status not in ("new", "triaged", "actioned", "declined", "all"):
        raise HTTPException(status_code=400, detail="unknown status filter")
    return {"feedback": feedback_service.queue(status, limit)}


@router.post("/feedback/{feedback_id}/triage")
async def triage_feedback(
    feedback_id: str,
    req: TriageFeedbackRequest,
    admin_id: str = Depends(require_admin),
):
    """Record a verdict on one piece of feedback.

    The tutor can read the result — `tutor_feedback_select_own` returns the
    whole row including `admin_note`. That is the point: §1.6's complaint about
    a support email is that it goes nowhere visible.
    """
    try:
        return feedback_service.triage(feedback_id, admin_id, req.status, req.admin_note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
