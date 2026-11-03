import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.dependencies import get_current_user
from app.models.schemas import SubscriptionCheckoutRequest, SubscriptionStatusResponse
from app.services import billing_service
from app.services.billing_service import BillingRefused
from app.services.stripe_service import process_webhook_event
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()

_STATUS = {"already_subscribed": 409, "not_configured": 503, "not_enabled": 403, "no_such_plan": 400,
           "not_ready": 409, "payment_failed": 502}


def _refused(exc: BillingRefused) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


@router.post("/checkout")
async def create_checkout(
    req: SubscriptionCheckoutRequest,
    user_id: str = Depends(get_current_user),
):
    """A signed-in learner starts a monthly plan (§151). Refuses a live plan, which would
    bill them twice. Enrolment plan H4 / 2.1."""
    try:
        return await billing_service.checkout(user_id, req.tier, req.channel)
    except BillingRefused as exc:
        raise _refused(exc)


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook endpoint, still used by add-ons, commitments and institutions.
    Paystack's is POST /payments/paystack/webhook."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        result = await process_webhook_event(payload, sig_header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Webhook processing error: %s", exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed")

    return result


@router.get("/status", response_model=SubscriptionStatusResponse)
async def subscription_status(user_id: str = Depends(get_current_user)):
    """Current subscription state for the authenticated student."""
    result = db.schema("app").table("student").select(
        "tier, tier_source, subscription_status, current_period_end, active_gift_id"
    ).eq("id", user_id).execute()

    if not result.data:
        return SubscriptionStatusResponse(
            tier="free_tier",
            source="self",
            status="none",
            current_period_end=None,
            active_gift=None,
        )

    profile = result.data[0]
    active_gift = None

    if profile.get("active_gift_id"):
        gift_result = db.schema("app").table("gift_subscriptions").select(
            "anonymous, expires_at, supporter_id"
        ).eq("id", profile["active_gift_id"]).execute()

        if gift_result.data:
            gift = gift_result.data[0]
            gifted_by = "angel"
            if not gift["anonymous"]:
                sup = db.schema("app").table("test_supporters").select("supporter_name").eq(
                    "id", gift["supporter_id"]
                ).execute()
                if sup.data:
                    gifted_by = sup.data[0]["supporter_name"]
            active_gift = {
                "gifted_by": gifted_by,
                "expires_at": gift["expires_at"],
            }

    return SubscriptionStatusResponse(
        tier=profile.get("tier", "free_tier"),
        source=profile.get("tier_source", "self"),
        status=profile.get("subscription_status", "none"),
        current_period_end=profile.get("current_period_end"),
        active_gift=active_gift,
    )


@router.post("/cancel")
async def cancel_subscription(user_id: str = Depends(get_current_user)):
    """Cancel a monthly plan at period end."""
    try:
        await billing_service.cancel(user_id)
    except BillingRefused as exc:
        raise _refused(exc)
    return {"status": "will_cancel_at_period_end"}
