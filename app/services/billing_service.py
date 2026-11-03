"""Monthly plans through Paystack (§151, API-2, decision D7).

THE TWO WAYS TO PAY
  card    a Paystack subscription. checkout() opens one; Paystack charges each month and
          tells us by webhook. It doesn't retry a failed charge, so a learner in grace can
          also pay the month once from their account (pay_month).
  mpesa   Paystack's subscriptions don't cover M-Pesa, so there's nothing to renew. We remind
          the learner before the month ends and they pay it (pay_month). Behind mpesa_payments.

WHAT A PAID MONTH DOES (settle_month, idempotent on the payment row)
  lessons granted, tier set, period moved on a month, then best effort: pause ended, grace
  resolved, ledger row, receipt. If a required step fails, the row goes back to pending and
  the webhook answers 500, so Paystack sends it again.

MISSED AND CANCELLED
  invoice.payment_failed, or a period that ran out with nothing paid   -> grace_service
  cancel()    stops future charges now; access runs to the end of the paid month
  daily()     ends plans at their period end, and reminds M-Pesa learners

Plan changes aren't built: Paystack has no swap, so a learner cancels and subscribes again.
No Stripe here. Add-ons, commitments and institutions still use it, behind their own flags.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta

from app.config import settings
from app.core.flags import is_ready
from app.services import payment_service as ps
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MONTHLY_MINOR = {"learner_tier": 3200, "tutor_tier": 7500, "intensive_tier": 14500}   # the pricing page
LIVE = ("active", "past_due", "cancelling")
CHANNELS = ("card", "mpesa")
REMIND_BEFORE = timedelta(days=3)
CARD_LEEWAY = timedelta(days=2)   # a renewal webhook can be late; don't call that a missed payment
PAYING_STATUS = {"active": "active", "past_due": "past_due", "cancelling": "cancelled", "ended": "cancelled"}


class BillingRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _one(rows) -> dict | None:
    return rows[0] if rows else None


def current(student_id: str) -> dict | None:
    return _one((_t("billing_subscription").select("*").eq("student_id", student_id).limit(1).execute()).data)


def _plan_code(tier: str) -> str:
    return {"learner_tier": settings.paystack_plan_learner, "tutor_tier": settings.paystack_plan_tutor,
            "intensive_tier": settings.paystack_plan_intensive}.get(tier, "")


def _price(tier: str, channel: str) -> tuple[int, str]:
    """(minor units, currency). M-Pesa is in shillings, set per tier in config."""
    if channel == "mpesa":
        kes = {"learner_tier": settings.mpesa_kes_learner, "tutor_tier": settings.mpesa_kes_tutor,
               "intensive_tier": settings.mpesa_kes_intensive}.get(tier, 0)
        return int(kes) * 100, "KES"
    return MONTHLY_MINOR[tier], "USD"


def _email_of(student_id: str) -> str:
    try:
        user = db.auth.admin.get_user_by_id(student_id).user
        return (user.email if user else "") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("billing: no email for %s: %s", student_id, exc)
        return ""


async def _mail(to: str | None, template: str, data: dict) -> None:
    """Best effort: a failed send never undoes a payment."""
    if not to:
        return
    from app.services import email_service
    try:
        await email_service.send_template_email(to, template, {"web_base_url": settings.web_base_url, **data},
                                                tags=["billing"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("billing mail %s failed: %s", template, exc)


def _check_channel(tier: str, channel: str) -> None:
    if tier not in MONTHLY_MINOR or channel not in CHANNELS:
        raise BillingRefused("bad_request")
    if not ps.configured():
        raise BillingRefused("not_configured")
    if channel == "mpesa":
        if not is_ready("mpesa_payments"):
            raise BillingRefused("not_enabled")
        if _price(tier, "mpesa")[0] <= 0:
            raise BillingRefused("not_configured")
    elif ps.provider() == "paystack" and not _plan_code(tier):
        raise BillingRefused("not_configured")


def _open_payment(student_id: str, tier: str, channel: str, purpose: str) -> dict:
    amount, currency = _price(tier, channel)
    payment_id = str(uuid.uuid4())
    row = {"id": payment_id, "student_id": student_id, "tier": tier, "channel": channel, "purpose": purpose,
           "amount_minor": amount, "currency": currency, "reference": f"mo-{payment_id}", "status": "pending"}
    return (_t("billing_payment").insert(row).execute()).data[0]


async def _open_checkout(payment: dict, email: str, *, plan: str | None) -> str:
    if ps.provider() == "demo":
        return f"{settings.api_base_url.rstrip('/')}/payments/demo/{payment['reference']}"
    base = settings.web_base_url.rstrip("/")
    body = {"email": email, "amount": str(int(payment["amount_minor"])), "currency": payment["currency"],
            "reference": payment["reference"], "callback_url": f"{base}/account/subscription",
            "metadata": json.dumps({"cancel_action": f"{base}/account/subscription"})}
    if plan:
        body["plan"] = plan
    else:
        body["channels"] = ["mobile_money"] if payment["channel"] == "mpesa" else ["card"]
    try:
        data = await ps.paystack_call("POST", "/transaction/initialize", body)
    except ps.PaymentFailed as exc:
        _t("billing_payment").update({"status": "failed"}).eq("id", payment["id"]).eq("status", "pending").execute()
        raise BillingRefused("payment_failed") from exc
    return data["authorization_url"]


async def checkout(student_id: str, tier: str, channel: str = "card") -> dict:
    """Start a plan. Refuses a live one, which would bill the learner twice."""
    _check_channel(tier, channel)
    existing = current(student_id)
    if existing and existing["status"] in LIVE:
        raise BillingRefused("already_subscribed")
    email = _email_of(student_id)
    if not email:
        raise BillingRefused("no_email")
    plan = _plan_code(tier) if channel == "card" and ps.provider() == "paystack" else None
    row = {"provider": ps.provider(), "channel": channel, "tier": tier, "status": "pending", "email": email.lower(),
           "plan_code": plan, "subscription_code": None, "email_token": None, "updated_at": _now().isoformat()}
    if existing:
        _t("billing_subscription").update(row).eq("student_id", student_id).execute()
    else:
        _t("billing_subscription").insert({"student_id": student_id, **row}).execute()
    payment = _open_payment(student_id, tier, channel, "first")
    return {"checkout_url": await _open_checkout(payment, email, plan=plan)}


async def pay_month(student_id: str, channel: str | None = None) -> dict:
    """Pay this month once: an M-Pesa learner's renewal, or anyone in grace."""
    sub = current(student_id)
    if not sub or sub["status"] not in ("active", "past_due"):
        raise BillingRefused("no_such_plan")
    channel = channel or sub["channel"]
    _check_channel(sub["tier"], channel)
    payment = _open_payment(student_id, sub["tier"], channel, "month")
    return {"checkout_url": await _open_checkout(payment, sub["email"], plan=None)}


