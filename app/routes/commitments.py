"""Termly commitments — plan Phase 4, spec §1.3 / §2.3.

**Not purchasable yet (D-18).** No Stripe price objects exist for either billing
mode, so `POST /commitments/checkout` returns **503** by name rather than letting
Stripe reject an empty price id with an error nobody could act on. The web
pricing section is gated `'coming'` in `featureStatus.js` for the same reason.

Everything else here works: a student can read the contract they hold, and an
admin can cancel one.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user, require_admin
from app.models.schemas import CancelCommitmentRequest, CommitmentCheckoutRequest
from app.services import commitment_service
from app.services.commitment_service import CommitmentUnavailable

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/mine")
async def my_commitment(user_id: str = Depends(get_current_user)):
    """STUDENT — the termly contract I hold, or null.

    Carries `days_remaining` and `milestone_reached` so §2.3's screen can lead
    with the guarantee — *"your slot with [tutor] is protected for the term"* —
    rather than with a price.
    """
    commitment = await commitment_service.get_active(user_id)
    return {"commitment": commitment, "has_commitment": commitment is not None}


@router.post("/checkout")
async def checkout(req: CommitmentCheckoutRequest,
                   user_id: str = Depends(get_current_user)):
    """STUDENT — start a term checkout.

    Writes a `pending` commitment **before** calling Stripe, so a completed
    payment always has somewhere to land. An abandoned checkout leaves a pending
    row that never activates, which is visible and cheaper than a webhook
    arriving with nothing to attach itself to.
    """
    from app.services.supabase_client import supabase_admin as db

    email = req.customer_email
    if not email:
        try:
            user = db.auth.admin.get_user_by_id(user_id)
            email = user.user.email if user and user.user else None
        except Exception:  # noqa: BLE001
            email = None
    if not email:
        raise HTTPException(status_code=400, detail="No email on file for this account")

    try:
        return await commitment_service.create_commitment_checkout(
            student_id=user_id,
            course_id=req.course_id,
            billing_mode=req.billing_mode,
            tier=req.tier,
            locked_home_tutor_id=req.locked_home_tutor_id,
            amount_minor=req.amount_minor,
            currency=req.currency,
            customer_email=email,
            milestone_target=req.milestone_target,
            success_url=req.success_url,
            cancel_url=req.cancel_url,
        )
    except CommitmentUnavailable as exc:
        # 503, not 400: nothing about the request is wrong. The product is not
        # configured yet, and the client should say "coming soon", not "invalid".
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/cancel")
async def cancel(req: CancelCommitmentRequest, admin_id: str = Depends(require_admin)):
    """ADMIN — cancel a commitment.

    **Admin only, deliberately.** `commitment.self_cancel` is `'none'`: a termly
    plan is sold on a guarantee that runs both ways, and a self-serve cancel
    button would need a refund policy that does not exist yet.

    The tier is **not** reverted. The refund and the entitlement are separate
    decisions, and a cancel that silently downgrades someone mid-conversation is
    a support incident.
    """
    result = await commitment_service.cancel(req.commitment_id, admin_id, req.reason)
    if not result.get("cancelled"):
        raise HTTPException(status_code=409, detail=result.get("reason", "could not cancel"))
    return result


@router.get("/student/{student_id}")
async def admin_student_commitment(student_id: str,
                                   admin_id: str = Depends(require_admin)):
    """ADMIN — the commitment for one student, and the tutor it locks.

    The locked tutor is what the matcher must honour (§1.3), so an admin
    reassigning a roster needs to see it before trying.
    """
    commitment = await commitment_service.get_active(student_id)
    return {
        "commitment": commitment,
        "locked_home_tutor_id": commitment_service.locked_tutor_for(student_id),
    }
