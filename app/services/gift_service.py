import asyncio
import logging
from datetime import datetime, timedelta, timezone
import stripe
from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key


async def create_gift_checkout(
    supporter_id: str,
    duration_months: int,
    anonymous: bool,
) -> dict:
    """Create a Stripe checkout for a gift subscription."""
    if duration_months not in (1, 3):
        raise ValueError("duration_months must be 1 or 3")

    supporter_result = db.schema("app").table("test_supporters").select(
        "status, student_id, supporter_email, supporter_name"
    ).eq("id", supporter_id).execute()

    if not supporter_result.data:
        raise ValueError("Supporter not found")

    supporter = supporter_result.data[0]
    if supporter["status"] != "opted_in":
        raise ValueError("Supporter must be opted-in to gift a subscription")

    student_id = supporter["student_id"]

    # Check for overlapping active gift with >30 days remaining
    now = datetime.now(timezone.utc)
    active_gifts = db.schema("app").table("gift_subscriptions").select(
        "id, expires_at"
    ).eq("student_id", student_id).eq("status", "active").execute()

    for gift in (active_gifts.data or []):
        expires = datetime.fromisoformat(gift["expires_at"].replace("Z", "+00:00"))
        if expires - now > timedelta(days=30):
            raise ValueError("Student already has an active gift with more than 30 days remaining")

    # Look up student name
    try:
        user_result = db.auth.admin.get_user_by_id(student_id)
        student_name = (user_result.user.user_metadata or {}).get("full_name", "your student")
    except Exception:
        student_name = "your student"

    from app.services.stripe_service import create_gift_checkout as stripe_gift_checkout
    checkout = await stripe_gift_checkout(
        supporter_id=supporter_id,
        student_id=student_id,
        duration_months=duration_months,
        anonymous=anonymous,
        supporter_email=supporter["supporter_email"],
        student_name=student_name,
        success_url=f"{settings.web_base_url}/gifts/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.web_base_url}/gifts/cancel",
    )

    amount_cents = 4000 if duration_months == 1 else 12000

    db.schema("app").table("gift_subscriptions").insert({
        "supporter_id": supporter_id,
        "student_id": student_id,
        "stripe_payment_id": checkout["session_id"],
        "tier": "tutor",
        "duration_months": duration_months,
        "amount_cents": amount_cents,
        "anonymous": anonymous,
        "status": "pending",
    }).execute()

    return {"checkout_url": checkout["checkout_url"]}


async def activate_gift(stripe_session_id: str) -> dict:
    """Activate a gift after Stripe payment completes."""
    from app.services.tier_service import change_tier
    from app.services.email_service import send_template_email

    gift_result = db.schema("app").table("gift_subscriptions").select("*").eq(
        "stripe_payment_id", stripe_session_id
    ).eq("status", "pending").execute()

    if not gift_result.data:
        logger.warning("No pending gift for session: %s", stripe_session_id)
        return {}

    gift = gift_result.data[0]
    student_id = gift["student_id"]
    now = datetime.now(timezone.utc)

    # Handle stacking: extend rather than overlap
    active_result = db.schema("app").table("gift_subscriptions").select("expires_at").eq(
        "student_id", student_id
    ).eq("status", "active").execute()

    if active_result.data:
        existing_expires = datetime.fromisoformat(
            active_result.data[0]["expires_at"].replace("Z", "+00:00")
        )
        base_date = max(existing_expires, now)
    else:
        base_date = now

    expires_at = base_date + timedelta(days=30 * gift["duration_months"])

    # Get Stripe receipt URL
    try:
        session = await asyncio.to_thread(
            lambda: stripe.checkout.Session.retrieve(stripe_session_id)
        )
        receipt_url = session.get("receipt_url", "")
    except Exception:
        receipt_url = ""

    db.schema("app").table("gift_subscriptions").update({
        "status": "active",
        "activated_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "stripe_receipt_url": receipt_url,
    }).eq("id", gift["id"]).execute()

    await change_tier(
        student_id, "tutor", "gifted",
        gift_id=gift["id"],
        preserve_original=True,
    )

    # Notify student
    try:
        user_result = db.auth.admin.get_user_by_id(student_id)
        student_email = user_result.user.email if user_result and user_result.user else None
        if student_email:
            supporter_result = db.schema("app").table("test_supporters").select(
                "supporter_name"
            ).eq("id", gift["supporter_id"]).execute()
            gifted_by = (
                "An anonymous supporter"
                if gift["anonymous"]
                else (supporter_result.data[0]["supporter_name"] if supporter_result.data else "A supporter")
            )
            await send_template_email(
                to=student_email,
                template_name="gift_activated",
                template_data={
                    "subject": "You've received a Tauka gift subscription!",
                    "gifted_by": gifted_by,
                    "tier": gift["tier"],
                    "expires_at": expires_at.strftime("%B %d, %Y"),
                },
            )
    except Exception as exc:
        logger.warning("Failed to notify student of gift: %s", exc)

    # Send supporter receipt
    try:
        supporter_result = db.schema("app").table("test_supporters").select(
            "supporter_email, supporter_name"
        ).eq("id", gift["supporter_id"]).execute()
        if supporter_result.data:
            await send_template_email(
                to=supporter_result.data[0]["supporter_email"],
                template_name="gift_confirmation",
                template_data={
                    "subject": "Your Tauka gift is confirmed",
                    "supporter_name": supporter_result.data[0]["supporter_name"],
                    "duration_months": gift["duration_months"],
                    "receipt_url": receipt_url,
                    "expires_at": expires_at.strftime("%B %d, %Y"),
                },
            )
    except Exception as exc:
        logger.warning("Failed to send gift confirmation: %s", exc)

    return {"gift_id": gift["id"], "expires_at": expires_at.isoformat()}


