import asyncio
import logging
import stripe
from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
stripe.api_key = settings.stripe_secret_key

_TIER_PRICES = {
    "learner": settings.stripe_price_learner_monthly,
    "tutor": settings.stripe_price_tutor_monthly,
    "intensive": settings.stripe_price_intensive_monthly,
}

_PRICE_TO_TIER = {v: k for k, v in _TIER_PRICES.items() if v}


async def get_full_subscription(user_id: str) -> dict:
    """Return merged Supabase + Stripe subscription state for the account portal."""
    profile_result = db.schema("app").table("student").select(
        "tier, tier_source, subscription_status, stripe_customer_id, "
        "stripe_subscription_id, current_period_end, active_gift_id, original_tier"
    ).eq("id", user_id).execute()

    profile = profile_result.data[0] if profile_result.data else {}
    tier = profile.get("tier", "free")
    tier_source = profile.get("tier_source", "self")
    stripe_customer_id = profile.get("stripe_customer_id")
    stripe_subscription_id = profile.get("stripe_subscription_id")

    stripe_data: dict = {}
    if stripe_customer_id and stripe_subscription_id:
        try:
            sub = await asyncio.to_thread(
                lambda: stripe.Subscription.retrieve(
                    stripe_subscription_id,
                    expand=["default_payment_method", "latest_invoice"],
                )
            )
            pm = sub.get("default_payment_method")
            payment_method = None
            if pm and isinstance(pm, dict):
                card = pm.get("card", {})
                payment_method = {
                    "brand": card.get("brand"),
                    "last4": card.get("last4"),
                    "exp_month": card.get("exp_month"),
                    "exp_year": card.get("exp_year"),
                }

            # Upcoming invoice
            upcoming = None
            try:
                inv = await asyncio.to_thread(
                    lambda: stripe.Invoice.upcoming(customer=stripe_customer_id)
                )
                # PY-003: keep Stripe's integer cents; expose a rounded
                # display value only at serialization.
                amount_due_cents = int(inv.get("amount_due", 0) or 0)
                upcoming = {
                    "amount_cents": amount_due_cents,
                    "amount": round(amount_due_cents / 100, 2),
                    "date": inv.get("next_payment_attempt"),
                }
            except Exception:
                pass

            price_id = (sub.get("items", {}).get("data", [{}]) or [{}])[0].get("price", {}).get("id", "")
            stripe_data = {
                "plan_name": _PRICE_TO_TIER.get(price_id, tier).capitalize(),
                "price_monthly_cents": _plan_price_cents(tier),
                "current_period_end": sub.get("current_period_end"),
                "cancel_at_period_end": sub.get("cancel_at_period_end", False),
                "payment_method": payment_method,
                "upcoming_invoice": upcoming,
            }
        except Exception as exc:
            logger.warning("Stripe subscription fetch failed: %s", exc)

    # Active gift details
    gift = None
    if profile.get("active_gift_id"):
        gift_result = db.schema("app").table("gift_subscriptions").select(
            "anonymous, expires_at, supporter_id"
        ).eq("id", profile["active_gift_id"]).execute()
        if gift_result.data:
            g = gift_result.data[0]
            gifted_by = "angel supporter"
            if not g["anonymous"]:
                sup = db.schema("app").table("test_supporters").select("supporter_name").eq(
                    "id", g["supporter_id"]
                ).execute()
                if sup.data:
                    gifted_by = sup.data[0]["supporter_name"]
            gift = {
                "gifted_by": gifted_by,
                "anonymous": g["anonymous"],
                "expires_at": g["expires_at"],
            }

    return {
        "tier": tier,
        "tier_source": tier_source,
        "subscription_status": profile.get("subscription_status", "none"),
        "stripe": stripe_data or None,
        "gift": gift,
    }


async def change_subscription(user_id: str, new_tier: str, success_url: str | None, cancel_url: str | None) -> dict:
    """Handle all plan change scenarios."""
    from app.services.tier_service import change_tier
    from app.services.stripe_service import create_subscription_checkout

    profile_result = db.schema("app").table("student").select(
        "tier, tier_source, stripe_customer_id, stripe_subscription_id, active_gift_id"
    ).eq("id", user_id).execute()

    profile = profile_result.data[0] if profile_result.data else {}
    current_tier = profile.get("tier", "free")
    stripe_sub_id = profile.get("stripe_subscription_id")
    stripe_customer_id = profile.get("stripe_customer_id")

    # Cannot downgrade while active gift is present
    if profile.get("active_gift_id") and new_tier != "free":
        from app.services.tier_service import TIER_RANK
        # Allow changes, they'll apply after gift expires
        pass

    # Free → Paid: create Checkout Session
    if current_tier == "free" and new_tier != "free":
        try:
            user_result = db.auth.admin.get_user_by_id(user_id)
            email = user_result.user.email if user_result and user_result.user else ""
        except Exception:
            email = ""
        result = await create_subscription_checkout(
            student_id=user_id,
            tier=new_tier,
            # /account/subscription/success is not a route either — the portal
            # has only /account/subscription, which shows post-webhook state.
            success_url=success_url or f"{settings.web_base_url}/account/subscription",
            cancel_url=cancel_url or f"{settings.web_base_url}/account/subscription",
            customer_email=email,
        )
        return {"action": "checkout", "checkout_url": result["checkout_url"]}

    # Paid → Free: cancel at period end
    if new_tier == "free" and stripe_sub_id:
        await asyncio.to_thread(
            lambda: stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=True)
        )
        db.schema("app").table("student").update({
            "subscription_status": "cancelled",
        }).eq("id", user_id).execute()
        return {"action": "cancelled", "message": "Subscription will cancel at period end"}

    # Paid → Different Paid: update Stripe subscription
    if stripe_sub_id and new_tier not in ("free", current_tier):
        price_id = _TIER_PRICES.get(new_tier)
        if not price_id:
            raise ValueError(f"Unknown tier: {new_tier}")
        sub = await asyncio.to_thread(
            lambda: stripe.Subscription.retrieve(stripe_sub_id)
        )
        item_id = sub["items"]["data"][0]["id"]
        updated = await asyncio.to_thread(
            lambda: stripe.Subscription.modify(
                stripe_sub_id,
                items=[{"id": item_id, "price": price_id}],
                proration_behavior="always_invoice",
            )
        )
        await change_tier(user_id, new_tier, "self")
        return {
            "action": "updated",
            "new_tier": new_tier,
            "effective_date": updated.get("current_period_start"),
        }

    return {"action": "no_change", "tier": current_tier}


