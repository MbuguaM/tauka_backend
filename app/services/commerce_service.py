"""Institutions, community partners and gift cards — enrolment plan Track 10, §139.

INSTITUTIONS (flag 'institutions')
  ops: create_org, create_contract (draft, a join code), activate (issues a
  Stripe invoice, send_invoice), end_contract. Staff: join(code) -> the
  contract's tier while it runs (seat limit), leave. Employer reports list
  progress and attendance ONLY for members who granted 'employer_reporting'.

COMMUNITY PARTNERS
  ops create a partner with a code; a learner who signs up with ?pref=CODE is
  attributed once (provision); the monthly statement is the partner's share
  of those learners' payments for 12 months (student_payment, E-11's shape).

GIFT CARDS (flag 'gift_cards')
  anyone buys N months of a tier (one-time Stripe checkout); paid -> a 12-char
  code emailed to the buyer; redeem(code) gives the recipient the tier for N
  months and those months' lessons; a daily sweep ends access afterwards
  unless they're paying by then. Unredeemed cards lapse after 12 months.
"""

import logging
import secrets
from datetime import date, datetime, timedelta, timezone

from app.config import settings
from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MONTHLY_MINOR = {"learner_tier": 3200, "tutor_tier": 7500, "intensive_tier": 14500}   # the pricing page
CARD_LIFETIME = timedelta(days=365)
PARTNER_MONTHS = 12


class CommerceRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _code(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def _ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise CommerceRefused("not_allowed")


# ── institutions ────────────────────────────────────────────────────────────

def create_org(actor: str, name: str, contact_email: str, country_code: str | None = None) -> dict:
    _ops(actor)
    if not is_ready("institutions"):
        raise CommerceRefused("not_enabled")
    return (_t("organization").insert({"name": name.strip()[:160], "contact_email": contact_email.strip().lower(),
                                       "country_code": (country_code or "").upper() or None}).execute()).data[0]


def create_contract(actor: str, org_id: str, tier: str, seats: int, starts_on: date, ends_on: date,
                    price_minor: int, currency: str) -> dict:
    _ops(actor)
    if tier not in MONTHLY_MINOR or ends_on <= starts_on or seats < 1:
        raise CommerceRefused("bad_contract")
    return (_t("org_contract").insert({"org_id": org_id, "tier": tier, "seats": seats,
                                       "starts_on": starts_on.isoformat(), "ends_on": ends_on.isoformat(),
                                       "price_minor": int(price_minor), "currency": currency.upper(),
                                       "join_code": _code(8), "status": "draft"}).execute()).data[0]


async def activate_contract(actor: str, contract_id: str) -> dict:
    """Draft -> active, and a Stripe invoice to the organisation (send_invoice:
    they pay by card or bank transfer on Stripe's hosted page)."""
    import asyncio
    import stripe
    _ops(actor)
    c = (_t("org_contract").select("*").eq("id", contract_id).limit(1).execute()).data or []
    if not c or c[0]["status"] != "draft":
        raise CommerceRefused("not_draft")
    c = c[0]
    org = (_t("organization").select("*").eq("id", c["org_id"]).limit(1).execute()).data[0]
    invoice_id = None
    if settings.stripe_secret_key and int(c["price_minor"]) > 0:
        def _bill():
            cust = org.get("stripe_customer_id") or stripe.Customer.create(
                email=org["contact_email"], name=org["name"], metadata={"org_id": org["id"]}).id
            stripe.InvoiceItem.create(customer=cust, amount=int(c["price_minor"]), currency=c["currency"].lower(),
                                      description=f"Tauka {c['tier'].replace('_tier', '')} plan, {c['seats']} seats, "
                                                  f"{c['starts_on']} to {c['ends_on']}")
            inv = stripe.Invoice.create(customer=cust, collection_method="send_invoice", days_until_due=30,
                                        metadata={"org_contract_id": c["id"]})
            stripe.Invoice.send_invoice(inv.id)
            return cust, inv.id
        cust, invoice_id = await asyncio.to_thread(_bill)
        if not org.get("stripe_customer_id"):
            _t("organization").update({"stripe_customer_id": cust}).eq("id", org["id"]).execute()
    _t("org_invoice").insert({"contract_id": c["id"], "amount_minor": int(c["price_minor"]), "currency": c["currency"],
                              "stripe_invoice_id": invoice_id, "status": "issued"}).execute()
    return (_t("org_contract").update({"status": "active"}).eq("id", c["id"]).execute()).data[0]


async def join(student_id: str, code: str, today: date | None = None) -> dict:
    from app.services.tier_service import change_tier
    if not is_ready("institutions"):
        raise CommerceRefused("not_enabled")
    today = today or _now().date()
    c = (_t("org_contract").select("*").eq("join_code", (code or "").strip().upper()).limit(1).execute()).data or []
    if not c or c[0]["status"] != "active" or not (str(c[0]["starts_on"]) <= today.isoformat() < str(c[0]["ends_on"])):
        raise CommerceRefused("no_such_contract")
    c = c[0]
    members = [m for m in (_t("org_member").select("student_id, left_at").eq("contract_id", c["id"]).execute()).data or []
               if not m.get("left_at")]
    if any(m["student_id"] == student_id for m in members):
        return {"contract_id": c["id"], "tier": c["tier"], "already": True}
    if len(members) >= int(c["seats"]):
        raise CommerceRefused("no_seats")
    _t("org_member").insert({"contract_id": c["id"], "student_id": student_id}).execute()
    await change_tier(student_id, c["tier"], "admin")
    return {"contract_id": c["id"], "tier": c["tier"]}


def employer_report(actor: str, contract_id: str) -> dict:
    """Progress and attendance for members who granted employer_reporting only."""
    from app.services.makeup_service import _names
    from app.services.tracker_service import for_student
    _ops(actor)
    members = [m["student_id"] for m in (_t("org_member").select("student_id, left_at").eq("contract_id", contract_id)
               .execute()).data or [] if not m.get("left_at")]
    consents = {}
    for r in (_t("consent_record").select("subject_id, purpose, granted, at").in_("subject_id", members or ["00000000-0000-0000-0000-000000000000"])
              .eq("purpose", "employer_reporting").execute()).data or []:
        prev = consents.get(r["subject_id"])
        if not prev or str(r["at"]) > str(prev["at"]):
            consents[r["subject_id"]] = r
    shared = [m for m in members if consents.get(m, {}).get("granted")]
    names = _names(shared) if shared else {}
    rows = []
    for sid in shared:
        t = for_student(sid)
        marks = (_t("cohort_session_attendance").select("status").eq("user_id", sid).execute()).data or []
        held = [m for m in marks if m["status"] in ("attended", "absent")]
        rows.append({"name": names.get(sid), "lessons_done": t.get("lessons_done"), "status": t.get("status"),
                     "attendance": round(sum(1 for m in held if m["status"] == "attended") / len(held), 2) if held else None})
    return {"members": len(members), "consented": len(shared), "rows": rows}


def set_report_consent(student_id: str, granted: bool) -> dict:
    """The member's own answer: may their organisation see their progress?
    Appended (the latest wins); asked after joining, changeable any time."""
    _t("consent_record").insert({"subject_id": student_id, "purpose": "employer_reporting", "granted": bool(granted),
                                 "policy_version": "employer-v1", "source": "settings",
                                 "at": _now().isoformat()}).execute()
    return {"employer_reporting": bool(granted)}


# ── community partners ──────────────────────────────────────────────────────

def create_partner(actor: str, name: str, code: str, contact_email: str | None, commission_bp: int = 1000) -> dict:
    _ops(actor)
    return (_t("partner").insert({"name": name.strip()[:160], "code": code.strip(), "contact_email": contact_email,
                                  "commission_bp": int(commission_bp), "active": True}).execute()).data[0]


def attribute(student_id: str, code: str | None) -> bool:
    """At provision: once per learner, never overwritten."""
    if not code:
        return False
    p = (_t("partner").select("id, active").eq("code", code.strip()).limit(1).execute()).data or []
    if not p or not p[0].get("active"):
        return False
    try:
        _t("partner_signup").insert({"student_id": student_id, "partner_id": p[0]["id"]}).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def statement(actor: str, partner_id: str, period_start: date, period_end: date) -> dict:
    _ops(actor)
    p = (_t("partner").select("*").eq("id", partner_id).limit(1).execute()).data or []
    if not p:
        raise CommerceRefused("no_such_partner")
    p = p[0]
    signups = {s["student_id"]: _ts(s["signed_up_at"]) for s in (_t("partner_signup").select("student_id, signed_up_at")
               .eq("partner_id", partner_id).execute()).data or []}
    pays = (_t("student_payment").select("student_id, amount_minor, currency, paid_at")
            .in_("student_id", list(signups) or ["00000000-0000-0000-0000-000000000000"]).execute()).data or []
    by_cur: dict[str, int] = {}
    for x in pays:
        paid = _ts(x["paid_at"])
        if not (period_start.isoformat() <= paid.date().isoformat() <= period_end.isoformat()):
            continue
        signed = signups[x["student_id"]]
        if paid < signed or paid >= signed + timedelta(days=30.44 * PARTNER_MONTHS):
            continue
        by_cur[x["currency"]] = by_cur.get(x["currency"], 0) + int(x["amount_minor"])
    return {"partner": p["name"], "signups": len(signups), "period": [period_start.isoformat(), period_end.isoformat()],
            "commission": {cur: amt * int(p["commission_bp"]) // 10000 for cur, amt in by_cur.items()}}


# ── gift cards ──────────────────────────────────────────────────────────────

async def buy_card(tier: str, months: int, purchaser_email: str, message: str | None) -> dict:
    import asyncio
    import stripe
    if not is_ready("gift_cards"):
        raise CommerceRefused("not_enabled")
    if tier not in MONTHLY_MINOR or not 1 <= int(months) <= 12:
        raise CommerceRefused("bad_card")
    if not settings.stripe_secret_key:
        raise CommerceRefused("not_configured")
    amount = MONTHLY_MINOR[tier] * int(months)
    card = (_t("gift_card").insert({"tier": tier, "months": int(months), "amount_minor": amount, "currency": "USD",
                                    "purchaser_email": purchaser_email.strip().lower(),
                                    "message": (message or "").strip()[:300] or None, "status": "pending"})
            .execute()).data[0]
    session = await asyncio.to_thread(lambda: stripe.checkout.Session.create(
        mode="payment", customer_email=purchaser_email,
        line_items=[{"price_data": {"currency": "usd", "unit_amount": amount,
                                    "product_data": {"name": f"Tauka gift card: {months} month(s) of {tier.replace('_tier', '').title()}"}},
                     "quantity": 1}],
        success_url=f"{settings.web_base_url}/gift-card/thanks", cancel_url=f"{settings.web_base_url}/gift-card",
        metadata={"gift_card_id": card["id"]}))
    _t("gift_card").update({"stripe_session_id": session.id}).eq("id", card["id"]).execute()
    return {"checkout_url": session.url}


async def card_paid(card_id: str) -> dict | None:
    """The webhook: a code, emailed to the buyer. Idempotent."""
    from app.services import email_service
    for _ in range(5):
        try:
            got = (_t("gift_card").update({"status": "paid", "code": _code(12)}).eq("id", card_id).eq("status", "pending")
                   .execute()).data or []
            break
        except Exception:  # noqa: BLE001  (a code collision: try another)
            got = []
    if not got:
        return None
    c = got[0]
    await email_service.send_template_email(c["purchaser_email"], "gift_card", {
        "subject": "Your Tauka gift card", "code": c["code"], "months": c["months"],
        "tier": c["tier"].replace("_tier", "").title(), "web_base_url": settings.web_base_url}, tags=["gift_card"])
    return c


async def redeem(student_id: str, code: str, now: datetime | None = None) -> dict:
    from app.services.lesson_service import grant_gift_months
    from app.services.tier_service import change_tier
    if not is_ready("gift_cards"):
        raise CommerceRefused("not_enabled")
    now = now or _now()
    c = (_t("gift_card").select("*").eq("code", (code or "").strip().upper()).limit(1).execute()).data or []
    if not c or c[0]["status"] != "paid" or _ts(c[0]["created_at"]) + CARD_LIFETIME < now:
        raise CommerceRefused("no_such_card")
    c = c[0]
    until = now + timedelta(days=30 * int(c["months"]))
    got = (_t("gift_card").update({"status": "redeemed", "redeemed_by": student_id, "redeemed_at": now.isoformat(),
                                   "access_until": until.isoformat()})
           .eq("id", c["id"]).eq("status", "paid").execute()).data or []
    if not got:
        raise CommerceRefused("no_such_card")
    await change_tier(student_id, c["tier"], "gifted")
    grant_gift_months(student_id, f"gc:{c['id']}", c["tier"], int(c["months"]), now)
    return {"tier": c["tier"], "access_until": until.isoformat()}


async def daily(now: datetime | None = None) -> dict:
    """End redeemed cards' access (unless the learner now pays); lapse old unredeemed ones."""
    from app.services.tier_service import change_tier
    now = now or _now()
    out = {"ended": 0, "lapsed": 0}
    for c in (_t("gift_card").select("*").in_("status", ["paid", "redeemed"]).execute()).data or []:
        if c["status"] == "paid" and _ts(c["created_at"]) + CARD_LIFETIME < now:
            _t("gift_card").update({"status": "expired"}).eq("id", c["id"]).eq("status", "paid").execute()
            out["lapsed"] += 1
        elif c["status"] == "redeemed" and c.get("access_until") and _ts(c["access_until"]) <= now:
            st = (_t("student").select("subscription_status").eq("id", c["redeemed_by"]).limit(1).execute()).data or [{}]
            if st[0].get("subscription_status") not in ("active", "trialing", "past_due"):
                await change_tier(c["redeemed_by"], "free_tier", "self")
            _t("gift_card").update({"status": "expired"}).eq("id", c["id"]).execute()
            out["ended"] += 1
    return out