async def process_gift_expiries() -> dict:
    """Daily cron: process gift warnings, expiries, and impact summaries."""
    from app.services.tier_service import restore_original_tier
    from app.services.email_service import send_template_email

    now = datetime.now(timezone.utc)
    warnings_sent = 0
    expired = 0
    summaries_sent = 0

    # Step 1: 14-day expiry warnings
    warning_threshold = now + timedelta(days=14)
    warn_result = db.schema("app").table("gift_subscriptions").select("*").eq(
        "status", "active"
    ).eq("expiry_warned", False).lt(
        "expires_at", warning_threshold.isoformat()
    ).gt("expires_at", now.isoformat()).execute()

    for gift in (warn_result.data or []):
        try:
            user_result = db.auth.admin.get_user_by_id(gift["student_id"])
            email = user_result.user.email if user_result and user_result.user else None
            if email:
                expires = datetime.fromisoformat(gift["expires_at"].replace("Z", "+00:00"))
                await send_template_email(
                    to=email,
                    template_name="gift_expiry_warning",
                    template_data={
                        "subject": "Your gifted Tauka access expires soon",
                        "expires_at": expires.strftime("%B %d, %Y"),
                        "days_left": (expires - now).days,
                    },
                )
                db.schema("app").table("gift_subscriptions").update({
                    "expiry_warned": True
                }).eq("id", gift["id"]).execute()
                warnings_sent += 1
        except Exception as exc:
            logger.warning("Expiry warning failed for gift %s: %s", gift["id"], exc)

    # Step 2: Actual expiration
    expired_result = db.schema("app").table("gift_subscriptions").select("*").eq(
        "status", "active"
    ).lt("expires_at", now.isoformat()).execute()

    for gift in (expired_result.data or []):
        try:
            db.schema("app").table("gift_subscriptions").update({
                "status": "expired"
            }).eq("id", gift["id"]).execute()
            await restore_original_tier(gift["student_id"])
            expired += 1
        except Exception as exc:
            logger.warning("Gift expiry failed for %s: %s", gift["id"], exc)

    # Step 3: Impact summaries (3 days after expiry)
    summary_threshold = now - timedelta(days=3)
    summary_result = db.schema("app").table("gift_subscriptions").select("*").eq(
        "status", "expired"
    ).eq("renewal_nudge_sent", False).gt(
        "expires_at", (now - timedelta(days=7)).isoformat()
    ).lt("expires_at", summary_threshold.isoformat()).execute()

    for gift in (summary_result.data or []):
        try:
            impact = await compile_gift_impact(gift["id"])
            supporter_result = db.schema("app").table("test_supporters").select(
                "supporter_email, supporter_name"
            ).eq("id", gift["supporter_id"]).execute()
            if supporter_result.data:
                await send_template_email(
                    to=supporter_result.data[0]["supporter_email"],
                    template_name="gift_impact_summary",
                    template_data={
                        "subject": "See how your gift made a difference",
                        "supporter_name": supporter_result.data[0]["supporter_name"],
                        **impact,
                    },
                )
                db.schema("app").table("gift_subscriptions").update({
                    "renewal_nudge_sent": True
                }).eq("id", gift["id"]).execute()
                summaries_sent += 1
        except Exception as exc:
            logger.warning("Impact summary failed for gift %s: %s", gift["id"], exc)

    return {
        "warnings_sent": warnings_sent,
        "expired": expired,
        "summaries_sent": summaries_sent,
    }