async def settle_month(payment: dict, now: datetime | None = None) -> dict | None:
    """A month was paid. A repeat of the same payment does nothing."""
    from app.services.lesson_service import grant_paid_month
    from app.services.tier_service import change_tier
    now = now or _now()
    got = (_t("billing_payment").update({"status": "paid", "paid_at": now.isoformat()})
           .eq("id", payment["id"]).eq("status", "pending").execute()).data or []
    if not got:
        return None
    student_id, tier = payment["student_id"], payment["tier"]
    sub = current(student_id) or {}
    try:
        grant_paid_month(student_id, tier, payment["reference"])
        await change_tier(student_id, tier, "self")
        end = _ts(sub.get("current_period_end"))
        until = (max(end, now) if end else now) + relativedelta(months=1)
        status = "cancelling" if sub.get("status") == "cancelling" else "active"
        _t("billing_subscription").update({"status": status, "current_period_end": until.isoformat(),
                                           "updated_at": now.isoformat()}).eq("student_id", student_id).execute()
        _t("student").update({"subscription_status": PAYING_STATUS[status], "current_period_end": until.isoformat()}) \
            .eq("id", student_id).execute()
    except Exception:
        _t("billing_payment").update({"status": "pending", "paid_at": None}).eq("id", payment["id"]).execute()
        raise
    _after_payment(student_id, payment, now)
    if payment.get("purpose") == "first":
        await _mail(sub.get("email"), "subscription_receipt", {
            "tier": tier.replace("_tier", ""), "subject": "Your Tauka subscription is active",
            "current_period_end": until.date().isoformat()})
    return got[0]


