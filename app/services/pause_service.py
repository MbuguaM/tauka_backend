"""Pausing (enrolment plan 4.4, §2c, migration §111).

Decided 2026-10-18 (developer):
  * A pause starts at the END OF THE PAID MONTH (§2c). Until then nothing
    changes: the learner keeps their pod and their sessions.
  * A paused month costs nothing and gives Free access. Billing stops through
    the same Stripe call as a cancellation (cancel at period end), so the
    subscription webhook moves the tier to Free when the month runs out.
    Lessons already unlocked stay unlocked (§2c).
  * At most 3 months. A week before the end the learner is reminded once;
    after it the pause LAPSES and the enrolment is cancelled (credits are
    forfeited then, as on any cancellation, §2e).
  * Coming back = paying again (a new checkout on the web). The learner goes
    to the WAIT POOL: first priority at the next formation, at their level, in
    whatever season. Make-ups cover their sessions until a pod forms.

Session credits are owed for OUR failures, so a pause keeps them (the
cancellation webhook skips the forfeit while a pause is open); a paused learner
is simply not grouped for make-ups.

Who can pause: a paying learner whose enrolment is paid, seat_confirmed,
wait_pool or in_pod. Someone holding an offer answers it first; a gift month
can't be paused (it is prepaid and simply ends).
"""

import logging
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta

from app.config import settings
from app.services import email_service, phase0_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MAX_MONTHS = 3
REMIND_BEFORE = timedelta(days=7)
PAUSABLE = ("paid", "seat_confirmed", "wait_pool", "in_pod")
OPEN = ("scheduled", "active")


class PauseRefused(Exception):
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
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def current(student_id: str) -> dict | None:
    rows = (_t("enrolment_pause").select("*").eq("student_id", student_id)
            .in_("status", list(OPEN)).limit(1).execute()).data or []
    return rows[0] if rows else None


def is_pausing(student_id: str) -> bool:
    """For the cancellation webhook: a scheduled or active pause is not a cancel."""
    try:
        return current(student_id) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("pause read failed for %s: %s", student_id, exc)
        return False


def view(student_id: str) -> dict:
    """GET /enrolment/pause — the open pause, or whether one can be asked for."""
    p = current(student_id)
    if p:
        return {"pause": p, "can_pause": False, "max_months": MAX_MONTHS}
    try:
        _check(student_id)
        return {"pause": None, "can_pause": True, "max_months": MAX_MONTHS}
    except PauseRefused as exc:
        return {"pause": None, "can_pause": False, "reason": exc.reason, "max_months": MAX_MONTHS}


def _check(student_id: str) -> dict:
    rows = (_t("student").select("tier, current_period_end, active_gift_id, stripe_subscription_id")
            .eq("id", student_id).limit(1).execute()).data or []
    st = rows[0] if rows else {}
    if (st.get("tier") or "free_tier") == "free_tier":
        raise PauseRefused("not_paying")
    if st.get("active_gift_id"):
        raise PauseRefused("gift")
    states = {r["state"] for r in (_t("enrolment_state").select("state")
              .eq("student_id", student_id).execute()).data or []}
    if "matched" in states:
        raise PauseRefused("answer_offers_first")
    if not states & set(PAUSABLE):
        raise PauseRefused("nothing_to_pause")
    return st


