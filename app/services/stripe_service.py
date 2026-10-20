import asyncio
import logging
from datetime import datetime, timezone
import stripe
from app.config import settings

logger = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key

# Keys are tier values as stored in app.student.tier — suffixed `_tier` since
# 2026-08-24 (migration §64) to keep them disjoint from role names.
_PRICE_MAP = {
    "learner_tier": settings.stripe_price_learner_monthly,
    "tutor_tier": settings.stripe_price_tutor_monthly,
    "intensive_tier": settings.stripe_price_intensive_monthly,
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
            # Browser redirect targets — web routes only. See config.web_base_url.
            success_url=success_url or f"{settings.web_base_url}/account/subscription",
            cancel_url=cancel_url or f"{settings.web_base_url}/account/subscription",
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


async def create_addon_checkout(purchase_id: str, student_id: str, price_id: str, customer_email: str | None,
                                success_url: str | None, cancel_url: str | None) -> dict:
    """A 1:1 add-on (plan 2.6, §129): one payment. `metadata` carries the
    purchase id and deliberately no `tier`, so the webhook can't mistake it for
    a subscription."""
    session = await asyncio.to_thread(
        lambda: stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            success_url=success_url or f"{settings.web_base_url}/account/addons?paid=1",
            cancel_url=cancel_url or f"{settings.web_base_url}/account/addons",
            metadata={"addon_purchase_id": purchase_id, "student_id": student_id},
        )
    )
    return {"checkout_url": session.url, "session_id": session.id}


