"""AI token packs (§154, plan ALL-18). Behind the `ai_tokens` flag.

A learner who reaches their AI allowance can spend tokens they bought. Bought tokens
never expire, and they're only used once the allowance has run out.

SPENDING  /ai/generate holds the estimated cost before calling the provider, returns it
          all if the call fails, and settles the difference with the real count after.
BUYING    checkout() opens a Paystack payment. payment_service.settle() checks the amount
          and calls paid(). confirm() asks Paystack directly when the buyer returns, which
          covers a late webhook and a local server Paystack can't reach.
REFUNDS   refund_service, always the full amount. Anything bought is removed first.

The ledger is append-only and a balance is a fold (app.ai_token_balance). The price is
here because the row is priced by us: settle() refuses a payment that doesn't match it.
"""

import logging
import uuid

from app.core.flags import is_ready
from app.services import payment_service as ps
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

PACK_TOKENS = 100_000
PACK_MINOR = 300
PACK_CURRENCY = "USD"


class TokensRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _email_of(student_id: str) -> str:
    try:
        user = db.auth.admin.get_user_by_id(student_id).user
        return (user.email if user else "") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai tokens: no email for %s: %s", student_id, exc)
        return ""


def balance(student_id: str) -> int:
    """Tokens left. 0 on a read error: nobody is charged, and the allowance still works."""
    try:
        data = db.schema("app").rpc("ai_token_balance", {"p_student_id": student_id}).execute().data
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai tokens: balance read failed for %s: %s", student_id, exc)
        return 0
    return data if isinstance(data, int) and not isinstance(data, bool) else 0


def hold(student_id: str, tokens: int) -> bool:
    """Take tokens for a call about to be made. False if they aren't all there."""
    try:
        data = db.schema("app").rpc("spend_ai_tokens", {"p_student_id": student_id, "p_tokens": int(tokens)}).execute().data
    except Exception as exc:  # noqa: BLE001
        logger.error("ai tokens: hold failed for %s: %s", student_id, exc)
        return False
    return data is True


def adjust(student_id: str, tokens: int, reason: str) -> None:
    """Write one correction: positive gives tokens back, negative takes more."""
    if tokens:
        _t("ai_token_ledger").insert({"student_id": student_id, "delta": int(tokens), "reason": reason}).execute()


def give_back(student_id: str, tokens: int) -> None:
    """A held call failed, so nothing was spent."""
    try:
        adjust(student_id, tokens, "return")
    except Exception as exc:  # noqa: BLE001
        logger.error("ai tokens: could not return %s tokens to %s: %s", tokens, student_id, exc)


async def settle_call(student_id: str, held: int, actual: int) -> None:
    """Replace the estimate with the real count. A long reply can take slightly more than was held."""
    difference = int(held) - int(actual)
    adjust(student_id, difference, "return" if difference > 0 else "spend")


def state(student_id: str) -> dict:
    """What /account/ai-tokens shows."""
    return {"on_sale": is_ready("ai_tokens"), "balance": balance(student_id), "pack_tokens": PACK_TOKENS,
            "amount_minor": PACK_MINOR, "currency": PACK_CURRENCY}


async def checkout(student_id: str) -> dict:
    if not is_ready("ai_tokens"):
        raise TokensRefused("not_enabled")
    if not ps.configured():
        raise TokensRefused("not_configured")
    # app.student holds learners only, and the order's foreign key points at it.
    if not (_t("student").select("id").eq("id", student_id).limit(1).execute()).data:
        raise TokensRefused("learners_only")
    email = _email_of(student_id)
    if not email:
        raise TokensRefused("no_email")
    purchase_id = str(uuid.uuid4())
    _t("ai_token_purchase").insert({
        "id": purchase_id, "student_id": student_id, "tokens": PACK_TOKENS, "amount_minor": PACK_MINOR,
        "currency": PACK_CURRENCY, "email": email, "status": "pending",
    }).execute()
    try:
        pay = await ps.checkout("tk", purchase_id, PACK_MINOR, PACK_CURRENCY, email, f"{PACK_TOKENS:,} Tauka AI tokens")
    except ps.PaymentFailed as exc:
        _t("ai_token_purchase").update({"status": "failed"}).eq("id", purchase_id).execute()
        raise TokensRefused("payment_failed") from exc
    _t("ai_token_purchase").update({"payment_provider": pay.provider, "payment_reference": pay.reference}) \
        .eq("id", purchase_id).execute()
    return {"checkout_url": pay.url, "reference": pay.reference}


async def paid(purchase_id: str) -> None:
    """Called by payment_service.settle(). A repeat grants nothing. Raises if the grant fails, so Paystack retries."""
    db.schema("app").rpc("settle_ai_token_purchase", {"p_purchase_id": purchase_id}).execute()


async def confirm(student_id: str, reference: str) -> dict:
    """The buyer came back from checkout: ask Paystack what happened, and grant if it was paid."""
    parts = ps.split_reference(reference)
    if not parts or parts[0] != "tk":
        raise TokensRefused("no_such_order")
    try:
        uuid.UUID(parts[1])
    except ValueError as exc:
        raise TokensRefused("no_such_order") from exc
    row = ps.order_row("tk", parts[1])
    if not row or row["student_id"] != student_id:
        raise TokensRefused("no_such_order")
    if row["status"] == "pending" and row.get("payment_provider") == "paystack":
        try:
            data = await ps.paystack_call("GET", f"/transaction/verify/{row['payment_reference']}")
        except ps.PaymentFailed as exc:
            raise TokensRefused("payment_failed") from exc
        if data.get("status") == "success":
            await ps.settle(reference, int(data.get("amount") or 0), data.get("currency") or "")
        row = ps.order_row("tk", parts[1]) or row
    return {"status": row["status"], "tokens": row["tokens"], "balance": balance(student_id)}


def history(student_id: str, limit: int = 10) -> list[dict]:
    rows = (_t("ai_token_purchase").select("id, tokens, amount_minor, currency, paid_at")
            .eq("student_id", student_id).eq("status", "paid").order("paid_at", desc=True).limit(limit).execute()).data or []
    return [{"id": r["id"], "date": r["paid_at"], "description": f"{r['tokens']:,} AI tokens",
             "amount_minor": r["amount_minor"], "currency": r["currency"], "status": "paid"} for r in rows]