async def request(student_id: str, now: datetime | None = None) -> dict:
    """POST /enrolment/pause — from the end of the paid month, for up to 3 months."""
    now = now or _now()
    if current(student_id):
        raise PauseRefused("already_paused")
    st = _check(student_id)
    end = _ts(st.get("current_period_end"))
    effective = end if end and end > now else now
    row = {"student_id": student_id, "effective_at": effective.isoformat(),
           "ends_by": (effective + relativedelta(months=MAX_MONTHS)).isoformat()}
    try:
        created = (_t("enrolment_pause").insert(row).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise PauseRefused("already_paused") from exc
        raise
    if st.get("stripe_subscription_id"):
        # Stop billing exactly as a cancellation does. If Stripe refuses, undo
        # the pause rather than leave one that keeps charging.
        from app.services import account_service
        try:
            await account_service.change_subscription(student_id, "free_tier", None, None)
        except Exception:
            _t("enrolment_pause").delete().eq("id", created[0]["id"]).eq("status", "scheduled").execute()
            raise
    if effective <= now:
        await _start(created[0], now)
        return current(student_id) or created[0]
    return created[0]


async def withdraw(student_id: str) -> dict:
    """POST /enrolment/pause/withdraw — only before it starts; billing resumes."""
    p = current(student_id)
    if not p or p["status"] != "scheduled":
        raise PauseRefused("nothing_to_withdraw")
    st = ((_t("student").select("stripe_subscription_id").eq("id", student_id).limit(1)
           .execute()).data or [{}])[0]
    if st.get("stripe_subscription_id"):
        from app.services import account_service
        await account_service.reactivate_subscription(student_id)
    _t("enrolment_pause").update({"status": "withdrawn"}).eq("id", p["id"]).eq("status", "scheduled").execute()
    return {**p, "status": "withdrawn"}


# ── The lifecycle ────────────────────────────────────────────────────────────

async def _start(p: dict, now: datetime) -> None:
    """The paid month has ended: out of the pod, offers and make-up seats
    released, enrolment `paused`."""
    from app.services import offer_service
    sid = p["student_id"]

    # Open offers: declined, so their seats are re-offered at once.
    for o in (_t("match_offer").select("id").eq("student_id", sid)
              .eq("status", offer_service.OPEN_OFFER).execute()).data or []:
        await offer_service.decline(sid, o["id"])

    # The pod seat. CONFLICT-048: status, left_at, removal_reason only.
    seat = (_t("cohort_memberships").select("cohort_id").eq("user_id", sid)
            .eq("status", "active").limit(1).execute()).data or []
    left = seat[0]["cohort_id"] if seat else None
    if left:
        _t("cohort_memberships").update({
            "status": "paused", "left_at": now.isoformat(),
            "removal_reason": "paused by the learner (plan 4.4); seat refilled at the next checkpoint",
        }).eq("user_id", sid).eq("cohort_id", left).eq("status", "active").execute()

    # Make-up seats not yet taken.
    pending = (_t("makeup_session_participant").select("makeup_session_id")
               .eq("student_id", sid).eq("status", "pending").execute()).data or []
    if pending:
        live = {s["id"] for s in (_t("makeup_session").select("id, status")
                .in_("id", [x["makeup_session_id"] for x in pending]).execute()).data or []
                if s["status"] in ("proposed", "scheduled")}
        for mid in live:
            _t("makeup_session_participant").update({"status": "withdrawn"}) \
                .eq("makeup_session_id", mid).eq("student_id", sid).eq("status", "pending").execute()

    phase0_service.advance(sid, ("matched",), "wait_pool")
    phase0_service.advance(sid, PAUSABLE, "paused")
    _t("enrolment_pause").update({"status": "active", "started_at": now.isoformat(), "left_cohort_id": left}) \
        .eq("id", p["id"]).eq("status", "scheduled").execute()


def _lapse(p: dict, now: datetime) -> None:
    sid = p["student_id"]
    phase0_service.advance(sid, ("paused",), "cancelled")
    try:
        from app.services.makeup_service import forfeit_on_cancellation
        forfeit_on_cancellation(sid)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not forfeit credits for lapsed pause %s: %s", sid, exc)
    _t("enrolment_pause").update({"status": "lapsed", "ended_at": now.isoformat()}) \
        .eq("id", p["id"]).eq("status", "active").execute()


def resume_after_payment(student_id: str, now: datetime | None = None) -> bool:
    """Called when a paid subscription starts. An active pause ends; the
    learner goes to the wait pool (or back to `paid` if Phase 0 isn't done)."""
    now = now or _now()
    p = current(student_id)
    if not p or p["status"] != "active":
        return False
    phase0_service.advance(student_id, ("paused",), "seat_confirmed")
    phase0_service.advance(student_id, ("seat_confirmed",), "wait_pool")
    phase0_service.advance(student_id, ("paused",), "paid")      # Phase 0 not finished
    _t("enrolment_pause").update({"status": "resumed", "ended_at": now.isoformat()}) \
        .eq("id", p["id"]).eq("status", "active").execute()
    return True


async def _remind(p: dict) -> None:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    sid = p["student_id"]
    to = _email_of(sid)
    if to:
        await email_service.send_template_email(to, "pause_ending", {
            "subject": "Your Tauka pause ends next week",
            "ends": local_time(_ts(p["ends_by"]), _zones([sid]).get(sid, "UTC")),
            "web_base_url": settings.web_base_url,
        }, tags=["pause", "ending"])


async def daily(now: datetime | None = None) -> dict:
    """Start pauses whose paid month has ended, remind a week before the end,
    lapse the ones past 3 months."""
    now = now or _now()
    out = {"started": 0, "reminded": 0, "lapsed": 0}
    for p in (_t("enrolment_pause").select("*").in_("status", list(OPEN)).execute()).data or []:
        try:
            if p["status"] == "scheduled" and _ts(p["effective_at"]) <= now:
                await _start(p, now)
                out["started"] += 1
            elif p["status"] == "active" and _ts(p["ends_by"]) <= now:
                _lapse(p, now)
                out["lapsed"] += 1
            elif (p["status"] == "active" and not p.get("reminder_sent_at")
                  and _ts(p["ends_by"]) - now <= REMIND_BEFORE):
                await _remind(p)
                _t("enrolment_pause").update({"reminder_sent_at": now.isoformat()}).eq("id", p["id"]).execute()
                out["reminded"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("pause %s: %s", p.get("id"), exc)
    return out
