"""Payments through one provider interface (§145, §151, decision D7: Paystack).

Gift cards and sponsored seats call `checkout()` and never name a provider. A
payment is settled in exactly one place, `settle()`, whichever provider reports it.

PAYMENT_PROVIDER:
  paystack  real payments. Needs PAYSTACK_SECRET_KEY (a test key behaves the same). Refused
            on a local API_BASE_URL: Paystack keys, test ones included, are only tried on
            a deployed server, where the webhook can reach us. Use demo locally.
  demo      a local page with Pay and Cancel buttons, so the whole flow can be tried
            with no account. Refused unless API_BASE_URL is a local address, so a
            deploy that ships this setting takes no free orders.

The reference is `<kind>-<row id>`, so it names the row without any lookup table
and is the same on every retry. `settle()` checks the amount and currency against
the row: the row was priced by us, and only it decides what was owed.

Monthly plans share this settlement path (kind "mo"), and their rules are in billing_service.
"""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

PAYSTACK_INITIALIZE = "https://api.paystack.co/transaction/initialize"
PAYSTACK_REFUND = "https://api.paystack.co/refund"
PAYSTACK_API = "https://api.paystack.co"
SUBSCRIPTION_EVENTS = {"subscription.create", "subscription.not_renew", "subscription.disable", "invoice.payment_failed"}

# kind -> (table, where the buyer lands after paying, where they land on cancel)
KINDS = {
    "gc": ("gift_card", "/gift-card/thanks", "/gift-card"),
    "sp": ("sponsorship", "/gift/thanks", "/gift"),
    "mo": ("billing_payment", "/account/subscription", "/account/subscription"),   # a month of a plan (§151)
    "tk": ("ai_token_purchase", "/account/ai-tokens", "/account/ai-tokens"),       # an AI token pack (§154)
    "xp": ("exchange_plus_purchase", "/exchange?plus=done", "/exchange"),           # a month of Exchange+ (§159)
}


class PaymentFailed(Exception):
    """The provider refused or could not be reached. Nothing was charged."""


@dataclass(frozen=True)
class Checkout:
    url: str
    provider: str
    reference: str


def provider() -> str:
    return (settings.payment_provider or "").strip().lower()


def configured() -> bool:
    p = provider()
    if p == "paystack":
        return bool(settings.paystack_secret_key) and not settings._is_local(settings.api_base_url)
    if p == "demo":
        return settings._is_local(settings.api_base_url)
    return False


def _reference(kind: str, row_id: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"Unknown payment kind {kind!r}")
    return f"{kind}-{row_id}"


def split_reference(reference: str) -> tuple[str, str] | None:
    kind, _, row_id = (reference or "").partition("-")
    return (kind, row_id) if kind in KINDS and row_id else None


def order_row(kind: str, row_id: str) -> dict | None:
    rows = (db.schema("app").table(KINDS[kind][0]).select("*").eq("id", row_id).limit(1).execute()).data or []
    return rows[0] if rows else None