def _after_payment(student_id: str, payment: dict, now: datetime) -> None:
    """Best effort: the month is paid and granted whatever happens here."""
    from app.services import grace_service
    from app.services.pause_service import resume_after_payment
    for label, step in (
        ("pause", lambda: resume_after_payment(student_id)),
        ("grace", lambda: grace_service.resolve(student_id, "paid", now)),
        ("ledger", lambda: _t("student_payment").insert({
            "student_id": student_id, "source": "paystack", "external_ref": payment["reference"],
            "amount_minor": int(payment["amount_minor"]), "currency": payment["currency"],
            "paid_at": now.isoformat()}).execute()),
    ):
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            logger.error("after payment of %s, %s failed: %s", payment.get("reference"), label, exc)


# ── Paystack's events ───────────────────────────────────────────────────────

def note_customer(reference: str, data: dict) -> None:
    """The first charge names the Paystack customer, which renewals are matched on."""
    code = (data.get("customer") or {}).get("customer_code")
    parts = ps.split_reference(reference)
    row = ps.order_row(*parts) if parts else None
    if code and row:
        _t("billing_subscription").update({"customer_code": code}).eq("student_id", row["student_id"]).execute()


async def renewal(data: dict, now: datetime | None = None) -> bool:
    """A subscription charge. Its reference is Paystack's, so the customer identifies it."""
    code = (data.get("customer") or {}).get("customer_code")
    plan = (data.get("plan") or {}).get("plan_code") if isinstance(data.get("plan"), dict) else None
    if not code or not plan:
        logger.info("charge %s is neither ours nor a subscription; ignored", data.get("reference"))
        return False
    sub = _one((_t("billing_subscription").select("*").eq("customer_code", code).limit(1).execute()).data)
    if not sub or sub["status"] not in LIVE:
        logger.error("renewal %s for customer %s, who has no live plan", data.get("reference"), code)
        return False
    amount, currency = int(data.get("amount") or 0), (data.get("currency") or "").upper()
    expected, want = _price(sub["tier"], "card")
    if amount != expected or currency != want:
        logger.error("renewal %s paid %s %s, owed %s %s: not granted", data.get("reference"), amount, currency, expected, want)
        await _mail(settings.admin_email, "payment_refund_ops", {
            "subject": f"Renewal didn't match the plan: {data.get('reference')}", "reference": data.get("reference"),
            "state": "amount mismatch, not granted", "outside": False})
        return False
    reference = data.get("reference")
    row = _one((_t("billing_payment").select("*").eq("reference", reference).limit(1).execute()).data)
    if not row:
        row = (_t("billing_payment").insert({
            "id": str(uuid.uuid4()), "student_id": sub["student_id"], "tier": sub["tier"], "channel": "card",
            "purpose": "renewal", "amount_minor": amount, "currency": currency, "reference": reference,
            "status": "pending"}).execute()).data[0]
    await settle_month(row, now)
    return True


