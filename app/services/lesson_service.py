"""Paid lesson unlocks — enrolment plan §2c/§2d, migration §91.

A lesson is one `app.unit`. Each paid month unlocks the next N lessons of the
course sequence (Phase 0: introduction, alphabet; then Phase 1), continuing
from where the learner's last grant ended:

    Learner 2 · Tutor 4 · Intensive 6   (a fixed count per paid month)

The two free lessons (Phase 0 lesson 1, Phase 1 lesson 1) need no grant; they
still occupy their slots, so Tutor month 1 = introduction (free), alphabet,
Unit 1 (free), Unit 2 — the developer's example.

All sequencing happens in `app.grant_lesson_slots()` under a per-student lock,
and it is idempotent on `grant_ref`, so a retried Stripe webhook cannot hand out
a second month. The storage policy on `Tauka_Courses` reads the result: an
ungranted lesson cannot be downloaded at all. Grants are never deleted; a gift
refund revokes its months.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

LESSONS_PER_MONTH = {
    "learner_tier": 2,
    "tutor_tier": 4,
    "intensive_tier": 6,
}

# Gifts already count a month as 30 days (gift_service.activate_gift).
GIFT_MONTH = timedelta(days=30)


def lessons_per_month(tier: str | None) -> int:
    return LESSONS_PER_MONTH.get(tier or "", 0)


def grant_paid_month(student_id: str, tier: str | None, grant_ref: str) -> dict | None:
    """Unlock one paid month of lessons. `grant_ref` is the Stripe invoice id
    (or the checkout session id for the first month). Returns the grant row, or
    None for a tier with no lessons to unlock."""
    count = lessons_per_month(tier)
    if count <= 0 or not grant_ref:
        return None
    result = db.schema("app").rpc("grant_lesson_slots", {
        "p_student_id": student_id,
        "p_count": count,
        "p_source": "paid_month",
        "p_grant_ref": grant_ref,
    }).execute()
    return result.data


def grant_gift_months(
    student_id: str, gift_id: str, tier: str | None, months: int, starts_at: datetime,
) -> list:
    """One grant per gift month. Later months are dated forward, so a 3-month
    gift unlocks at the same pace as three paid months would."""
    count = lessons_per_month(tier)
    if count <= 0:
        return []
    rows = []
    for k in range(max(months, 0)):
        result = db.schema("app").rpc("grant_lesson_slots", {
            "p_student_id": student_id,
            "p_count": count,
            "p_source": "gift_month",
            "p_grant_ref": f"{gift_id}:{k}",
            "p_available_from": (starts_at + GIFT_MONTH * k).isoformat(),
        }).execute()
        rows.append(result.data)
    return rows


def revoke_gift_months(student_id: str, gift_id: str) -> None:
    """On a gift refund (full, within 7 days): withdraw every month of it.
    Lessons already on the device stay there; they just can't be downloaded
    again. Rows are revoked, never deleted (the table is append-only)."""
    now = datetime.now(timezone.utc).isoformat()
    (
        db.schema("app").table("lesson_entitlement")
        .update({"revoked_at": now})
        .eq("student_id", student_id)
        .eq("source", "gift_month")
        .like("grant_ref", f"{gift_id}:%")
        .is_("revoked_at", "null")
        .execute()
    )


# ── Read-ahead and the upgrade offer (enrolment plan §2c, 2.2) ─────────────────
#
# While subscribed, a learner may unlock ONE lesson from next month, by asking.
# `grant_lesson_slots(source='read_ahead')` gives the slot at the cursor and the
# cursor ignores read-ahead rows, so next month's grant covers it again: the
# lesson is early, not extra. One per paid month (keyed on that month's grant).
# Completing it before the next month's grant -> an upgrade offer: Learner ->
# Tutor, Tutor -> Intensive. The trigger is a lesson-completion record
# (`unit_progress`), never telemetry. Intensive: 1:1 add-ons (2.6) aren't sold
# yet, so no offer. Not during a missed-payment grace (§126).

NEXT_TIER = {"learner_tier": "tutor_tier", "tutor_tier": "intensive_tier"}
_LIVE = ("active", "trialing")


class ReadAheadRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _app(name: str):
    return db.schema("app").table(name)


def _month_grant(student_id: str) -> dict | None:
    """The newest paid or gift month already available."""
    now = datetime.now(timezone.utc).isoformat()
    rows = (_app("lesson_entitlement").select("grant_ref, source, slot_to, available_from, created_at, revoked_at")
            .eq("student_id", student_id).in_("source", ["paid_month", "gift_month"]).execute()).data or []
    live = [r for r in rows if not r.get("revoked_at") and str(r.get("available_from") or "") <= now]
    return max(live, key=lambda r: (int(r["slot_to"]), str(r.get("created_at")))) if live else None


def _cursor(student_id: str) -> int:
    rows = (_app("lesson_entitlement").select("slot_to, source, revoked_at").eq("student_id", student_id)
            .execute()).data or []
    return max([int(r["slot_to"]) for r in rows if not r.get("revoked_at") and r.get("source") != "read_ahead"],
               default=0)


def _unit_at(course_id: str, slot: int) -> dict | None:
    """The same order as app.unit_lesson_slot(): preliminary first, then unit_order."""
    units = (_app("unit").select("id, title, unit_order, is_preliminary, created_at, deleted_at")
             .eq("course_id", course_id).execute()).data or []
    units = sorted((u for u in units if not u.get("deleted_at")),
                   key=lambda u: (not u.get("is_preliminary"), u.get("unit_order") or 0,
                                  str(u.get("created_at")), u["id"]))
    if not 0 <= slot < len(units):
        return None
    u = units[slot]
    firsts = {pre: min((v.get("unit_order") or 0) for v in units if bool(v.get("is_preliminary")) == pre)
              for pre in {bool(v.get("is_preliminary")) for v in units}}
    return {**u, "free": (u.get("unit_order") or 0) == firsts[bool(u.get("is_preliminary"))]}


def read_ahead(student_id: str, course_id: str) -> dict:
    """GET /enrolment/read-ahead — what the learner may read ahead, whether it's
    unlocked, whether it's done, and any upgrade offer."""
    st = (_app("student").select("tier, subscription_status, active_gift_id").eq("id", student_id)
          .limit(1).execute()).data or [{}]
    st = st[0]
    tier = st.get("tier")
    out = {"available": False, "reason": None, "lesson": None, "unlocked": False, "completed": False, "offer": None}
    if lessons_per_month(tier) <= 0:
        return {**out, "reason": "not_paying"}
    if st.get("subscription_status") not in _LIVE and not st.get("active_gift_id"):
        return {**out, "reason": "not_subscribed"}
    from app.services.grace_service import current as grace_now
    if grace_now(student_id):
        return {**out, "reason": "payment_pending"}
    month = _month_grant(student_id)
    if not month:
        return {**out, "reason": "no_paid_month"}
    unit = _unit_at(course_id, _cursor(student_id))
    if not unit:
        return {**out, "reason": "course_finished"}
    if unit["free"]:
        return {**out, "reason": "next_is_free", "lesson": {"unit_id": unit["id"], "title": unit.get("title")}}
    granted = bool((_app("lesson_entitlement").select("id").eq("student_id", student_id).eq("source", "read_ahead")
                    .eq("grant_ref", month["grant_ref"]).limit(1).execute()).data)
    done = bool((_app("unit_progress").select("status").eq("user_id", student_id).eq("unit_id", unit["id"])
                 .eq("status", "completed").limit(1).execute()).data)
    offer = None
    if granted and done and NEXT_TIER.get(tier):
        offer = {"to_tier": NEXT_TIER[tier], "per_month": lessons_per_month(NEXT_TIER[tier])}
    return {**out, "available": True, "lesson": {"unit_id": unit["id"], "title": unit.get("title")},
            "unlocked": granted, "completed": done, "offer": offer}


def request_read_ahead(student_id: str, course_id: str) -> dict:
    """POST /enrolment/read-ahead — unlock next month's first lesson now."""
    s = read_ahead(student_id, course_id)
    if not s["available"]:
        raise ReadAheadRefused(s["reason"] or "not_available")
    if s["unlocked"]:
        return s
    month = _month_grant(student_id)
    db.schema("app").rpc("grant_lesson_slots", {
        "p_student_id": student_id, "p_count": 1, "p_source": "read_ahead", "p_grant_ref": month["grant_ref"],
    }).execute()
    return read_ahead(student_id, course_id)
