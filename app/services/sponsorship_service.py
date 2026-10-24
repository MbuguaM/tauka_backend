"""Sponsorship — §144, the pricing page's "Three other ways in" (flag 'sponsorship').

THE SPONSORSHIP WAITLIST
  apply(): a signed-in learner asks for a sponsored seat with a first name, a
  country, a reason and a short story, and consents to those (and only those)
  being shown to sponsors ('sponsorship_listing', L-024). Ops review every
  application (review) before it is listed. public_list() serves listed,
  unheld ones without any account id, surname or email.

SPONSORING
  checkout(mode='waitlist'): pay for a listed learner. The request is held for
  30 minutes (the Stripe session's own lifetime) so two sponsors can't pay for
  the same person. Paid -> granted at once and the learner is emailed.
  checkout(mode='named'): pay for someone the sponsor names by email. Paid ->
  we email them a claim link (the token is stored hashed); claim() grants it.
  A sponsor who was once sponsored is paying it forward (forward_of).

GRANTING is the gift card's: the tier now (change_tier 'gifted') and the
months' lessons (grant_gift_months). daily() releases lapsed holds, ends access
after the months unless the learner is paying by then (and invites them to pay
it forward), and lapses named seats unclaimed after 12 months.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.commerce_service import MONTHLY_MINOR
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

HOLD = timedelta(minutes=31)          # the Stripe session lives 30 minutes
CLAIM_LIFETIME = timedelta(days=365)
MONTH = timedelta(days=30)
REASONS = ("heritage", "partner", "relocating", "faith", "work", "other")
PUBLIC_FIELDS = "id, first_name, country_code, reason, story, tier_wanted, months_wanted, listed_at"


class SponsorshipRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _on() -> None:
    if not is_ready("sponsorship"):
        raise SponsorshipRefused("not_enabled")


def _ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise SponsorshipRefused("not_allowed")


def _email_of(user_id: str) -> str | None:
    try:
        return db.auth.admin.get_user_by_id(user_id).user.email
    except Exception as exc:  # noqa: BLE001
        logger.warning("sponsorship: no email for %s: %s", user_id, exc)
        return None


def _plan(tier: str) -> str:
    return tier.replace("_tier", "").title()


async def _mail(to: str | None, template: str, data: dict) -> None:
    """Best-effort, like every mail here: a failed send never undoes a payment."""
    if not to:
        return
    from app.services import email_service
    try:
        await email_service.send_template_email(to, template, {"web_base_url": settings.web_base_url, **data},
                                                tags=["sponsorship"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("sponsorship mail %s failed: %s", template, exc)


# ── the learner who asks ────────────────────────────────────────────────────

def apply(student_id: str, first_name: str, country_code: str | None, reason: str, story: str,
          tier_wanted: str = "tutor_tier", months_wanted: int = 12) -> dict:
    _on()
    story, first_name = (story or "").strip(), (first_name or "").strip()
    if reason not in REASONS or tier_wanted not in MONTHLY_MINOR or int(months_wanted) not in (3, 6, 12) \
            or not 1 <= len(first_name) <= 40 or not 40 <= len(story) <= 600:
        raise SponsorshipRefused("bad_request")
    open_ = (_t("sponsorship_request").select("id").eq("student_id", student_id)
             .in_("status", ["pending", "listed"]).limit(1).execute()).data or []
    if open_:
        raise SponsorshipRefused("already_open")
    row = (_t("sponsorship_request").insert({
        "student_id": student_id, "first_name": first_name, "story": story, "reason": reason,
        "country_code": (country_code or "").strip().upper() or None, "tier_wanted": tier_wanted,
        "months_wanted": int(months_wanted), "status": "pending"}).execute()).data[0]
    _t("consent_record").insert({"subject_id": student_id, "purpose": "sponsorship_listing", "granted": True,
                                 "policy_version": "sponsorship-v1", "source": "in_app",
                                 "at": _now().isoformat()}).execute()
    return row


def mine(student_id: str) -> dict:
    """The learner's own application and any seat sponsored for them."""
    req = (_t("sponsorship_request").select(PUBLIC_FIELDS + ", status, decline_reason, created_at").eq("student_id", student_id)
           .order("created_at", desc=True).limit(1).execute()).data or []
    seats = (_t("sponsorship").select("id, tier, months, status, anonymous, sponsor_name, message, access_until, "
                                      "claimed_at, forward_of")
             .eq("recipient_id", student_id).in_("status", ["claimed", "expired"])
             .order("claimed_at", desc=True).execute()).data or []
    for s in seats:
        s["from"] = "An anonymous sponsor" if s.pop("anonymous") or not s.get("sponsor_name") else s["sponsor_name"]
        s.pop("sponsor_name", None)
    return {"request": req[0] if req else None, "seats": seats, "can_pay_forward": bool(seats)}


