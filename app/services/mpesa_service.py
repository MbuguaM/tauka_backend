"""M-Pesa as a second payment method — enrolment plan 2.4, §2d, migration §130.

Stripe stays the one ledger. An M-Pesa learner has a Stripe subscription with
`collection_method = send_invoice`; each invoice is paid by a Daraja STK push
("Lipa na M-Pesa Online") to their phone:

    subscribe(tier, phone)  -> Stripe customer + send_invoice subscription,
                               STK push for the first invoice
    pay_open_invoice()      -> STK push for the learner's open invoice
    callback(body)          -> NOT trusted: the request is re-checked with an
                               STK query, then the invoice is paid out of band
    on_invoice_created()    -> a renewal invoice prompts automatically
                               (in place of Ratiba standing orders, which need
                               Safaricom's approval)

First month: Stripe's `subscription_create` invoice.paid is skipped by the
webhook (it assumes a card checkout did the activation), so `_confirm` activates
the plan itself: student ids, tier, month 1's lessons. Renewals then take the
ordinary invoice.paid path (lessons, grace resolved).

Configuration (env): MPESA_ENV (sandbox|production), MPESA_CONSUMER_KEY,
MPESA_CONSUMER_SECRET, MPESA_SHORTCODE, MPESA_PASSKEY, MPESA_CALLBACK_TOKEN,
MPESA_KES_LEARNER / _TUTOR / _INTENSIVE (whole shillings). Without them every
call refuses with `not_configured`.
"""

import base64
import logging
import re
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

BASE = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
TIERS = ("learner_tier", "tutor_tier", "intensive_tier")
_PHONE = re.compile(r"^(?:\+?254|0)?(7\d{8}|1\d{8})$")


class MpesaRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> bool:
    return all([settings.mpesa_consumer_key, settings.mpesa_consumer_secret, settings.mpesa_shortcode,
                settings.mpesa_passkey, settings.mpesa_callback_token])


def normalise_phone(raw: str) -> str:
    """'0712 345 678' / '+254712345678' -> '254712345678' (Safaricom MSISDN)."""
    digits = re.sub(r"[\s\-()]", "", raw or "")
    m = _PHONE.match(digits)
    if not m:
        raise MpesaRefused("bad_phone")
    return "254" + m.group(1)


def price_kes(tier: str) -> int:
    return int({"learner_tier": settings.mpesa_kes_learner, "tutor_tier": settings.mpesa_kes_tutor,
                "intensive_tier": settings.mpesa_kes_intensive}.get(tier) or 0)


# ── the phone, encrypted ────────────────────────────────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    if not settings.encryption_key:
        raise MpesaRefused("not_configured")
    k = settings.encryption_key
    return Fernet(k.encode() if isinstance(k, str) else k)


def save_payer(student_id: str, msisdn: str) -> None:
    _t("mpesa_payer").upsert({"student_id": student_id, "phone_encrypted": _fernet().encrypt(msisdn.encode()).decode(),
                              "phone_last4": msisdn[-4:], "updated_at": _now().isoformat()}).execute()


def payer_phone(student_id: str) -> str | None:
    rows = (_t("mpesa_payer").select("phone_encrypted").eq("student_id", student_id).limit(1).execute()).data or []
    if not rows:
        return None
    return _fernet().decrypt(rows[0]["phone_encrypted"].encode()).decode()


# ── Daraja ──────────────────────────────────────────────────────────────────

def _base() -> str:
    return BASE.get(settings.mpesa_env or "sandbox", BASE["sandbox"])


async def _token(client: httpx.AsyncClient) -> str:
    r = await client.get(f"{_base()}/oauth/v1/generate?grant_type=client_credentials",
                         auth=(settings.mpesa_consumer_key, settings.mpesa_consumer_secret))
    r.raise_for_status()
    return r.json()["access_token"]


def _password(stamp: str) -> str:
    return base64.b64encode(f"{settings.mpesa_shortcode}{settings.mpesa_passkey}{stamp}".encode()).decode()


