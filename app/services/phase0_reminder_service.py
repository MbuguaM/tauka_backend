"""Phase 0 reminders by email — enrolment plan 1.3, migration §127.

A learner still in Phase 0 (state phase0 or paid, not finished, reminders on)
gets ONE email a day at most, chosen in this order:

    deadline_3   0-3 days before the target intake's lesson deadline
    deadline_14  4-14 days before it
    weekly_<n>   every 7th day since they started Phase 0

Each is claimed first (`phase0_reminder`, unique per learner, intake and kind)
and the claim removed if the send fails, so none goes twice and a failure is
retried tomorrow. The row is synced first (`phase0_service._sync`), so a learner
who finished yesterday gets nothing today.

The off link is an HMAC of the learner id (nothing stored); it sets
`enrolment_state.reminders_off_at`. WhatsApp waits on L-011.
"""

import hashlib
import hmac
import logging
from datetime import date, datetime, timezone

from app.config import settings
from app.core.ethiopic_calendar import MONTHS
from app.services import email_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

IN_PHASE0 = ("phase0", "paid")


def _t(name: str):
    return db.schema("app").table(name)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def off_token(student_id: str) -> str:
    key = (settings.supabase_jwt_secret or "dev").encode()
    return hmac.new(key, f"phase0-reminders:{student_id}".encode(), hashlib.sha256).hexdigest()[:32]


def off_url(student_id: str) -> str:
    return f"{settings.api_base_url}/enrolment/reminders/off?s={student_id}&t={off_token(student_id)}"


def turn_off(student_id: str, token: str) -> bool:
    if not hmac.compare_digest(off_token(student_id), token or ""):
        return False
    _t("enrolment_state").update({"reminders_off_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("student_id", student_id).is_("reminders_off_at", "null").execute()
    return True


def pick(started: date, deadline: date | None, today: date) -> str | None:
    """Which reminder today, if any. Pure."""
    if deadline is not None:
        left = (deadline - today).days
        if 0 <= left <= 3:
            return "deadline_3"
        if 3 < left <= 14:
            return "deadline_14"
    days = (today - started).days
    if days >= 7 and days % 7 == 0 and days // 7 < 100:
        return f"weekly_{days // 7}"
    return None


async def daily(today: date | None = None) -> dict:
    from app.services import phase0_service
    today = today or _today()
    out = {"sent": 0}
    rows = (_t("enrolment_state").select("*").in_("state", list(IN_PHASE0)).execute()).data or []
    for row in rows:
        if row.get("phase0_completed_at") or row.get("reminders_off_at"):
            continue
        try:
            row = phase0_service._sync(row)
            if row["state"] not in IN_PHASE0 or row.get("phase0_completed_at") or not row.get("target_intake_id"):
                continue
            target = phase0_service._present(row).get("target_intake") or {}
            deadline = _d(target["cutoff_closes"]) if target.get("cutoff_closes") else None
            kind = pick(_d(row.get("phase0_started_at") or row["created_at"]), deadline, today)
            if not kind:
                continue
            try:
                _t("phase0_reminder").insert({"student_id": row["student_id"], "intake_id": row["target_intake_id"],
                                              "kind": kind}).execute()
            except Exception as exc:  # noqa: BLE001
                if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                    continue
                raise
            if await _send(row["student_id"], kind, target, deadline, today):
                out["sent"] += 1
            else:
                _t("phase0_reminder").delete().eq("student_id", row["student_id"]) \
                    .eq("intake_id", row["target_intake_id"]).eq("kind", kind).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("phase0 reminder for %s: %s", row.get("student_id"), exc)
    return out


async def _send(student_id: str, kind: str, target: dict, deadline: date | None, today: date) -> bool:
    from app.services.makeup_delivery import _email_of
    to = _email_of(student_id)
    if not to:
        return False
    month = target.get("ethiopic_month")
    intake = f"{MONTHS[month - 1].title()} {target.get('ec_year')}" if isinstance(month, int) and 1 <= month <= 13 \
        else "your intake"
    subject = {"deadline_3": f"3 days left to join the {intake} pods",
               "deadline_14": f"Two weeks to join the {intake} pods"}.get(kind, "Your Phase 0 this week")
    try:
        res = await email_service.send_template_email(to, "phase0_reminder", {
            "subject": subject, "kind": "weekly" if kind.startswith("weekly") else kind, "intake": intake,
            "deadline": f"{deadline:%a} {deadline.day} {deadline:%b}" if deadline else None,
            "days_left": (deadline - today).days if deadline else None,
            "off_url": off_url(student_id), "web_base_url": settings.web_base_url,
        }, tags=["phase0", kind.split("_")[0]])
        ok = (res or {}).get("status") != "failed"
        if ok and kind.startswith("deadline") and deadline:
            # Plan 6.1: the cutoff warnings also go by WhatsApp when on (L-011).
            from app.services.notify_service import notify
            await notify(student_id, "phase0_deadline", f"{kind}:{deadline.isoformat()}", subject,
                         f"Finish Phase 0 by {deadline:%d %b}.", "/phase0",
                         whatsapp=("phase0_deadline", [intake, f"{deadline:%d %b}"]))
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.error("phase0 reminder email failed: %s", exc)
        return False