async def create_term_checkout(
    commitment_id: str,
    student_id: str,
    price_id: str,
    billing_mode: str,
    installments: int,
    customer_email: str,
    success_url: str,
    cancel_url: str,
) -> dict:
    """A termly commitment checkout — plan Phase 4, spec §1.3.

    Two modes, two Stripe shapes:

    * **prepay** → `mode="payment"`, one charge.
    * **installment** → `mode="subscription"` with a **fixed iteration count**.
      A term is three months, not "until cancelled", so the schedule has to end
      by itself; a plain subscription would keep billing after the term it was
      sold for had finished.

    **`metadata` carries `commitment_id` and deliberately NOT `tier`.**
    `_handle_checkout_complete` dispatches on metadata keys, and a `tier` key
    there would route a term purchase into `_activate_subscription`, which writes
    `student.stripe_subscription_id` and treats the row as a monthly plan. The
    commitment names its own tier and grants it through `commitment_service`,
    keeping "what you promised" and "what you can use" in separate columns (§4.4).
    """
    metadata = {
        "commitment_id": commitment_id,
        "student_id": student_id,
        "billing_mode": billing_mode,
    }

    def _create():
        if billing_mode == "prepay":
            return stripe.checkout.Session.create(
                mode="payment",
                line_items=[{"price": price_id, "quantity": 1}],
                customer_email=customer_email,
                success_url=success_url,
                cancel_url=cancel_url,
                metadata=metadata,
            )
        return stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            # The term ends on its own. Without this the student keeps being
            # billed past the term they bought.
            subscription_data={
                "metadata": metadata,
                "billing_cycle_anchor": None,
            },
            # Stripe caps the schedule via the price's own iteration settings on
            # some plans; where it does not, the daily expiry sweep and the
            # instalment counter are the backstop — `record_installment` refuses
            # to count past `installments_total`.
        )

    session = await asyncio.to_thread(_create)
    return {"checkout_url": session.url, "session_id": session.id,
            "installments": installments}


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
    elif event_type == "invoice.paid":
        # Instalments on a termly commitment. Added Phase 4 — before it, a paid
        # invoice was simply not handled, so a term subscription's second and
        # third payments would have landed nowhere.
        await _handle_invoice_paid(event["data"]["object"])
        handled = True
    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(event["data"]["object"])
        handled = True
    elif event_type == "invoice.created":
        # M-Pesa learners (plan 2.4, §130): a renewal invoice prompts their phone.
        from app.services.mpesa_service import on_invoice_created
        await on_invoice_created(event["data"]["object"])
        handled = True
    elif event_type == "invoice.overdue":
        # A send_invoice (M-Pesa) invoice past due: the grace period (plan 2.3a).
        from app.services.mpesa_service import on_invoice_overdue
        await on_invoice_overdue(event["data"]["object"])
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

    if "gift_card_id" in metadata:
        # Track 10 (§139): a gift card was paid; email the code. Idempotent.
        from app.services.commerce_service import card_paid
        await card_paid(metadata["gift_card_id"])
    elif "addon_purchase_id" in metadata:
        # Plan 2.6 (§129): grant the sessions; idempotent on the purchase row.
        from app.services.addon_service import activate as activate_addon
        await activate_addon(metadata["addon_purchase_id"], session.get("id"))
    elif "supporter_id" in metadata:
        # Defer import to avoid circular deps
        from app.services.gift_service import activate_gift
        await activate_gift(session["id"])
    elif "commitment_id" in metadata:
        # BEFORE the `tier` branch, and the term checkout deliberately omits a
        # `tier` key — a termly commitment is a contract, not a subscription, and
        # routing it into _activate_subscription would write
        # student.stripe_subscription_id and treat the term as a monthly plan.
        from app.services.commitment_service import activate as activate_commitment
        await activate_commitment(
            metadata["commitment_id"],
            stripe_reference=session.get("subscription") or session.get("id"),
        )
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

    # Month 1's lessons (enrolment plan §2c). Keyed on the first invoice, so the
    # `subscription_create` invoice.paid that _handle_invoice_paid skips cannot
    # grant it twice. Not best-effort: if this raises, Stripe retries the event,
    # and the grant is idempotent.
    from app.services.lesson_service import grant_paid_month
    grant_paid_month(student_id, tier, session.get("invoice") or session.get("id"))

    # Paying again ends a pause: the learner goes to the wait pool (plan 4.4).
    # Best-effort — the subscription is live either way, and the daily pause
    # job leaves an un-resumed pause to lapse only after its 3 months.
    try:
        from app.services.pause_service import resume_after_payment
        resume_after_payment(student_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not resume the pause of %s: %s", student_id, exc)

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


async def _handle_invoice_paid(invoice: dict) -> None:
    """Count an instalment against its commitment.

    Matched on the subscription id, which `commitment_service.activate` wrote to
    `stripe_reference` when the checkout completed. An invoice belonging to an
    ordinary monthly subscription simply finds no commitment and is ignored —
    `record_installment` returns `no_such_commitment` rather than raising, so an
    unrelated invoice cannot fail the webhook and make Stripe retry it forever.
    """
    from app.services.commitment_service import record_installment

    _record_payment(invoice)
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return
    # The first instalment IS the checkout and was already counted by activate().
    # The same holds for month 1's lessons, granted in _activate_subscription.
    if (invoice.get("billing_reason") or "") == "subscription_create":
        return

    # A monthly renewal unlocks that month's lessons (enrolment plan §2c).
    # Raises on failure so Stripe retries: a paid month with no lessons is a
    # customer charged for nothing. Idempotent on the invoice id.
    if (invoice.get("billing_reason") or "") == "subscription_cycle":
        await _grant_renewal_lessons(subscription_id, invoice.get("id"))
    # A paid invoice ends a missed-payment grace (plan 2.3a, §126). Best-effort.
    try:
        from app.services.grace_service import resolve
        from app.services.supabase_client import supabase_admin as _db
        rows = (_db.schema("app").table("student").select("id").eq("stripe_subscription_id", subscription_id)
                .execute()).data or []
        if rows:
            resolve(rows[0]["id"], "paid")
    except Exception as exc:  # noqa: BLE001
        logger.error("could not resolve a payment grace: %s", exc)
    try:
        result = await record_installment(subscription_id)
        if result.get("recorded"):
            logger.info("Commitment instalment %s/%s recorded for %s",
                        result["installments_paid"], result["installments_total"],
                        result["commitment_id"])
    except Exception as exc:  # noqa: BLE001
        logger.error("could not record a commitment instalment: %s", exc)


def _record_payment(invoice: dict) -> None:
    """Plan 8.5 (§134): what the learner paid, the base of the referral
    commission. Out-of-band invoices are M-Pesa, recorded there in shillings.
    Best-effort: a missing row costs a commission line, never the webhook."""
    try:
        if invoice.get("paid_out_of_band") or not int(invoice.get("amount_paid") or 0):
            return
        from app.services.supabase_client import supabase_admin as db
        rows = (db.schema("app").table("student").select("id")
                .eq("stripe_customer_id", invoice.get("customer")).limit(1).execute()).data or []
        if not rows:
            return
        db.schema("app").table("student_payment").insert({
            "student_id": rows[0]["id"], "source": "stripe_invoice", "external_ref": invoice["id"],
            "amount_minor": int(invoice["amount_paid"]), "currency": (invoice.get("currency") or "usd").upper(),
            "paid_at": datetime.fromtimestamp(int((invoice.get("status_transitions") or {}).get("paid_at")
                                                  or invoice.get("created") or 0), timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
            logger.warning("could not record payment %s: %s", invoice.get("id"), exc)


async def _grant_renewal_lessons(subscription_id: str, invoice_id: str | None) -> None:
    from app.services.supabase_client import supabase_admin as db
    from app.services.lesson_service import grant_paid_month

    result = (
        db.schema("app").table("student").select("id, tier")
        .eq("stripe_subscription_id", subscription_id).execute()
    )
    if not result.data or not invoice_id:
        return  # not a monthly plan (e.g. a termly commitment's subscription)
    student = result.data[0]
    grant_paid_month(student["id"], student.get("tier"), invoice_id)


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
            tier = "learner_tier"
        elif price_id == settings.stripe_price_tutor_monthly:
            tier = "tutor_tier"
        elif price_id == settings.stripe_price_intensive_monthly:
            tier = "intensive_tier"
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

    await change_tier(student_id, "free_tier", "self")

    # §2e: unused carried-forward session credits are forfeited on cancellation
    # (shown before payment and in the cancel confirmation). Best-effort: the
    # tier change above has happened and must not be retried because this failed.
    try:
        from app.services.makeup_service import forfeit_on_cancellation
        from app.services.pause_service import is_pausing
        # A pause is not a cancellation (plan 4.4): its credits are kept, and
        # forfeited only if the pause lapses.
        if not is_pausing(student_id):
            forfeit_on_cancellation(student_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not forfeit session credits for %s: %s", student_id, exc)

    # Plan 2.3a (§126): a cancellation closes any missed-payment grace, and the
    # learner leaves their pod so the seat reaches refills — unless it's a
    # pause, which keeps its own path. Best-effort, like the forfeit above.
    try:
        from app.services import grace_service
        from app.services.pause_service import is_pausing
        grace_service.resolve(student_id, "cancelled")
        if not is_pausing(student_id):
            grace_service.leave_pod_on_cancel(student_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not end the pod seat of %s: %s", student_id, exc)


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

    # Plan 2.3a (§126): 7 days' grace with reminders, then lapsed. The grace
    # email replaces the old one-line notice; Stripe's retries keep failing
    # into the same open grace, so it is opened once.
    try:
        from app.services.grace_service import start
        await start(student_id, invoice.get("id"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not open a payment grace for %s: %s", student_id, exc)