async def handle_event(name: str, data: dict, now: datetime | None = None) -> bool:
    now = now or _now()
    if name == "subscription.create":
        customer = data.get("customer") or {}
        update = {"subscription_code": data.get("subscription_code"), "email_token": data.get("email_token"),
                  "customer_code": customer.get("customer_code"), "updated_at": now.isoformat()}
        got = (_t("billing_subscription").update(update).eq("customer_code", customer.get("customer_code") or "-")
               .execute()).data or []
        if not got and customer.get("email"):
            got = (_t("billing_subscription").update(update).eq("email", customer["email"].lower())
                   .in_("status", ["pending", "active"]).execute()).data or []
        if not got:
            logger.error("subscription %s matches no learner", data.get("subscription_code"))
        return bool(got)
    code = data.get("subscription_code") or (data.get("subscription") or {}).get("subscription_code")
    sub = _one((_t("billing_subscription").select("*").eq("subscription_code", code or "-").limit(1).execute()).data)
    if not sub:
        return False
    if name == "invoice.payment_failed":
        if sub["status"] != "cancelling":
            await _mark_past_due(sub, data.get("invoice_code"), now)
        return True
    if name in ("subscription.not_renew", "subscription.disable") and sub["status"] in ("active", "past_due"):
        _set_status(sub["student_id"], "cancelling", now)
        return True
    return False


def _set_status(student_id: str, status: str, now: datetime) -> None:
    _t("billing_subscription").update({"status": status, "updated_at": now.isoformat()}) \
        .eq("student_id", student_id).execute()
    _t("student").update({"subscription_status": PAYING_STATUS[status]}).eq("id", student_id).execute()


async def _mark_past_due(sub: dict, invoice_ref: str | None, now: datetime) -> None:
    from app.services import grace_service
    _set_status(sub["student_id"], "past_due", now)
    await grace_service.start(sub["student_id"], invoice_ref, now)


# ── the learner's choices ───────────────────────────────────────────────────

async def cancel(student_id: str, now: datetime | None = None) -> dict:
    """Stops future charges now. Access runs to the end of the paid month."""
    now = now or _now()
    sub = current(student_id)
    if not sub or sub["status"] not in ("active", "past_due"):
        raise BillingRefused("no_such_plan")
    if sub["provider"] == "paystack" and sub["channel"] == "card":
        if not (sub.get("subscription_code") and sub.get("email_token")):
            raise BillingRefused("not_ready")     # Paystack's subscription.create hasn't arrived
        try:
            await ps.paystack_call("POST", "/subscription/disable",
                                   {"code": sub["subscription_code"], "token": sub["email_token"]})
        except ps.PaymentFailed as exc:
            raise BillingRefused("payment_failed") from exc
    _set_status(student_id, "cancelling", now)
    return {"action": "cancelled", "message": "Your plan runs to the end of the month you've paid for.",
            "current_period_end": sub.get("current_period_end")}


async def reactivate(student_id: str, now: datetime | None = None) -> dict:
    """Undo a cancellation before the paid month ends."""
    now = now or _now()
    sub = current(student_id)
    if not sub or sub["status"] != "cancelling":
        raise BillingRefused("no_such_plan")
    if sub["provider"] == "paystack" and sub["channel"] == "card":
        try:
            await ps.paystack_call("POST", "/subscription/enable",
                                   {"code": sub["subscription_code"], "token": sub["email_token"]})
        except ps.PaymentFailed as exc:
            raise BillingRefused("payment_failed") from exc
    _set_status(student_id, "active", now)
    return {"status": "active", "next_billing": sub.get("current_period_end")}


async def manage_link(student_id: str) -> str:
    """Paystack's hosted page for changing a card."""
    sub = current(student_id)
    if not sub or sub["status"] not in LIVE or sub["channel"] != "card":
        raise BillingRefused("no_such_plan")
    if sub["provider"] == "demo":
        return f"{settings.web_base_url.rstrip('/')}/account/subscription"
    if not sub.get("subscription_code"):
        raise BillingRefused("not_ready")
    try:
        data = await ps.paystack_call("GET", f"/subscription/{sub['subscription_code']}/manage/link")
    except ps.PaymentFailed as exc:
        raise BillingRefused("payment_failed") from exc
    return data["link"]


def summary(student_id: str) -> dict | None:
    """For /account/subscription. Never includes the token."""
    sub = current(student_id)
    if not sub or sub["status"] not in LIVE:
        return None
    amount, currency = _price(sub["tier"], sub["channel"])
    return {"plan_name": sub["tier"].replace("_tier", "").title(), "status": sub["status"],
            "channel": sub["channel"], "amount_minor": amount, "currency": currency,
            "current_period_end": sub.get("current_period_end"), "cancel_at_period_end": sub["status"] == "cancelling",
            "can_pay_now": sub["status"] == "past_due" or (sub["channel"] == "mpesa" and sub["status"] == "active")}