def withdraw(student_id: str) -> dict:
    got = (_t("sponsorship_request").update({"status": "withdrawn", "held_until": None})
           .eq("student_id", student_id).in_("status", ["pending", "listed"]).execute()).data or []
    if not got:
        raise SponsorshipRefused("no_such_request")
    return {"status": "withdrawn"}


def public_list(now: datetime | None = None) -> list[dict]:
    """Listed and not held. Only the fields the applicant agreed to show."""
    _on()
    now = now or _now()
    rows = (_t("sponsorship_request").select(PUBLIC_FIELDS + ", held_until").eq("status", "listed")
            .order("listed_at").execute()).data or []
    out = []
    for r in rows:
        if r.get("held_until") and _ts(r["held_until"]) > now:
            continue
        # Projected here as well as in the select: these fields and nothing else leave the server.
        shown = {k: r.get(k) for k in PUBLIC_FIELDS.split(", ")}
        shown["price_minor"] = MONTHLY_MINOR[r["tier_wanted"]] * int(r["months_wanted"])
        out.append(shown)
    return out


# ── ops ─────────────────────────────────────────────────────────────────────

def queue(actor: str, status: str = "pending") -> list[dict]:
    _ops(actor)
    return (_t("sponsorship_request").select("*").eq("status", status).order("created_at").execute()).data or []


async def review(actor: str, request_id: str, decision: str, reason: str | None = None) -> dict:
    _ops(actor)
    if decision not in ("list", "decline"):
        raise SponsorshipRefused("bad_request")
    now = _now().isoformat()
    patch = {"reviewed_by": actor, "reviewed_at": now}
    if decision == "list":
        patch.update({"status": "listed", "listed_at": now})
    else:
        patch.update({"status": "declined", "decline_reason": (reason or "").strip()[:300] or None})
    got = (_t("sponsorship_request").update(patch).eq("id", request_id).eq("status", "pending").execute()).data or []
    if not got:
        raise SponsorshipRefused("no_such_request")
    r = got[0]
    await _mail(_email_of(r["student_id"]), "sponsorship_review", {
        "subject": "You're on the sponsorship list" if decision == "list" else "About your sponsorship application",
        "listed": decision == "list", "first_name": r["first_name"], "reason": r.get("decline_reason")})
    return r


# ── the sponsor ─────────────────────────────────────────────────────────────

def _hold(request_id: str, now: datetime) -> dict:
    until = (now + HOLD).isoformat()
    got = (_t("sponsorship_request").update({"held_until": until}).eq("id", request_id).eq("status", "listed")
           .is_("held_until", "null").execute()).data or []
    if not got:
        got = (_t("sponsorship_request").update({"held_until": until}).eq("id", request_id).eq("status", "listed")
               .lt("held_until", now.isoformat()).execute()).data or []
    if not got:
        raise SponsorshipRefused("taken")
    return got[0]


