"""1:1 add-on purchases — enrolment plan 2.6, E-15, migration §129.

The pricing page's products (authoritative, E-03), one-time Stripe checkouts:

    one_to_one_50   1 × 50 min   $26
    one_to_one_25   1 × 25 min   $13
    bundle4_50      4 × 50 min   $99
    bundle4_25      4 × 25 min   $49

Payment grants `session_entitlement` (reason addon_purchase, no expiry). The
purchase row's conditional pending -> paid update is the lock, so a retried
webhook grants once.

E-15: a learner with no active tutor link (Free, Phase 0) names a rung >= 2
active tutor; payment creates a guest link to them (source 'addon'), so the
learner can book only within their own roster (D-02).

Minutes matter: `fifty_minute_units` tells booking_service how many 50-minute
bookings the balance can still pay for (25-minute purchases cover 25 only).
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

PRODUCTS = {
    "one_to_one_50": {"sessions": 1, "minutes": 50, "amount_minor": 2600, "price": "stripe_price_addon_1v1_50"},
    "one_to_one_25": {"sessions": 1, "minutes": 25, "amount_minor": 1300, "price": "stripe_price_addon_1v1_25"},
    "bundle4_50": {"sessions": 4, "minutes": 50, "amount_minor": 9900, "price": "stripe_price_addon_bundle4_50"},
    "bundle4_25": {"sessions": 4, "minutes": 25, "amount_minor": 4900, "price": "stripe_price_addon_bundle4_25"},
}
MIN_RUNG = 2
INTENSIVE_NUDGE_PER_WEEK = 4


class AddonRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _has_tutor(student_id: str) -> bool:
    return bool(hs.linked_tutor_ids(student_id))


def eligible_tutors() -> list[dict]:
    """For a learner with no tutor: active tutors at rung >= 2 (E-15)."""
    from app.services.makeup_service import _names
    rows = (_t("tutor").select("id, status, deleted_at, language").eq("status", "active").execute()).data or []
    ids = [r["id"] for r in rows if not r.get("deleted_at") and (hs.rung_of(r["id"]) or 0) >= MIN_RUNG]
    names = _names(ids)
    langs = {r["id"]: r.get("language") for r in rows}
    return sorted(({"tutor_id": i, "name": names.get(i) or "A tutor", "language": langs.get(i)} for i in ids),
                  key=lambda t: t["name"])


def options(student_id: str) -> dict:
    """GET /addons — the products, and whether a tutor must be chosen."""
    need = not _has_tutor(student_id)
    return {"products": [{"product": k, "sessions": v["sessions"], "minutes": v["minutes"],
                          "amount_minor": v["amount_minor"], "currency": "USD",
                          "available": bool(getattr(settings, v["price"], ""))} for k, v in PRODUCTS.items()],
            "needs_tutor": need, "tutors": eligible_tutors() if need else []}


async def start_checkout(student_id: str, product: str, tutor_id: str | None, email: str | None,
                         success_url: str | None = None, cancel_url: str | None = None) -> dict:
    from app.services import stripe_service
    p = PRODUCTS.get(product)
    if not p:
        raise AddonRefused("bad_product")
    price_id = getattr(settings, p["price"], "")
    if not price_id:
        raise AddonRefused("not_configured")
    if not _has_tutor(student_id):
        if not tutor_id:
            raise AddonRefused("tutor_required")
        if tutor_id not in {t["tutor_id"] for t in eligible_tutors()}:
            raise AddonRefused("tutor_not_eligible")
    else:
        tutor_id = None
    row = (_t("addon_purchase").insert({
        "student_id": student_id, "product": product, "sessions": p["sessions"], "minutes": p["minutes"],
        "tutor_id": tutor_id, "amount_minor": p["amount_minor"], "currency": "USD",
    }).execute()).data[0]
    out = await stripe_service.create_addon_checkout(row["id"], student_id, price_id, email, success_url, cancel_url)
    _t("addon_purchase").update({"stripe_session_id": out["session_id"]}).eq("id", row["id"]).execute()
    return {**out, "purchase_id": row["id"]}


async def activate(purchase_id: str, stripe_session_id: str | None = None) -> dict:
    """The webhook: paid -> grant, and (E-15) the guest link. Idempotent."""
    got = (_t("addon_purchase").update({"status": "paid", "paid_at": _now().isoformat()})
           .eq("id", purchase_id).eq("status", "pending").execute()).data or []
    if not got:
        return {"granted": 0, "reason": "already_processed"}
    p = got[0]
    _t("session_entitlement").insert({
        "student_id": p["student_id"], "kind": "one_to_one", "direction": "grant", "quantity": int(p["sessions"]),
        "reason": "addon_purchase", "reference_type": f"addon_{int(p['minutes'])}", "reference_id": p["id"],
    }).execute()
    try:
        # Plan 8.5: the base of a referral commission.
        _t("student_payment").insert({"student_id": p["student_id"], "source": "addon",
                                      "external_ref": stripe_session_id or p["id"],
                                      "amount_minor": int(p["amount_minor"]), "currency": p.get("currency") or "USD",
                                      "paid_at": _now().isoformat()}).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("add-on payment record failed: %s", exc)
    link = None
    if p.get("tutor_id") and not _has_tutor(p["student_id"]):
        from app.services import matching_service
        try:
            link = await matching_service.create_link(p["student_id"], p["tutor_id"], "guest", source="addon")
        except Exception as exc:  # noqa: BLE001
            # The sessions are paid for either way; ops can link by hand.
            logger.error("add-on guest link for %s failed: %s", p["student_id"], exc)
    return {"granted": int(p["sessions"]), "link": link}


def mine(student_id: str) -> list[dict]:
    return (_t("addon_purchase").select("id, product, sessions, minutes, status, created_at, paid_at")
            .eq("student_id", student_id).order("created_at", desc=True).execute()).data or []


# ── what the balance can pay for ────────────────────────────────────────────

def fifty_minute_units(student_id: str, balance: int) -> int:
    """How many 50-minute bookings the balance can pay for. Units bought as
    25-minute sessions, net of 25-minute bookings, can't be stretched to 50."""
    rows = (_t("session_entitlement").select("direction, quantity, reason, reference_type, reference_id")
            .eq("student_id", student_id).eq("kind", "one_to_one").execute()).data or []
    bought_25 = sum(int(r["quantity"]) for r in rows
                    if r["direction"] == "grant" and r.get("reference_type") == "addon_25")
    if not bought_25:
        return max(0, balance)
    booked = [r["reference_id"] for r in rows if r["direction"] in ("consume", "refund") and r.get("reference_id")]
    durations = {}
    if booked:
        durations = {s["id"]: int(s.get("duration_minutes") or 50) for s in
                     (_t("one_to_one_session").select("id, duration_minutes").in_("id", booked).execute()).data or []}
    net_25 = 0
    for r in rows:
        if r["direction"] in ("consume", "refund") and durations.get(r.get("reference_id")) == 25:
            net_25 += int(r["quantity"]) if r["direction"] == "consume" else -int(r["quantity"])
    left_25_only = max(0, bought_25 - max(0, net_25))
    return max(0, balance - left_25_only)


def suggest_intensive(student_id: str, tier: str | None, now: datetime | None = None) -> bool:
    """More than 4 1:1s booked this week, and not on Intensive: suggest it."""
    if tier == "intensive_tier":
        return False
    now = now or _now()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (_t("one_to_one_session").select("scheduled_at, status").eq("student_id", student_id)
            .gte("scheduled_at", monday.isoformat()).execute()).data or []
    week_end = (monday + timedelta(days=7)).isoformat()
    n = sum(1 for r in rows if str(r["scheduled_at"]) < week_end and r.get("status") not in ("cancelled",))
    return n > INTENSIVE_NUDGE_PER_WEEK