async def checkout(kind: str, row_id: str, amount_minor: int, currency: str, email: str, description: str) -> Checkout:
    """Open a payment. Raises PaymentFailed if the provider won't take it."""
    reference = _reference(kind, row_id)
    p = provider()
    if p == "demo":
        if not configured():
            raise PaymentFailed("The demo provider only runs on a local address.")
        return Checkout(f"{settings.api_base_url.rstrip('/')}/payments/demo/{reference}", p, reference)
    if p != "paystack" or not configured():
        raise PaymentFailed(f"Payment provider {p!r} is not configured.")

    _, success, cancel = KINDS[kind]
    base = settings.web_base_url.rstrip("/")
    body = {
        "email": email,
        "amount": str(int(amount_minor)),
        "currency": currency.upper(),
        "reference": reference,
        "callback_url": f"{base}{success}",
        # Paystack takes metadata as a JSON string. cancel_action is its "back" link.
        "metadata": json.dumps({"description": description, "cancel_action": f"{base}{cancel}"}),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(PAYSTACK_INITIALIZE, json=body,
                                    headers={"Authorization": f"Bearer {settings.paystack_secret_key}"})
        data = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Paystack initialize failed for %s: %s", reference, exc)
        raise PaymentFailed("Could not reach Paystack.") from exc
    if res.status_code != 200 or not data.get("status"):
        logger.error("Paystack refused %s: %s", reference, data.get("message"))
        raise PaymentFailed(data.get("message") or "Paystack refused the payment.")
    return Checkout(data["data"]["authorization_url"], p, reference)


async def settle(reference: str, amount_minor: int, currency: str) -> bool:
    """A provider says this was paid. Grants once; a repeat is a no-op.

    Returns False for a reference that isn't ours or a payment that doesn't match
    the row, which are logged and grant nothing. Raises if granting fails, so the
    provider retries.
    """
    parts = split_reference(reference)
    if not parts:
        logger.info("Payment %s is not a one-off we sold; ignored", reference)
        return False
    kind, row_id = parts
    row = order_row(kind, row_id)
    if not row:
        logger.error("Payment %s names no row", reference)
        return False
    if int(amount_minor) != int(row["amount_minor"]) or (currency or "").upper() != row["currency"].upper():
        logger.error("Payment %s paid %s %s, owed %s %s: not granted", reference, amount_minor, currency,
                     row["amount_minor"], row["currency"])
        return False
    if kind == "gc":
        from app.services.commerce_service import card_paid
        await card_paid(row_id)
    elif kind == "mo":
        from app.services.billing_service import settle_month
        await settle_month(row)
    elif kind == "tk":
        from app.services.ai_token_service import paid as tokens_paid
        await tokens_paid(row_id)
    elif kind == "xp":
        from app.services.exchange_member_service import plus_paid
        await plus_paid(row_id)
    else:
        from app.services.sponsorship_service import paid
        await paid(row_id)
    return True


async def provider_refund(provider_name: str, reference: str, reason: str) -> None:
    """Ask the provider that took this payment for all of it back."""
    if provider_name == "demo":
        return
    if provider_name != "paystack" or not settings.paystack_secret_key:
        raise PaymentFailed(f"Payment provider {provider_name!r} is not configured.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(PAYSTACK_REFUND, json={"transaction": reference, "merchant_note": reason},
                                    headers={"Authorization": f"Bearer {settings.paystack_secret_key}"})
        data = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Paystack refund failed for %s: %s", reference, exc)
        raise PaymentFailed("Could not reach Paystack.") from exc
    if res.status_code not in (200, 201) or not data.get("status"):
        logger.error("Paystack refused the refund of %s: %s", reference, data.get("message"))
        raise PaymentFailed(data.get("message") or "Paystack refused the refund.")


async def paystack_call(method: str, path: str, body: dict | None = None) -> dict:
    """One Paystack API call. Returns its `data`; raises PaymentFailed if refused or unreachable."""
    if not settings.paystack_secret_key:
        raise PaymentFailed("Paystack is not configured.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.request(method, f"{PAYSTACK_API}{path}", json=body,
                                       headers={"Authorization": f"Bearer {settings.paystack_secret_key}"})
        payload = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Paystack %s %s failed: %s", method, path, exc)
        raise PaymentFailed("Could not reach Paystack.") from exc
    if res.status_code not in (200, 201) or not payload.get("status"):
        logger.error("Paystack refused %s %s: %s", method, path, payload.get("message"))
        raise PaymentFailed(payload.get("message") or "Paystack refused the request.")
    return payload.get("data") or {}


def paystack_signature_ok(payload: bytes, signature: str) -> bool:
    """Paystack signs the raw body: HMAC-SHA512 with the secret key, hex digest."""
    if not settings.paystack_secret_key or not signature:
        return False
    digest = hmac.new(settings.paystack_secret_key.encode(), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, signature)


def _minor(amount) -> int | None:
    """Paystack sends a refund's amount as a string of minor units. None if it's missing or unreadable."""
    try:
        return int(amount) or None
    except (TypeError, ValueError):
        return None


async def handle_paystack_event(event: dict) -> bool:
    """A successful charge grants, and a refund event updates the order. Everything
    else is acknowledged."""
    name, data = event.get("event") or "", event.get("data") or {}
    if name.startswith("refund."):
        from app.services.refund_service import provider_update
        return await provider_update(data.get("transaction_reference") or "", name, amount_minor=_minor(data.get("amount")))
    if name in SUBSCRIPTION_EVENTS:
        from app.services.billing_service import handle_event
        return await handle_event(name, data)
    if name != "charge.success":
        return False
    if data.get("status") != "success":
        return False
    reference = data.get("reference") or ""
    parts = split_reference(reference)
    if not parts:
        # Not a reference we made, so a subscription charge if it's anything.
        from app.services.billing_service import renewal
        return await renewal(data)
    if parts[0] == "mo":
        from app.services.billing_service import note_customer
        note_customer(reference, data)
    return await settle(reference, int(data.get("amount") or 0), data.get("currency") or "")


def demo_order(reference: str) -> dict | None:
    """What the demo checkout page shows. None unless the demo provider is on."""
    if provider() != "demo" or not configured():
        return None
    parts = split_reference(reference)
    row = order_row(*parts) if parts else None
    if not row:
        return None
    _, success, cancel = KINDS[parts[0]]
    base = settings.web_base_url.rstrip("/")
    what = (f"{row['tokens']:,} AI tokens" if parts[0] == "tk"
            else f"{row.get('months', 1)} month(s) of {row['tier'].replace('_tier', '').title()}")
    return {"amount_minor": int(row["amount_minor"]), "currency": row["currency"], "status": row["status"],
            "what": what, "success_url": f"{base}{success}", "cancel_url": f"{base}{cancel}"}
