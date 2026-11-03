"""Refunds on gift cards and sponsored seats (§146). Finance or ops only.

THE RULE, matching the supporter gift's (gift_service.request_refund):
  unused (a paid card, an unclaimed seat)   refundable, and the code or link dies at once
  in use, within 7 days of redeem or claim  refundable if ops confirm taking the access away
  in use, after 7 days                      refused

Always the full amount: a partial refund would leave a seat half paid for, with no
rule for what it then buys.

status moves to 'refunded' BEFORE the provider is called, so nobody can redeem a
code while its money is on the way back. If the provider refuses, the status goes
back. refund_state then follows the provider, which can take days and can fail.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import hierarchy_service as hs
from app.services import payment_service as ps
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
PAYING = ("active", "trialing", "past_due")
# kind -> (the status once used, who has it, when they got it, who paid)
USE = {
    "gc": ("redeemed", "redeemed_by", "redeemed_at", "purchaser_email"),
    "sp": ("claimed", "recipient_id", "claimed_at", "sponsor_email"),
}
_ORDER_FIELDS = ("id, status, tier, months, amount_minor, currency, payment_provider, payment_reference, created_at, "
                 "refund_state, refund_reason, refund_requested_at, refund_processed_at")
_STATE = {"refund.processed": "processed", "refund.failed": "failed", "refund.needs-attention": "needs_attention"}


class RefundRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _allowed(actor: str) -> None:
    if not (hs.admin_can(actor, "finance") or hs.admin_can(actor, "ops")):
        raise RefundRefused("not_allowed")


async def _mail(to: str | None, template: str, data: dict) -> None:
    """Best-effort: the money has already moved, and a failed send must not hide that."""
    if not to:
        return
    from app.services import email_service
    try:
        await email_service.send_template_email(to, template, {"web_base_url": settings.web_base_url, **data},
                                                tags=["refund"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("refund mail %s failed: %s", template, exc)


def orders(actor: str, limit: int = 200) -> list[dict]:
    """Every paid or refunded one-off, newest first. Unpaid checkouts aren't orders."""
    _allowed(actor)
    out = []
    for kind, (table, _, _) in ps.KINDS.items():
        used, _who, when, payer = USE[kind]
        rows = (_t(table).select(f"{_ORDER_FIELDS}, {when}, {payer}")
                .in_("status", ["paid", used, "refunded", "expired"]).order("created_at", desc=True).limit(limit)
                .execute()).data or []
        for r in rows:
            used_at, payer_email = r.pop(when, None), r.pop(payer, None)
            out.append({**r, "kind": kind, "payer_email": payer_email, "used_at": used_at,
                        "in_use": r["status"] == used,
                        "refundable": _refundable(kind, r["status"], used_at, r.get("refund_state"), r.get("payment_provider"))})
    return sorted(out, key=lambda r: str(r["created_at"]), reverse=True)[:limit]


def _refundable(kind: str, status: str, used_at, refund_state, provider, now: datetime | None = None) -> bool:
    if not provider:
        return False
    if status == "paid":
        return True
    if status == "refunded":
        return refund_state == "failed"      # try again
    return status == USE[kind][0] and bool(used_at) and _ts(used_at) + WINDOW >= (now or _now())


async def _take_back(kind: str, row: dict) -> str | None:
    """Ends the access a used card or seat gave. Returns the learner's id."""
    from app.services.lesson_service import revoke_gift_months
    from app.services.tier_service import change_tier
    student_id = row[USE[kind][1]]
    if not student_id:
        return None
    revoke_gift_months(student_id, f"{kind}:{row['id']}")
    st = (_t("student").select("subscription_status").eq("id", student_id).limit(1).execute()).data or [{}]
    if st[0].get("subscription_status") not in PAYING:
        await change_tier(student_id, "free_tier", "self")
    return student_id