async def reactivate_subscription(user_id: str) -> dict:
    """Reactivate a subscription that was cancelled but hasn't ended yet."""
    profile_result = db.schema("app").table("student").select(
        "stripe_subscription_id"
    ).eq("id", user_id).execute()

    if not profile_result.data or not profile_result.data[0].get("stripe_subscription_id"):
        raise ValueError("No subscription to reactivate")

    sub_id = profile_result.data[0]["stripe_subscription_id"]
    sub = await asyncio.to_thread(
        lambda: stripe.Subscription.modify(sub_id, cancel_at_period_end=False)
    )
    db.schema("app").table("student").update({
        "subscription_status": "active",
    }).eq("id", user_id).execute()

    return {
        "status": "active",
        "next_billing": sub.get("current_period_end"),
    }


async def get_billing_history(
    user_id: str,
    limit: int = 10,
    starting_after: str | None = None,
) -> list[dict]:
    """Return Stripe invoice history for the student."""
    profile_result = db.schema("app").table("student").select(
        "stripe_customer_id"
    ).eq("id", user_id).execute()

    if not profile_result.data or not profile_result.data[0].get("stripe_customer_id"):
        return []

    customer_id = profile_result.data[0]["stripe_customer_id"]
    params: dict = {"customer": customer_id, "limit": limit}
    if starting_after:
        params["starting_after"] = starting_after

    invoices = await asyncio.to_thread(lambda: stripe.Invoice.list(**params))

    return [
        {
            "invoice_id": inv.get("id"),
            "date": inv.get("created"),
            "amount_cents": int(inv.get("amount_paid") or 0),
            "amount": round(int(inv.get("amount_paid") or 0) / 100, 2),
            "currency": inv.get("currency", "usd").upper(),
            "status": inv.get("status"),
            "description": inv.get("description") or _invoice_description(inv),
            "pdf_url": inv.get("invoice_pdf"),
        }
        for inv in (invoices.get("data") or [])
    ]


async def get_payment_method(user_id: str) -> dict | None:
    """Return the student's current payment method details."""
    profile_result = db.schema("app").table("student").select(
        "stripe_customer_id, stripe_subscription_id"
    ).eq("id", user_id).execute()

    if not profile_result.data:
        return None

    profile = profile_result.data[0]
    if not profile.get("stripe_subscription_id"):
        return None

    try:
        sub = await asyncio.to_thread(
            lambda: stripe.Subscription.retrieve(
                profile["stripe_subscription_id"],
                expand=["default_payment_method"],
            )
        )
        pm = sub.get("default_payment_method")
        if not pm or not isinstance(pm, dict):
            return None
        card = pm.get("card", {})
        return {
            "brand": card.get("brand"),
            "last4": card.get("last4"),
            "exp_month": card.get("exp_month"),
            "exp_year": card.get("exp_year"),
        }
    except Exception as exc:
        logger.warning("get_payment_method failed: %s", exc)
        return None


async def create_billing_portal_session(user_id: str) -> str:
    """Create a Stripe Billing Portal session for payment method updates."""
    profile_result = db.schema("app").table("student").select(
        "stripe_customer_id"
    ).eq("id", user_id).execute()

    if not profile_result.data or not profile_result.data[0].get("stripe_customer_id"):
        raise ValueError("No Stripe customer for this user")

    customer_id = profile_result.data[0]["stripe_customer_id"]
    session = await asyncio.to_thread(
        lambda: stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{settings.web_base_url}/account/subscription",
        )
    )
    return session.url


def _plan_price_cents(tier: str) -> int:
    """PY-003: monthly plan price in integer cents (never float)."""
    prices = {"learner": 1400, "tutor": 4000, "intensive": 11900}
    return prices.get(tier, 0)


def _invoice_description(inv: dict) -> str:
    lines = inv.get("lines", {}).get("data", [])
    if lines:
        return lines[0].get("description", "Tauka subscription")
    return "Tauka subscription"