def history(student_id: str, limit: int = 10) -> list[dict]:
    rows = (_t("billing_payment").select("id, tier, channel, amount_minor, currency, status, paid_at, created_at")
            .eq("student_id", student_id).eq("status", "paid").order("paid_at", desc=True).limit(limit).execute()).data or []
    return [{"id": r["id"], "date": r.get("paid_at"), "amount_minor": int(r["amount_minor"]), "currency": r["currency"],
             "description": f"{r['tier'].replace('_tier', '').title()} · one month", "channel": r["channel"],
             "status": r["status"]} for r in rows]


# ── the daily run ───────────────────────────────────────────────────────────

async def _end(sub: dict, now: datetime) -> None:
    """The paid month ran out on a cancelled plan."""
    from app.services import grace_service, makeup_service
    from app.services.pause_service import is_pausing
    from app.services.tier_service import change_tier
    student_id = sub["student_id"]
    _t("billing_subscription").update({"status": "ended", "subscription_code": None, "email_token": None,
                                       "updated_at": now.isoformat()}).eq("student_id", student_id).execute()
    _t("student").update({"subscription_status": "cancelled"}).eq("id", student_id).execute()
    await change_tier(student_id, "free_tier", "self")
    pausing = is_pausing(student_id)
    for label, step in (
        # §2e: unused credits go on a cancellation. A pause keeps them, and keeps its own path.
        ("credits", lambda: None if pausing else makeup_service.forfeit_on_cancellation(student_id)),
        ("grace", lambda: grace_service.resolve(student_id, "cancelled", now)),
        ("pod", lambda: None if pausing else grace_service.leave_pod_on_cancel(student_id, now)),
    ):
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            logger.error("ending the plan of %s, %s failed: %s", student_id, label, exc)


async def daily(now: datetime | None = None) -> dict:
    now = now or _now()
    out = {"ended": 0, "reminded": 0, "missed": 0}
    for sub in (_t("billing_subscription").select("*").in_("status", list(LIVE)).execute()).data or []:
        try:
            end = _ts(sub.get("current_period_end"))
            if not end:
                continue
            if sub["status"] == "cancelling":
                if end <= now:
                    await _end(sub, now)
                    out["ended"] += 1
            elif sub["status"] == "active":
                late = end + (timedelta(0) if sub["channel"] == "mpesa" else CARD_LEEWAY)
                if now > late:
                    await _mark_past_due(sub, None, now)
                    out["missed"] += 1
                elif sub["channel"] == "mpesa" and end - now <= REMIND_BEFORE \
                        and _ts(sub.get("reminded_for")) != end:
                    _t("billing_subscription").update({"reminded_for": end.isoformat()}) \
                        .eq("student_id", sub["student_id"]).execute()
                    amount, currency = _price(sub["tier"], "mpesa")
                    await _mail(sub["email"], "billing_month_due", {
                        "subject": "Your next Tauka month is due", "tier": sub["tier"].replace("_tier", "").title(),
                        "amount": f"{currency} {amount / 100:,.0f}", "due": end.date().isoformat()})
                    out["reminded"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("billing daily for %s: %s", sub.get("student_id"), exc)
    return out


# ── the local test checkout ─────────────────────────────────────────────────

async def demo_event(student_id: str, event: str) -> dict:
    """Stands in for Paystack's renewal and failure events. Demo provider only."""
    sub = current(student_id)
    if not sub or sub["status"] not in LIVE:
        raise BillingRefused("no_such_plan")
    if event == "fail":
        await _mark_past_due(sub, "demo", _now())
        return {"status": "past_due"}
    if event == "renew":
        payment = _open_payment(student_id, sub["tier"], sub["channel"], "renewal")
        await settle_month(payment)
        return {"status": "renewed", "reference": payment["reference"]}
    raise BillingRefused("bad_request")