async def refund(actor: str, reference: str, reason: str, revoke_access: bool = False,
                 now: datetime | None = None) -> dict:
    _allowed(actor)
    now = now or _now()
    reason = (reason or "").strip()[:300]
    if len(reason) < 3:
        raise RefundRefused("reason_required")
    parts = ps.split_reference(reference)
    row = ps.order_row(*parts) if parts else None
    if not row:
        raise RefundRefused("no_such_order")
    kind, row_id = parts
    table = ps.KINDS[kind][0]
    used, _who, when, payer = USE[kind]
    status = row["status"]
    if not row.get("payment_provider"):
        raise RefundRefused("not_refundable")     # paid before §145, or never reached checkout
    retry = status == "refunded" and row.get("refund_state") == "failed"
    in_use = status == used
    if in_use:
        if not revoke_access:
            raise RefundRefused("in_use")
        if not row.get(when) or _ts(row[when]) + WINDOW < now:
            raise RefundRefused("window_passed")
    elif status != "paid" and not retry:
        raise RefundRefused("not_refundable")

    claim = {"status": "refunded", "refund_state": "requested", "refund_reason": reason, "refunded_by": actor,
             "refund_requested_at": now.isoformat()}
    got = (_t(table).update(claim).eq("id", row_id).eq("status", status).execute()).data or []
    if not got:
        raise RefundRefused("not_refundable")     # someone redeemed or refunded it just now
    try:
        await ps.provider_refund(row["payment_provider"], reference, reason)
    except ps.PaymentFailed as exc:
        back = {"status": status, "refund_state": "failed" if retry else None, "refund_reason": row.get("refund_reason"),
                "refunded_by": row.get("refunded_by"), "refund_requested_at": row.get("refund_requested_at")}
        _t(table).update(back).eq("id", row_id).execute()
        raise RefundRefused("payment_failed") from exc

    state = "requested"
    if row["payment_provider"] == "demo":     # nothing is coming back to report it
        state = "processed"
        _t(table).update({"refund_state": state, "refund_processed_at": now.isoformat()}).eq("id", row_id).execute()

    plan = {"months": row["months"], "tier": row["tier"].replace("_tier", "").title()}
    if in_use:
        student_id = await _take_back(kind, row)
        if kind == "sp" and row.get("request_id"):
            from app.services.sponsorship_service import close_refunded_request
            close_refunded_request(row["request_id"], now)
        if student_id:
            from app.services.sponsorship_service import _email_of
            await _mail(_email_of(student_id), "seat_refunded", {"subject": "Your gifted months have ended", **plan})
    if not retry:
        await _mail(row.get(payer), "payment_refunded", {
            "subject": "Your Tauka refund", "amount": f"{row['currency']} {int(row['amount_minor']) / 100:,.2f}", **plan})
    return {"reference": reference, "status": "refunded", "refund_state": state, "access_taken_back": in_use}


async def provider_update(reference: str, event: str, now: datetime | None = None) -> bool:
    """A provider's refund.* event. Anything unrecognised is ignored, not an error."""
    state = _STATE.get(event)
    parts = ps.split_reference(reference)
    if not state or not parts:
        return False
    row = ps.order_row(*parts)
    if not row:
        return False
    now = now or _now()
    if row["status"] != "refunded":
        # Refunded from the provider's dashboard: the code or seat is still live here.
        logger.error("Refund event %s for %s, which Tauka hasn't refunded", event, reference)
        await _mail(settings.admin_email, "payment_refund_ops", {
            "subject": f"Refund made outside Tauka: {reference}", "reference": reference, "outside": True})
        return False
    update = {"refund_state": state}
    if state == "processed":
        update["refund_processed_at"] = now.isoformat()
    _t(ps.KINDS[parts[0]][0]).update(update).eq("id", parts[1]).execute()
    if state != "processed":
        await _mail(settings.admin_email, "payment_refund_ops", {
            "subject": f"Refund needs you: {reference}", "reference": reference, "state": state, "outside": False})
    return True
