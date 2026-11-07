"""Exchange guests, language bands, Exchange+ and the practice certificate (§159;
exchange-extension-plan XE-23, XE-13, XE-25).

XE-23  Someone learning Amharic outside Tauka joins the exchange from the website: an
       ordinary student account (POST /auth/provision's rules, user_type 'student', Free
       tier) marked `enrolment_state.state = 'exchange_guest'` (D-6). They take Foundation
       sessions only, with a taster and the Free allowance, and every session ends with a
       card pointing to the course. Behind `exchange_guests`.
XE-13  Bands per language: a learner's Amharic band from their lesson (Foundation = Phase 0
       and FSI 1-20, Conversation 21-60, Advanced after) unless a placement or a tutor set
       it in the last 90 days; a partner's English band from the reviewed level.
XE-25  Exchange+ for Amharic speakers practising English: a month at a time (doc 02 §8.4),
       bought through payment_service (kind 'xp'). Kenya (KES) and the diaspora (USD) only;
       Ethiopia waits for gate G5. Behind `exchange_plus`.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.core.flags import is_ready
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

PLACEMENT_FRESH = timedelta(days=90)
PLUS_DAYS = 30


class MemberRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# ── XE-23 guests ────────────────────────────────────────────────────────────

def _course_id() -> str | None:
    rows = (_t("course").select("id, created_at").execute()).data or []
    return sorted(rows, key=lambda r: str(r.get("created_at")))[0]["id"] if rows else None


async def join_guest(user_id: str, first_name: str | None, age_confirmed: bool) -> dict:
    """The web's "Learning Amharic? Practise with a native speaker" sign-up, after the
    emailed code. A partner account can't also be a guest learner (A2)."""
    from app.services import exchange_service as ex
    from app.services import provisioning_service
    if not ex.enabled() or not is_ready("exchange_guests"):
        raise MemberRefused("not_enabled")
    if not age_confirmed:
        raise MemberRefused("age_required")
    prof = (_t("user_profiles").select("id, user_type").eq("id", user_id).limit(1).execute()).data or []
    if prof and prof[0].get("user_type") == "partner":
        raise MemberRefused("partner_account")
    await provisioning_service.provision(user_id, first_name=first_name, age_confirmed=True, source_platform="web")
    st = (_t("enrolment_state").select("id, state").eq("student_id", user_id).limit(1).execute()).data or []
    if not st:
        course = _course_id()
        if not course:
            raise MemberRefused("no_course")
        _t("enrolment_state").insert({"student_id": user_id, "course_id": course, "state": "exchange_guest"}).execute()
    if not (_t("exchange_language_level").select("user_id").eq("user_id", user_id).eq("language", "am").limit(1)
            .execute()).data:
        _t("exchange_language_level").insert({"user_id": user_id, "language": "am", "band": "foundation",
                                              "source": "self"}).execute()
    return guest_state(user_id)


def guest_state(user_id: str) -> dict:
    from app.services import exchange_service as ex
    guest = ex.enrolment_state(user_id) == "exchange_guest"   # a paid guest has moved on (2026-11-07)
    return {"guest": guest, "eligible": ex.eligible(user_id),
            "allowance": ex.allowance(user_id) if ex.eligible(user_id) else None,
            # What each session's closing card points to (the conversion step).
            "next": {"placement": "/test/amharic", "pricing": "/pricing", "downloads": "/downloads"} if guest else None}


def graduate_paid_guests() -> int:
    """Daily: guests now on a paid plan move to Phase 0 (exchange_service.enrolment_state)."""
    from app.services import exchange_service as ex
    rows = (_t("enrolment_state").select("student_id").eq("state", "exchange_guest").execute()).data or []
    return sum(1 for r in rows if ex.enrolment_state(r["student_id"]) != "exchange_guest")


# ── XE-13 bands ─────────────────────────────────────────────────────────────

def band_for_lesson(lesson: int) -> str:
    return "foundation" if lesson <= 20 else "conversation" if lesson <= 60 else "advanced"


def band_for_english(level: str | None) -> str | None:
    return {"A2": "foundation", "B1": "conversation", "B2": "conversation", "C1": "advanced"}.get(level or "")


def refresh_levels(now: datetime | None = None) -> int:
    """Daily. Never overwrites a placement or tutor band less than 90 days old."""
    from app.services import exchange_service as ex
    now = now or _now()
    current = {(r["user_id"], r["language"]): r for r in (_t("exchange_language_level").select("*").execute()).data or []}

    def fresh(key) -> bool:
        r = current.get(key)
        return bool(r and r["source"] in ("placement", "tutor") and _ts(r["assessed_at"]) > now - PLACEMENT_FRESH)

    n = 0
    learners = {r["learner_id"] for r in (_t("exchange_session").select("learner_id").execute()).data or []}
    for uid in learners:
        if fresh((uid, "am")):
            continue
        band = "foundation" if ex.foundation_only(uid) else band_for_lesson(ex._lesson(uid))
        if (current.get((uid, "am")) or {}).get("band") != band:
            _t("exchange_language_level").upsert({"user_id": uid, "language": "am", "band": band, "source": "lesson",
                                                  "assessed_at": now.isoformat()}).execute()
            n += 1
    for p in (_t("exchange_partner").select("user_id, english_level").execute()).data or []:
        band = band_for_english(p.get("english_level"))
        if band and not fresh((p["user_id"], "en")) and (current.get((p["user_id"], "en")) or {}).get("band") != band:
            _t("exchange_language_level").upsert({"user_id": p["user_id"], "language": "en", "band": band,
                                                  "cefr": p["english_level"], "source": "speaking_sample",
                                                  "assessed_at": now.isoformat()}).execute()
            n += 1
    return n