async def request_refund(gift_id: str, supporter_email: str) -> dict:
    """Issue a refund within 7 days of gift activation."""
    from app.services.tier_service import restore_original_tier
    from app.services.email_service import send_template_email

    gift_result = db.schema("app").table("gift_subscriptions").select("*").eq(
        "id", gift_id
    ).execute()

    if not gift_result.data:
        raise ValueError("Gift not found")

    gift = gift_result.data[0]

    supporter_result = db.schema("app").table("test_supporters").select(
        "supporter_email"
    ).eq("id", gift["supporter_id"]).execute()

    if not supporter_result.data:
        raise ValueError("Supporter not found")

    if supporter_result.data[0]["supporter_email"].lower() != supporter_email.lower():
        raise ValueError("Email does not match gift supporter")

    if gift["status"] != "active":
        raise ValueError(f"Gift is not refundable (status: {gift['status']})")

    activated_at = datetime.fromisoformat(gift["activated_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - activated_at > timedelta(days=7):
        raise ValueError("Refund window has passed (7 days)")

    # Issue Stripe refund
    from app.services.stripe_service import issue_refund
    try:
        session = await asyncio.to_thread(
            lambda: stripe.checkout.Session.retrieve(gift["stripe_payment_id"])
        )
        payment_intent_id = session.get("payment_intent")
        if not payment_intent_id:
            raise ValueError("No payment intent found for this gift")
        refund = await issue_refund(payment_intent_id)
    except Exception as exc:
        raise ValueError(f"Refund failed: {exc}") from exc

    now = datetime.now(timezone.utc)
    db.schema("app").table("gift_subscriptions").update({
        "status": "refunded",
        "refunded_at": now.isoformat(),
    }).eq("id", gift_id).execute()

    await restore_original_tier(gift["student_id"])

    # Notify student neutrally
    try:
        user_result = db.auth.admin.get_user_by_id(gift["student_id"])
        student_email = user_result.user.email if user_result and user_result.user else None
        if student_email:
            await send_template_email(
                to=student_email,
                template_name="gift_expiry_warning",
                template_data={
                    "subject": "Your gifted Tauka access has ended",
                    "message": "Your gifted access has ended. You can continue with a free account or subscribe.",
                },
            )
    except Exception as exc:
        logger.warning("Failed to notify student of refund: %s", exc)

    return {"refund_id": refund["refund_id"]}


async def compile_gift_impact(gift_id: str) -> dict:
    """Compile student progress during the gifted period."""
    gift_result = db.schema("app").table("gift_subscriptions").select("*").eq("id", gift_id).execute()
    if not gift_result.data:
        return {}

    gift = gift_result.data[0]
    activated_at = gift.get("activated_at")
    expires_at = gift.get("expires_at")
    duration_months = gift.get("duration_months", 1)

    try:
        profile_result = db.schema("app").table("student").select(
            "lessons_completed, current_streak, assessed_level"
        ).eq("id", gift["student_id"]).execute()
        profile = profile_result.data[0] if profile_result.data else {}
    except Exception:
        profile = {}

    return {
        "lessons_completed": profile.get("lessons_completed", 0) or 0,
        "sessions_attended": profile.get("cohort_sessions_attended", 0) or 0,
        "best_streak": profile.get("current_streak", 0) or 0,
        "level_progress": profile.get("assessed_level", "A1"),
        "topics_covered": ["greetings", "daily routines", "numbers"],
        "duration_description": f"{duration_months} month{'s' if duration_months > 1 else ''}",
    }