async def stk_push(msisdn: str, amount_kes: int, reference: str, description: str) -> dict:
    stamp = _now().strftime("%Y%m%d%H%M%S")
    body = {
        "BusinessShortCode": settings.mpesa_shortcode, "Password": _password(stamp), "Timestamp": stamp,
        "TransactionType": "CustomerPayBillOnline", "Amount": int(amount_kes), "PartyA": msisdn,
        "PartyB": settings.mpesa_shortcode, "PhoneNumber": msisdn,
        "CallBackURL": f"{settings.api_base_url}/mpesa/callback/{settings.mpesa_callback_token}",
        "AccountReference": reference[:12], "TransactionDesc": description[:13],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        tok = await _token(client)
        r = await client.post(f"{_base()}/mpesa/stkpush/v1/processrequest", json=body,
                              headers={"Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        return r.json()


async def stk_query(checkout_request_id: str) -> dict:
    stamp = _now().strftime("%Y%m%d%H%M%S")
    body = {"BusinessShortCode": settings.mpesa_shortcode, "Password": _password(stamp), "Timestamp": stamp,
            "CheckoutRequestID": checkout_request_id}
    async with httpx.AsyncClient(timeout=30) as client:
        tok = await _token(client)
        r = await client.post(f"{_base()}/mpesa/stkpushquery/v1/query", json=body,
                              headers={"Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        return r.json()


# ── the learner's side ──────────────────────────────────────────────────────

async def _prompt(student_id: str, msisdn: str, amount: int, purpose: str, invoice_id: str | None,
                  tier: str | None) -> dict:
    row = (_t("mpesa_payment").insert({"student_id": student_id, "stripe_invoice_id": invoice_id, "purpose": purpose,
                                       "tier": tier, "amount_kes": amount, "status": "pending"}).execute()).data[0]
    try:
        res = await stk_push(msisdn, amount, "TAUKA", "Tauka plan")
    except Exception as exc:  # noqa: BLE001
        logger.error("STK push failed for %s: %s", student_id, exc)
        _t("mpesa_payment").update({"status": "failed", "completed_at": _now().isoformat()}) \
            .eq("id", row["id"]).execute()
        raise MpesaRefused("push_failed") from exc
    _t("mpesa_payment").update({"checkout_request_id": res.get("CheckoutRequestID"),
                                "merchant_request_id": res.get("MerchantRequestID")}).eq("id", row["id"]).execute()
    return {"payment_id": row["id"], "status": "pending", "amount_kes": amount}


async def subscribe(student_id: str, tier: str, phone: str, email: str | None) -> dict:
    """Start an M-Pesa plan: a send_invoice subscription, and a prompt for month 1."""
    import stripe
    if not configured():
        raise MpesaRefused("not_configured")
    if tier not in TIERS or not price_kes(tier):
        raise MpesaRefused("bad_tier")
    msisdn = normalise_phone(phone)
    st = (_t("student").select("stripe_subscription_id, subscription_status, stripe_customer_id")
          .eq("id", student_id).limit(1).execute()).data or [{}]
    if st[0].get("stripe_subscription_id") and st[0].get("subscription_status") in ("active", "trialing", "past_due"):
        raise MpesaRefused("already_subscribed")
    from app.services.stripe_service import _PRICE_MAP
    customer = st[0].get("stripe_customer_id") or stripe.Customer.create(
        email=email, metadata={"student_id": student_id}).id
    sub = stripe.Subscription.create(customer=customer, items=[{"price": _PRICE_MAP[tier]}],
                                     collection_method="send_invoice", days_until_due=7,
                                     metadata={"student_id": student_id, "tier": tier, "rail": "mpesa"})
    save_payer(student_id, msisdn)
    return await _prompt(student_id, msisdn, price_kes(tier), "first_month", sub.latest_invoice, tier)


async def pay_open_invoice(student_id: str, phone: str | None = None) -> dict:
    """Prompt for the learner's open invoice (a renewal, or a missed payment)."""
    import stripe
    if not configured():
        raise MpesaRefused("not_configured")
    msisdn = normalise_phone(phone) if phone else payer_phone(student_id)
    if not msisdn:
        raise MpesaRefused("no_phone")
    st = (_t("student").select("stripe_customer_id, tier").eq("id", student_id).limit(1).execute()).data or [{}]
    if not st[0].get("stripe_customer_id"):
        raise MpesaRefused("nothing_to_pay")
    open_ = stripe.Invoice.list(customer=st[0]["stripe_customer_id"], status="open", limit=1).data
    if not open_:
        raise MpesaRefused("nothing_to_pay")
    if phone:
        save_payer(student_id, msisdn)
    return await _prompt(student_id, msisdn, price_kes(st[0].get("tier")) or 1, "invoice", open_[0].id,
                         st[0].get("tier"))


async def status(student_id: str, payment_id: str) -> dict:
    """The learner polls after the prompt; a pending one is re-checked with
    Daraja, so a lost callback doesn't strand a payment."""
    def read():
        rows = (_t("mpesa_payment").select("id, status, amount_kes, purpose, completed_at, checkout_request_id")
                .eq("id", payment_id).eq("student_id", student_id).limit(1).execute()).data or []
        if not rows:
            raise MpesaRefused("no_such_payment")
        return rows[0]
    row = read()
    if row["status"] == "pending" and row.get("checkout_request_id") and configured():
        try:
            await confirm(row["checkout_request_id"])
            row = read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("M-Pesa status check failed: %s", exc)
    row.pop("checkout_request_id", None)
    return row


# ── Daraja's callback, and confirmation ─────────────────────────────────────

async def callback(token: str, body: dict) -> dict:
    """Never trusted on its own: the CheckoutRequestID is re-checked by query."""
    if not settings.mpesa_callback_token or token != settings.mpesa_callback_token:
        raise MpesaRefused("bad_token")
    cb = ((body or {}).get("Body") or {}).get("stkCallback") or {}
    cid = cb.get("CheckoutRequestID")
    if not cid:
        return {"ok": False}
    items = {i.get("Name"): i.get("Value") for i in ((cb.get("CallbackMetadata") or {}).get("Item") or [])}
    return await confirm(cid, receipt=items.get("MpesaReceiptNumber"))


async def confirm(checkout_request_id: str, receipt: str | None = None) -> dict:
    rows = (_t("mpesa_payment").select("*").eq("checkout_request_id", checkout_request_id).limit(1)
            .execute()).data or []
    if not rows or rows[0]["status"] != "pending":
        return {"ok": True, "status": rows[0]["status"] if rows else "unknown"}
    p = rows[0]
    q = await stk_query(checkout_request_id)
    code = q.get("ResultCode")
    if code is None:
        return {"ok": True, "status": "pending"}
    code = int(code)
    if code != 0:
        _t("mpesa_payment").update({"status": "cancelled" if code == 1032 else "failed", "result_code": code,
                                    "completed_at": _now().isoformat()}).eq("id", p["id"]).eq("status", "pending") \
            .execute()
        return {"ok": True, "status": "failed"}
    got = (_t("mpesa_payment").update({"status": "paid", "result_code": 0, "completed_at": _now().isoformat(),
                                       "mpesa_receipt": receipt})
           .eq("id", p["id"]).eq("status", "pending").execute()).data or []
    if got:
        try:
            # Plan 8.5: recorded in shillings; the Stripe invoice is out of band.
            _t("student_payment").insert({"student_id": p["student_id"], "source": "mpesa",
                                          "external_ref": receipt or checkout_request_id,
                                          "amount_minor": int(p["amount_kes"]) * 100, "currency": "KES",
                                          "paid_at": _now().isoformat()}).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("M-Pesa payment record failed: %s", exc)
        await _settle(p)
    return {"ok": True, "status": "paid"}


async def _settle(p: dict) -> None:
    """Pay the Stripe invoice out of band; for month 1, activate the plan."""
    import stripe
    if p.get("stripe_invoice_id"):
        stripe.Invoice.pay(p["stripe_invoice_id"], paid_out_of_band=True)
    if p["purpose"] == "first_month":
        inv = stripe.Invoice.retrieve(p["stripe_invoice_id"]) if p.get("stripe_invoice_id") else None
        from app.services.lesson_service import grant_paid_month
        from app.services.tier_service import change_tier
        _t("student").update({"stripe_customer_id": getattr(inv, "customer", None),
                              "stripe_subscription_id": getattr(inv, "subscription", None),
                              "subscription_status": "active"}).eq("id", p["student_id"]).execute()
        await change_tier(p["student_id"], p["tier"], "self")
        grant_paid_month(p["student_id"], p["tier"], p.get("stripe_invoice_id") or p["id"])


async def on_invoice_overdue(invoice: dict) -> None:
    """A send_invoice invoice past due is a missed payment (plan 2.3a): open the
    grace period, and prompt again."""
    rows = (_t("student").select("id").eq("stripe_customer_id", invoice.get("customer")).limit(1)
            .execute()).data or []
    if not rows:
        return
    from app.services import grace_service
    await grace_service.start(rows[0]["id"], invoice.get("id"))
    await on_invoice_created({**invoice, "billing_reason": "overdue"})


async def on_invoice_created(invoice: dict) -> None:
    """A renewal invoice for an M-Pesa learner prompts at once (the stand-in for
    Ratiba standing orders). Best-effort: the grace emails follow if unpaid."""
    if invoice.get("collection_method") != "send_invoice" or not configured():
        return
    if (invoice.get("billing_reason") or "") == "subscription_create":
        return          # month 1 is prompted by subscribe()
    rows = (_t("student").select("id, tier").eq("stripe_customer_id", invoice.get("customer")).limit(1)
            .execute()).data or []
    if not rows:
        return
    payer = (_t("mpesa_payer").select("auto_prompt").eq("student_id", rows[0]["id"]).limit(1).execute()).data or []
    if not payer or payer[0].get("auto_prompt") is False:
        return
    try:
        await _prompt(rows[0]["id"], payer_phone(rows[0]["id"]), price_kes(rows[0].get("tier")) or 1, "invoice",
                      invoice.get("id"), rows[0].get("tier"))
    except Exception as exc:  # noqa: BLE001
        logger.error("auto M-Pesa prompt for %s failed: %s", rows[0]["id"], exc)
