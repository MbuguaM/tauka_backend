"""The account portal's view of a learner's plan. Monthly plans run through billing_service
(§151, Paystack). Only tauka-python talks to the payment provider."""

import logging

from app.services import billing_service
from app.services.billing_service import BillingRefused
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


async def get_full_subscription(user_id: str) -> dict:
    """Plan state for /account/subscription. Never includes provider tokens."""
    profile_result = db.schema("app").table("student").select(
        "tier, tier_source, subscription_status, current_period_end, active_gift_id"
    ).eq("id", user_id).execute()

    profile = profile_result.data[0] if profile_result.data else {}

    # Active gift details
    gift = None
    if profile.get("active_gift_id"):
        gift_result = db.schema("app").table("gift_subscriptions").select(
            "anonymous, expires_at, supporter_id"
        ).eq("id", profile["active_gift_id"]).execute()
        if gift_result.data:
            g = gift_result.data[0]
            gifted_by = "an anonymous giver"
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
        "tier": profile.get("tier", "free_tier"),
        "tier_source": profile.get("tier_source", "self"),
        "subscription_status": profile.get("subscription_status", "none"),
        "billing": billing_service.summary(user_id),
        "gift": gift,
    }


async def change_subscription(user_id: str, new_tier: str, success_url: str | None = None,
                              cancel_url: str | None = None, channel: str = "card") -> dict:
    """Free to paid opens a checkout. Paid to free cancels at period end. Paid to another paid
    is refused for now: Paystack has no plan swap, so a change would mean two subscriptions."""
    sub = billing_service.current(user_id)
    live = bool(sub and sub["status"] in billing_service.LIVE)

    if new_tier == "free_tier":
        if live:
            return await billing_service.cancel(user_id)
        return {"action": "no_change", "tier": "free_tier"}
    if live:
        if sub["tier"] == new_tier:
            return {"action": "no_change", "tier": new_tier}
        raise BillingRefused("plan_change_unavailable")

    result = await billing_service.checkout(user_id, new_tier, channel)
    return {"action": "checkout", "checkout_url": result["checkout_url"]}


async def reactivate_subscription(user_id: str) -> dict:
    """Undo a cancellation before the paid month ends."""
    return await billing_service.reactivate(user_id)


async def pay_now(user_id: str) -> dict:
    return await billing_service.pay_month(user_id)


async def get_billing_history(user_id: str, limit: int = 10, starting_after: str | None = None) -> list[dict]:
    """Paid months, newest first. `starting_after` is unused: the page shows the latest few."""
    return billing_service.history(user_id, limit)


async def get_payment_method(user_id: str) -> dict | None:
    """How the plan is paid. Paystack holds the card, and we keep no card details."""
    sub = billing_service.current(user_id)
    if not sub or sub["status"] not in billing_service.LIVE:
        return None
    return {"channel": sub["channel"]}


async def create_billing_portal_session(user_id: str) -> str:
    """Paystack's hosted page for changing a card."""
    return await billing_service.manage_link(user_id)