async def checkout(mode: str, sponsor_email: str, *, request_id: str | None = None, months: int | None = None,
                   recipient_email: str | None = None, recipient_name: str | None = None, tier: str | None = None,
                   sponsor_name: str | None = None, anonymous: bool = False, message: str | None = None,
                   sponsor_user_id: str | None = None, now: datetime | None = None) -> dict:
    import asyncio
    import stripe
    _on()
    now = now or _now()
    if not settings.stripe_secret_key:
        raise SponsorshipRefused("not_configured")
    row = {"mode": mode, "sponsor_email": sponsor_email.strip().lower(), "anonymous": bool(anonymous),
           "sponsor_name": (sponsor_name or "").strip()[:60] or None, "message": (message or "").strip()[:300] or None,
           "sponsor_user_id": sponsor_user_id, "status": "pending", "currency": "USD"}
    if mode == "waitlist":
        if not request_id:
            raise SponsorshipRefused("bad_request")
        req = (_t("sponsorship_request").select("*").eq("id", request_id).limit(1).execute()).data or []
        if not req or req[0]["status"] != "listed":
            raise SponsorshipRefused("taken")
        months = int(months or req[0]["months_wanted"])
        if months not in (3, 6, 12) or months > int(req[0]["months_wanted"]) \
                or (sponsor_user_id and sponsor_user_id == req[0]["student_id"]):
            raise SponsorshipRefused("bad_request")
        req = _hold(request_id, now)
        row.update({"request_id": request_id, "tier": req["tier_wanted"], "months": months})
        who = req["first_name"]
    elif mode == "named":
        if not recipient_email or tier not in MONTHLY_MINOR or not 1 <= int(months or 0) <= 12:
            raise SponsorshipRefused("bad_request")
        row.update({"recipient_email": recipient_email.strip().lower(), "tier": tier, "months": int(months),
                    "recipient_name": (recipient_name or "").strip()[:60] or None})
        who = row["recipient_name"] or "someone you know"
    else:
        raise SponsorshipRefused("bad_request")
    row["amount_minor"] = MONTHLY_MINOR[row["tier"]] * row["months"]
    if sponsor_user_id:
        mine_ = (_t("sponsorship").select("id").eq("recipient_id", sponsor_user_id)
                 .in_("status", ["claimed", "expired"]).limit(1).execute()).data or []
        row["forward_of"] = mine_[0]["id"] if mine_ else None
    sp = (_t("sponsorship").insert(row).execute()).data[0]
    try:
        session = await asyncio.to_thread(lambda: stripe.checkout.Session.create(
            mode="payment", customer_email=row["sponsor_email"],
            expires_at=int((now + timedelta(minutes=30)).timestamp()),
            line_items=[{"price_data": {"currency": "usd", "unit_amount": row["amount_minor"],
                                        "product_data": {"name": f"Sponsor {who}: {row['months']} month(s) of "
                                                                 f"Tauka {_plan(row['tier'])}"}},
                         "quantity": 1}],
            success_url=f"{settings.web_base_url}/sponsor/thanks", cancel_url=f"{settings.web_base_url}/sponsor",
            metadata={"sponsorship_id": sp["id"]}))
    except Exception:
        _t("sponsorship").update({"status": "cancelled"}).eq("id", sp["id"]).execute()
        if mode == "waitlist":
            _t("sponsorship_request").update({"held_until": None}).eq("id", request_id).execute()
        raise
    _t("sponsorship").update({"stripe_session_id": session.id}).eq("id", sp["id"]).execute()
    return {"checkout_url": session.url}


async def _grant(sp: dict, student_id: str, now: datetime) -> dict:
    from app.services.lesson_service import grant_gift_months
    from app.services.tier_service import change_tier
    until = now + MONTH * int(sp["months"])
    got = (_t("sponsorship").update({"status": "claimed", "recipient_id": student_id, "claimed_at": now.isoformat(),
                                     "access_until": until.isoformat()})
           .eq("id", sp["id"]).eq("status", "paid").execute()).data or []
    if not got:
        raise SponsorshipRefused("no_such_seat")
    await change_tier(student_id, sp["tier"], "gifted")
    grant_gift_months(student_id, f"sp:{sp['id']}", sp["tier"], int(sp["months"]), now)
    return got[0]


