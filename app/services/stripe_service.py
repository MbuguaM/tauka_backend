import asyncio
import logging
import stripe
from app.config import settings

logger = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key

_PRICE_MAP = {
    "learner": settings.stripe_price_learner_monthly,
    "tutor": settings.stripe_price_tutor_monthly,
    "intensive": settings.stripe_price_intensive_monthly,
}


def _run_sync(coro):
    """Run a sync stripe call in the thread pool to avoid blocking the event loop."""
    return asyncio.to_thread(coro)


async def create_subscription_checkout(
    student_id: str,
    tier: str,
    success_url: str,
    cancel_url: str,
    customer_email: str,
) -> dict:
    price_id = _PRICE_MAP.get(tier)
    if not price_id:
        raise ValueError(f"Unknown tier: {tier}")

    session = await asyncio.to_thread(
        lambda: stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            success_url=success_url or f"{settings.app_base_url}/subscription/success",
            cancel_url=cancel_url or f"{settings.app_base_url}/subscription/cancel",
            metadata={"student_id": student_id, "tier": tier},
        )
    )
    return {"checkout_url": session.url, "session_id": session.id}


async def create_gift_checkout(
    supporter_id: str,
    student_id: str,
    duration_months: int,
    anonymous: bool,
    supporter_email: str,
    student_name: str,
    success_url: str,
    cancel_url: str,
) -> dict:
    price_id = (
        settings.stripe_price_gift_1mo if duration_months == 1
        else settings.stripe_price_gift_3mo
    )
    if not price_id:
        raise ValueError("Gift price IDs not configured")

    session = await asyncio.to_thread(
        lambda: stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=supporter_email,
            success_url=success_url or f"{settings.web_base_url}/gifts/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=cancel_url or f"{settings.web_base_url}/gifts/cancel",
            metadata={
                "supporter_id": supporter_id,
                "student_id": student_id,
                "duration_months": str(duration_months),
                "anonymous": str(anonymous).lower(),
                "student_name": student_name,
            },
        )
    )
    return {"checkout_url": session.url, "session_id": session.id}


async def process_webhook_event(payload: bytes, sig_header: str) -> dict:
    try:
        event = await asyncio.to_thread(
            lambda: stripe.Webhook.construct_event(
                payload, sig_header, settings.stripe_webhook_secret
            )
        )
    except stripe.SignatureVerificationError:
        raise ValueError("Invalid webhook signature")

    event_type = event["type"]
    handled = False

    if event_type == "checkout.session.completed":
        await _handle_checkout_complete(event["data"]["object"])
        handled = True
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_update(event["data"]["object"])
        handled = True
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_cancel(event["data"]["object"])
        handled = True
    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(event["data"]["object"])
        handled = True

    return {"handled": handled, "event_type": event_type}


async def issue_refund(payment_intent_id: str) -> dict:
    refund = await asyncio.to_thread(
        lambda: stripe.Refund.create(payment_intent=payment_intent_id)
    )
    return {"refund_id": refund.id, "status": refund.status}


# ── Internal webhook handlers ─────────────────────────────────────────────────

async def _handle_checkout_complete(session: dict) -> None:
    metadata = session.get("metadata", {})

    if "supporter_id" in metadata:
        # Defer import to avoid circular deps
        from app.services.gift_service import activate_gift
        await activate_gift(session["id"])
    elif "tier" in metadata:
        await _activate_subscription(session)
    else:
        logger.warning("Unknown checkout session type: %s", session.get("id"))


async def _activate_subscription(session: dict) -> None:
    from app.services.supabase_client import supabase_admin as db
    from app.services.tier_service import change_tier
    from app.services.email_service import send_template_email

    metadata = session.get("metadata", {})
    student_id = metadata.get("student_id")
    tier = metadata.get("tier")
    stripe_customer_id = session.get("customer")
    stripe_subscription_id = session.get("subscription")

    if not student_id or not tier:
        logger.warning("Missing metadata in subscription checkout: %s", session.get("id"))
        return

    db.schema("app").table("student").upsert({
        "id": student_id,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "subscription_status": "active",
    }).execute()

    await change_tier(student_id, tier, "self")

    # Send receipt email (best-effort)
    try:
        student = db.schema("app").table("student").select("id").eq("id", student_id).execute()
        from supabase import Client
        user_result = db.auth.admin.get_user_by_id(student_id)
        email = user_result.user.email if user_result and user_result.user else None
        if email:
            await send_template_email(
                to=email,
                template_name="subscription_receipt",
                template_data={"tier": tier, "subject": "Your Tauka subscription is active"},
            )
    except Exception as exc:
        logger.warning("Failed to send subscription receipt: %s", exc)


async def _handle_subscription_update(subscription: dict) -> None:
    from app.services.supabase_client import supabase_admin as db
    from app.services.tier_service import change_tier

    stripe_subscription_id = subscription.get("id")
    status = subscription.get("status", "")
    current_period_end = subscription.get("current_period_end")

    result = db.schema("app").table("student").select(
        "id, tier"
    ).eq("stripe_subscription_id", stripe_subscription_id).execute()

    if not result.data:
        return

    student_id = result.data[0]["id"]
    db.schema("app").table("student").update({
        "subscription_status": status,
        "current_period_end": current_period_end,
    }).eq("id", student_id).execute()

    # Sync tier from plan
    items = subscription.get("items", {}).get("data", [])
    if items:
        price_id = items[0].get("price", {}).get("id", "")
        tier = None
        if price_id == settings.stripe_price_learner_monthly:
            tier = "learner"
        elif price_id == settings.stripe_price_tutor_monthly:
            tier = "tutor"
        elif price_id == settings.stripe_price_intensive_monthly:
            tier = "intensive"
        if tier:
            await change_tier(student_id, tier, "self")


async def _handle_subscription_cancel(subscription: dict) -> None:
    from app.services.supabase_client import supabase_admin as db
    from app.services.tier_service import change_tier

    stripe_subscription_id = subscription.get("id")
    result = db.schema("app").table("student").select("id").eq(
        "stripe_subscription_id", stripe_subscription_id
    ).execute()

    if not result.data:
        return

    student_id = result.data[0]["id"]
    db.schema("app").table("student").update({
        "subscription_status": "cancelled",
        "stripe_subscription_id": None,
    }).eq("id", student_id).execute()

    await change_tier(student_id, "free", "self")


async def _handle_payment_failed(invoice: dict) -> None:
    from app.services.supabase_client import supabase_admin as db
    from app.services.email_service import send_template_email

    stripe_customer_id = invoice.get("customer")
    result = db.schema("app").table("student").select("id").eq(
        "stripe_customer_id", stripe_customer_id
    ).execute()

    if not result.data:
        return

    student_id = result.data[0]["id"]
    db.schema("app").table("student").update({
        "subscription_status": "past_due",
    }).eq("id", student_id).execute()

    try:
        user_result = db.auth.admin.get_user_by_id(student_id)
        email = user_result.user.email if user_result and user_result.user else None
        if email:
            await send_template_email(
                to=email,
                template_name="subscription_receipt",
                template_data={
                    "subject": "Action required: Tauka payment failed",
                    "message": "Your recent payment failed. Please update your payment method.",
                },
            )
    except Exception as exc:
        logger.warning("Failed to send payment failed email: %s", exc)