# ── XE-25 Exchange+ ─────────────────────────────────────────────────────────

def _price(country: str | None) -> tuple[int, str]:
    if (country or "").upper() == "ET":
        raise MemberRefused("not_in_market")     # gate G5: collecting in Ethiopia isn't cleared
    if (country or "").upper() == "KE":
        return int(settings.exchange_plus_kes_minor or 0), "KES"
    return int(settings.exchange_plus_usd_minor or 0), "USD"


def plus_state(user_id: str, now: datetime | None = None) -> dict:
    now = now or _now()
    p = (_t("exchange_partner").select("plus_until, country_code").eq("user_id", user_id).limit(1).execute()).data or []
    until = p[0].get("plus_until") if p else None
    try:
        amount, currency = _price(p[0].get("country_code") if p else None)
        market = True
    except MemberRefused:
        amount, currency, market = 0, None, False
    return {"enabled": is_ready("exchange_plus"), "active": bool(until and _ts(until) > now), "until": until,
            "price_minor": amount or None, "currency": currency, "in_market": market}


async def plus_checkout(user_id: str) -> dict:
    from app.services import payment_service as ps
    if not is_ready("exchange_plus"):
        raise MemberRefused("not_enabled")
    p = (_t("exchange_partner").select("user_id, status, country_code").eq("user_id", user_id).limit(1).execute()).data or []
    if not p or p[0]["status"] not in ("active", "waitlisted"):
        raise MemberRefused("partners_only")
    amount, currency = _price(p[0].get("country_code"))
    if amount <= 0 or not ps.configured():
        raise MemberRefused("not_configured")
    u = db.auth.admin.get_user_by_id(user_id)
    email = getattr(getattr(u, "user", None), "email", None)
    if not email:
        raise MemberRefused("no_email")
    pid = str(uuid.uuid4())
    _t("exchange_plus_purchase").insert({"id": pid, "user_id": user_id, "days": PLUS_DAYS, "amount_minor": amount,
                                         "currency": currency, "email": email}).execute()
    try:
        pay = await ps.checkout("xp", pid, amount, currency, email, "Tauka Exchange+ (one month)")
    except ps.PaymentFailed as exc:
        _t("exchange_plus_purchase").update({"status": "failed"}).eq("id", pid).execute()
        raise MemberRefused("payment_failed") from exc
    _t("exchange_plus_purchase").update({"payment_provider": pay.provider, "payment_reference": pay.reference}).eq("id", pid).execute()
    return {"checkout_url": pay.url, "reference": pay.reference}


async def plus_paid(purchase_id: str, now: datetime | None = None) -> None:
    """payment_service.settle(): a month from now, or from the end of the current month."""
    now = now or _now()
    got = (_t("exchange_plus_purchase").update({"status": "paid", "paid_at": now.isoformat()}).eq("id", purchase_id)
           .eq("status", "pending").execute()).data or []
    if not got:
        return                                   # a repeat grants nothing
    row = got[0]
    p = (_t("exchange_partner").select("plus_until").eq("user_id", row["user_id"]).limit(1).execute()).data or []
    base = max(now, _ts(p[0]["plus_until"])) if p and p[0].get("plus_until") else now
    _t("exchange_partner").update({"plus_until": (base + timedelta(days=int(row["days"]))).isoformat()}) \
        .eq("user_id", row["user_id"]).execute()


async def plus_confirm(user_id: str, reference: str) -> dict:
    from app.services import payment_service as ps
    parts = ps.split_reference(reference)
    if not parts or parts[0] != "xp":
        raise MemberRefused("no_such_order")
    row = ps.order_row("xp", parts[1])
    if not row or row["user_id"] != user_id:
        raise MemberRefused("no_such_order")
    if row["status"] == "pending" and row.get("payment_provider") == "paystack":
        try:
            data = await ps.paystack_call("GET", f"/transaction/verify/{row['payment_reference']}")
        except ps.PaymentFailed as exc:
            raise MemberRefused("payment_failed") from exc
        if data.get("status") == "success":
            await ps.settle(reference, int(data.get("amount") or 0), data.get("currency") or "")
    return plus_state(user_id)


def certificate(user_id: str) -> dict:
    """Doc 05 §4.8: verified speaking hours and band, between dates. Not a qualification."""
    rows = [r for r in (_t("exchange_session").select("scheduled_at, length_min, status, partner_id, learner_id")
            .eq("partner_id", user_id).eq("status", "completed").execute()).data or []]
    rows += [r for r in (_t("exchange_session").select("scheduled_at, length_min, status, partner_id, learner_id")
             .eq("learner_id", user_id).eq("status", "completed").execute()).data or []]
    if not rows:
        return {"hours": 0}
    minutes = sum(int(r["length_min"]) for r in rows)
    band = (_t("exchange_language_level").select("band, language").eq("user_id", user_id).execute()).data or []
    name = (_t("user_profiles").select("first_name").eq("id", user_id).limit(1).execute()).data or []
    dates = sorted(str(r["scheduled_at"])[:10] for r in rows)
    return {"name": (name[0].get("first_name") if name else None) or "", "hours": round(minutes / 60, 1),
            "sessions": len(rows), "bands": {b["language"]: b["band"] for b in band}, "from": dates[0], "to": dates[-1],
            "wording": "This is a record of practice, not an accredited qualification."}