async def paid(sponsorship_id: str, now: datetime | None = None) -> dict | None:
    """The webhook. Idempotent: only a pending seat moves."""
    now = now or _now()
    got = (_t("sponsorship").update({"status": "paid"}).eq("id", sponsorship_id).eq("status", "pending")
           .execute()).data or []
    if not got:
        return None
    sp = got[0]
    plan = {"months": sp["months"], "tier": _plan(sp["tier"])}
    sponsor = "An anonymous sponsor" if sp["anonymous"] or not sp.get("sponsor_name") else sp["sponsor_name"]
    if sp["mode"] == "waitlist":
        req = (_t("sponsorship_request").update({"status": "sponsored", "sponsored_at": now.isoformat(),
                                                 "held_until": None})
               .eq("id", sp["request_id"]).eq("status", "listed").execute()).data or []
        if not req:
            # Withdrawn while the sponsor was paying: the money is held for ops to refund or redirect.
            await _mail(settings.admin_email, "sponsorship_receipt", {
                "subject": f"Sponsorship {sp['id']} paid for a request that is no longer listed", "ops": True,
                "who": "a withdrawn request", **plan})
            return sp
        sp = await _grant(sp, req[0]["student_id"], now)
        await _mail(_email_of(req[0]["student_id"]), "sponsorship_sponsored", {
            "subject": "You've been sponsored", "first_name": req[0]["first_name"], "sponsor": sponsor,
            "message": sp.get("message"), **plan})
        who = req[0]["first_name"]
    else:
        token = secrets.token_urlsafe(24)
        _t("sponsorship").update({"claim_token_hash": _hash(token)}).eq("id", sp["id"]).execute()
        await _mail(sp["recipient_email"], "sponsorship_claim", {
            "subject": f"{sponsor} has sponsored your Amharic", "sponsor": sponsor, "name": sp.get("recipient_name"),
            "message": sp.get("message"), "claim_url": f"{settings.web_base_url}/sponsorship/claim?token={token}",
            **plan})
        who = sp.get("recipient_name") or sp["recipient_email"]
    await _mail(sp["sponsor_email"], "sponsorship_receipt", {
        "subject": "Thank you for sponsoring a learner", "who": who, "named": sp["mode"] == "named",
        "forward": bool(sp.get("forward_of")), **plan})
    return sp


async def claim(student_id: str, token: str, now: datetime | None = None) -> dict:
    _on()
    now = now or _now()
    rows = (_t("sponsorship").select("*").eq("claim_token_hash", _hash((token or "").strip())).limit(1)
            .execute()).data or []
    if not rows or rows[0]["status"] != "paid" or (rows[0].get("created_at") and _ts(rows[0]["created_at"]) + CLAIM_LIFETIME < now):
        raise SponsorshipRefused("no_such_seat")
    sp = await _grant(rows[0], student_id, now)
    return {"tier": sp["tier"], "months": sp["months"], "access_until": sp["access_until"]}


# ── the daily sweep ─────────────────────────────────────────────────────────

async def daily(now: datetime | None = None) -> dict:
    from app.services.tier_service import change_tier
    now = now or _now()
    out = {"released": 0, "ended": 0, "lapsed": 0}
    for r in (_t("sponsorship_request").select("id, held_until").eq("status", "listed")
              .lt("held_until", now.isoformat()).execute()).data or []:
        _t("sponsorship_request").update({"held_until": None}).eq("id", r["id"]).execute()
        out["released"] += 1
    for s in (_t("sponsorship").select("*").in_("status", ["paid", "claimed"]).execute()).data or []:
        if s["status"] == "paid" and s["mode"] == "named" and s.get("created_at") and _ts(s["created_at"]) + CLAIM_LIFETIME < now:
            _t("sponsorship").update({"status": "expired"}).eq("id", s["id"]).eq("status", "paid").execute()
            out["lapsed"] += 1
        elif s["status"] == "claimed" and s.get("access_until") and _ts(s["access_until"]) <= now:
            st = (_t("student").select("subscription_status").eq("id", s["recipient_id"]).limit(1)
                  .execute()).data or [{}]
            if st[0].get("subscription_status") not in ("active", "trialing", "past_due"):
                await change_tier(s["recipient_id"], "free_tier", "self")
            _t("sponsorship").update({"status": "expired", "forward_invited_at": now.isoformat()}) \
                .eq("id", s["id"]).execute()
            await _mail(_email_of(s["recipient_id"]), "sponsorship_forward", {
                "subject": "Your sponsored seat has ended. Pass it on?", "months": s["months"],
                "tier": _plan(s["tier"])})
            out["ended"] += 1
    return out
