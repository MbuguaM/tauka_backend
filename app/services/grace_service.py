"""Missed payment: 7-day grace, then lapsed — enrolment plan 2.3a / 2.3b, §2c,
migration §126.

    invoice.payment_failed   -> grace opens (day 0 email)
    daily, day 3 and day 6   -> reminder emails
    daily, day 7 unpaid      -> lapsed: no live-session room tokens, not grouped
                                for make-ups, no clinic or drop-in bookings.
                                The pod seat is HELD.
    invoice.paid             -> resolved 'paid': everything back
    subscription cancelled   -> resolved 'cancelled'

Grace keeps what the learner has: sessions go on and unlocked lessons stay.
The new month's lessons are only ever granted on invoice.paid, so nothing is
granted here. Every email asks the learner to download unlocked lessons they
haven't got on the device yet (2.3b): after a cancellation the signed links
stop.

WhatsApp and the M-Pesa prompt wait on L-011 and plan 2.4.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import email_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

GRACE_DAYS = 7


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def current(student_id: str) -> dict | None:
    rows = (_t("payment_grace").select("*").eq("student_id", student_id).is_("resolved_at", "null")
            .limit(1).execute()).data or []
    # Only a real row list counts (an unconfigured client must never read as "lapsed").
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


def is_lapsed(student_id: str) -> bool:
    """For gates: lapsed (grace over, still unpaid). Fails open on a read error,
    so an outage never locks a paying learner out of their session."""
    try:
        g = current(student_id)
        return bool(g and g.get("lapsed_at"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("grace read failed for %s: %s", student_id, exc)
        return False


def lapsed_ids(student_ids: list[str]) -> set[str]:
    if not student_ids:
        return set()
    try:
        rows = (_t("payment_grace").select("student_id, lapsed_at").in_("student_id", list(student_ids))
                .is_("resolved_at", "null").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("grace read failed: %s", exc)
        return set()
    if not isinstance(rows, list):
        return set()
    return {r["student_id"] for r in rows if isinstance(r, dict) and r.get("lapsed_at")}


def view(student_id: str, now: datetime | None = None) -> dict:
    """GET /enrolment/grace — none | grace (with the day access pauses) | lapsed."""
    g = current(student_id)
    if not g:
        return {"state": "none"}
    if g.get("lapsed_at"):
        return {"state": "lapsed", "since": g["lapsed_at"]}
    return {"state": "grace", "until": (_ts(g["started_at"]) + timedelta(days=GRACE_DAYS)).isoformat()}


# ── Stripe events (called from stripe_service) ──────────────────────────────

async def start(student_id: str, invoice_id: str | None, now: datetime | None = None) -> bool:
    """A payment failed. Idempotent: one open grace per learner."""
    now = now or _now()
    if current(student_id):
        return False
    try:
        _t("payment_grace").insert({"student_id": student_id, "invoice_id": invoice_id,
                                    "started_at": now.isoformat()}).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return False
        raise
    await _mail(student_id, "started", now + timedelta(days=GRACE_DAYS))
    return True


def resolve(student_id: str, resolution: str, now: datetime | None = None) -> bool:
    now = now or _now()
    got = (_t("payment_grace").update({"resolved_at": now.isoformat(), "resolution": resolution})
           .eq("student_id", student_id).is_("resolved_at", "null").execute()).data or []
    return bool(got)


def leave_pod_on_cancel(student_id: str, now: datetime | None = None) -> str | None:
    """A cancelled subscription ends the pod seat (§126): membership `dropped`,
    enrolment `cancelled`, so the seat reaches refills (plan 3.6). Not for a
    pause, which has its own path. CONFLICT-048: status, left_at, removal_reason only."""
    from app.services import phase0_service
    now = now or _now()
    seat = (_t("cohort_memberships").select("cohort_id").eq("user_id", student_id)
            .eq("status", "active").limit(1).execute()).data or []
    left = seat[0]["cohort_id"] if seat else None
    if left:
        _t("cohort_memberships").update({
            "status": "dropped", "left_at": now.isoformat(),
            "removal_reason": "subscription ended (plan 2.3a); seat refilled",
        }).eq("user_id", student_id).eq("cohort_id", left).eq("status", "active").execute()
    phase0_service.advance(student_id, ("paid", "seat_confirmed", "matched", "wait_pool", "in_pod"), "cancelled")
    return left


# ── the daily run ───────────────────────────────────────────────────────────

async def daily(now: datetime | None = None) -> dict:
    now = now or _now()
    out = {"reminded": 0, "lapsed": 0}
    for g in (_t("payment_grace").select("*").is_("resolved_at", "null").execute()).data or []:
        try:
            day = (now - _ts(g["started_at"])).total_seconds() / 86400
            until = _ts(g["started_at"]) + timedelta(days=GRACE_DAYS)
            if day >= GRACE_DAYS and not g.get("lapsed_at"):
                got = (_t("payment_grace").update({"lapsed_at": now.isoformat()}).eq("id", g["id"])
                       .is_("lapsed_at", "null").is_("resolved_at", "null").execute()).data or []
                if got:
                    await _mail(g["student_id"], "lapsed", until)
                    out["lapsed"] += 1
            elif day >= 6 and not g.get("reminded_6_at"):
                _t("payment_grace").update({"reminded_6_at": now.isoformat()}).eq("id", g["id"]).execute()
                await _mail(g["student_id"], "last_day", until)
                out["reminded"] += 1
            elif 3 <= day < 6 and not g.get("reminded_3_at"):
                _t("payment_grace").update({"reminded_3_at": now.isoformat()}).eq("id", g["id"]).execute()
                await _mail(g["student_id"], "reminder", until)
                out["reminded"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("grace %s: %s", g.get("id"), exc)
    return out


async def _mail(student_id: str, kind: str, until: datetime) -> None:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    to = _email_of(student_id)
    if not to:
        return
    subjects = {"started": "Your payment didn't go through", "reminder": "Your payment still needs updating",
                "last_day": "Tomorrow your live sessions pause", "lapsed": "Your live sessions are paused"}
    try:
        await email_service.send_template_email(to, "grace", {
            "subject": subjects[kind], "kind": kind,
            "until": local_time(until, _zones([student_id]).get(student_id, "UTC")),
            "web_base_url": settings.web_base_url}, tags=["grace", kind])
    except Exception as exc:  # noqa: BLE001
        logger.error("grace email failed: %s", exc)
