import asyncio
import logging
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from app.config import settings
from app.dependencies import get_current_user
from app.models.schemas import SubscriptionCheckoutRequest, SubscriptionStatusResponse
from app.services.stripe_service import create_subscription_checkout, process_webhook_event
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/checkout")
async def create_checkout(
    req: SubscriptionCheckoutRequest,
    user_id: str = Depends(get_current_user),
):
    """Authenticated student creates a Stripe Checkout for self-pay subscription."""
    try:
        user_result = db.auth.admin.get_user_by_id(user_id)
        email = user_result.user.email if user_result and user_result.user else ""
    except Exception:
        email = ""

    try:
        result = await create_subscription_checkout(
            student_id=user_id,
            tier=req.tier,
            # Stripe redirects a browser here, so it must be a web route.
            # /account/subscription renders current state and so reads correctly
            # after either outcome; /subscription/success has never existed.
            success_url=req.success_url or f"{settings.web_base_url}/account/subscription",
            cancel_url=req.cancel_url or f"{settings.web_base_url}/account/subscription",
            customer_email=email,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Checkout creation failed: {exc}")

    return {"checkout_url": result["checkout_url"]}


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook endpoint — no auth, Stripe signs the payload."""
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
            tier="free",
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
        tier=profile.get("tier", "free"),
        source=profile.get("tier_source", "self"),
        status=profile.get("subscription_status", "none"),
        current_period_end=profile.get("current_period_end"),
        active_gift=active_gift,
    )


@router.post("/cancel")
async def cancel_subscription(user_id: str = Depends(get_current_user)):
    """Cancel self-pay subscription at period end."""
    profile_result = db.schema("app").table("student").select(
        "stripe_subscription_id"
    ).eq("id", user_id).execute()

    if not profile_result.data or not profile_result.data[0].get("stripe_subscription_id"):
        raise HTTPException(status_code=400, detail="No active subscription to cancel")

    sub_id = profile_result.data[0]["stripe_subscription_id"]

    try:
        await asyncio.to_thread(
            lambda: stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cancellation failed: {exc}")

    db.schema("app").table("student").update({
        "subscription_status": "cancelled",
    }).eq("id", user_id).execute()

    return {"status": "will_cancel_at_period_end"}
