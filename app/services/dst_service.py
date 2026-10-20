"""Clock-change warnings a week ahead — enrolment plan 6.5.

A pod meets at a fixed time in its own zone. When daylight saving starts or
ends — in the pod's zone or in a member's — the member's LOCAL time for the
session moves by an hour. Daily: for every live pod, compare each member's
local time of consecutive upcoming sessions; when it changes in the next
8 days, tell them once (email, plus push/WhatsApp when on):

    "From Sun 29 Mar your pod meets at 19:00 your time (was 18:00): the clocks change."

Once per person per changed session (`notice_log`, kind dst_warning).
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import email_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

AHEAD = timedelta(days=8)
LOOK = timedelta(days=22)


def _t(name: str):
    return db.schema("app").table(name)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _zone(tz: str | None):
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001
        return timezone.utc


def first_change(starts: list[datetime], tz: str) -> tuple[datetime, str, str] | None:
    """The first session whose local HH:MM differs from the one before it. Pure."""
    z = _zone(tz)
    prev = None
    for at in sorted(starts):
        hhmm = at.astimezone(z).strftime("%H:%M")
        if prev is not None and hhmm != prev:
            return at, prev, hhmm
        prev = hhmm
    return None


async def daily(now: datetime | None = None) -> dict:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    from app.services.notify_service import already, notify
    now = now or datetime.now(timezone.utc)
    out = {"warned": 0}
    pods = (_t("cohorts").select("id, name, status, tutor_id").eq("status", "active").execute()).data or []
    for pod in pods:
        try:
            sessions = [s for s in (_t("cohort_session").select("id, scheduled_at, status").eq("cohort_id", pod["id"])
                        .gte("scheduled_at", now.isoformat()).execute()).data or []
                        if s.get("status") == "scheduled" and _ts(s["scheduled_at"]) <= now + LOOK]
            if len(sessions) < 2:
                continue
            by_at = {_ts(s["scheduled_at"]): s["id"] for s in sessions}
            people = [m["user_id"] for m in (_t("cohort_memberships").select("user_id").eq("cohort_id", pod["id"])
                      .eq("status", "active").execute()).data or []]
            people += [pod["tutor_id"]] if pod.get("tutor_id") else []
            zones = _zones(people) if people else {}
            for uid in people:
                ch = first_change(list(by_at), zones.get(uid, "UTC"))
                if not ch or ch[0] - now > AHEAD:
                    continue
                at, was, now_hhmm = ch
                ref = f"{pod['id']}:{by_at[at]}"
                if already(uid, "dst_warning", ref):
                    continue
                when = local_time(at, zones.get(uid, "UTC"))
                body = f"From {when} your pod meets at {now_hhmm} your time (was {was}): the clocks change."
                await notify(uid, "dst_warning", ref, "Your pod's time moves by an hour", body, "/learn")
                to = _email_of(uid)
                if to:
                    await email_service.send_template_email(to, "dst_warning", {
                        "subject": "Your pod's time moves by an hour", "pod_name": pod.get("name") or "Your pod",
                        "when": when, "was": was, "now": now_hhmm, "web_base_url": settings.web_base_url,
                    }, tags=["dst_warning"])
                    # notify() released the claim if no push/WhatsApp went; hold it for the email.
                    if not already(uid, "dst_warning", ref):
                        try:
                            _t("notice_log").insert({"user_id": uid, "kind": "dst_warning", "ref": ref,
                                                     "channels": ["email"]}).execute()
                        except Exception:  # noqa: BLE001
                            pass
                out["warned"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("dst check for pod %s: %s", pod.get("id"), exc)
    return out
