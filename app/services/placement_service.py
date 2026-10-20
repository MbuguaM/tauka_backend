"""Placement notices (enrolment plan 3.5, migrations §110, §110b).

When a pod goes live — or a learner accepts a seat in a pod that already is —
each member gets ONE email:

  * the pod, the home tutor's first name, how many learners;
  * the slot in the learner's own timezone and how often it meets;
  * the season and Ethiopic month it starts in;
  * the first session, and every generated session as an **.ics attachment**;
  * the pre-session checklist (device test, audio-only fallback, conduct,
    missed sessions, pausing).

Email is the channel: push is suspended in the app (FC-008) and WhatsApp (6.1)
isn't built. A pod channel invite waits for pod chat (Track 6.3 / 4.9).

ONCE. A `placement_notice` row is inserted before sending (the claim — a
parallel accept or a retried job can't mail twice) and stamped `sent_at` after
the provider accepts. A failed send deletes the claim; a claim left unsent for
an hour (a send abandoned by the request's time limit) is retried by the daily
job. Only memberships from the last 14 days are considered, so pods that
existed before this was built are never mailed out of the blue.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.core import ethiopic_calendar as ec
from app.services import email_service
from app.services.makeup_delivery import _email_of, _zones, local_time
from app.services.makeup_service import _ts
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

RECENT = timedelta(days=14)
STALE_CLAIM = timedelta(hours=1)

SEASON = {
    "tsedey": "Tsedey, the season of new growth",
    "bega": "Bega, the dry season",
    "belg": "Belg, the season of the short rains",
    "kiremt": "Kiremt, the season of the long rains",
}
CADENCE = {1: "once a month", 2: "twice a month", 3: "three times a month", 4: "every week"}


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first_names(ids: list[str]) -> dict[str, str]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = (_t("user_profiles").select("id, first_name").in_("id", ids).execute()).data or []
    return {r["id"]: r.get("first_name") or "your tutor" for r in rows}


# ── The calendar file ────────────────────────────────────────────────────────

def _ics_text(v: str) -> str:
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(pod: dict, sessions: list[dict], stamp: datetime, cancelled: list[dict] = ()) -> bytes:
    """An RFC 5545 calendar of the pod's generated sessions, in UTC so every
    calendar app shows them in the reader's own time. UIDs are the session ids,
    so a later file with more sessions updates rather than duplicates.

    `cancelled` (plan 4.5): sessions a time change cancelled, sent with the same
    UID, SEQUENCE:1 and STATUS:CANCELLED so a calendar that imported the first
    file removes them."""
    def utc(d: datetime) -> str:
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tauka//Pod sessions//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    summary = _ics_text(f"Tauka pod session · {pod.get('name') or 'your pod'}")
    for s in cancelled:
        start = _ts(s["scheduled_at"])
        end = start + timedelta(minutes=int(s.get("duration_minutes") or 50))
        lines += ["BEGIN:VEVENT", f"UID:{s['id']}@tauka.io", f"DTSTAMP:{utc(stamp)}", "SEQUENCE:1",
                  "STATUS:CANCELLED", f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}",
                  f"SUMMARY:{summary} (moved)", "END:VEVENT"]
    for s in sessions:
        start = _ts(s["scheduled_at"])
        end = start + timedelta(minutes=int(s.get("duration_minutes") or 50))
        lines += ["BEGIN:VEVENT", f"UID:{s['id']}@tauka.io", f"DTSTAMP:{utc(stamp)}",
                  f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}", f"SUMMARY:{summary}",
                  "DESCRIPTION:" + _ics_text("Join from the Learn tab in the Tauka app a few minutes early."),
                  "BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY",
                  "DESCRIPTION:" + summary, "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ── The email ────────────────────────────────────────────────────────────────

def _slot_line(first: datetime, tz: str, per_month: int | None) -> str:
    """'Saturdays at 10:00 (America/New_York), three times a month'."""
    try:
        z = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        z, tz = timezone.utc, "UTC"
    t = first.astimezone(z)
    return f"{t:%A}s at {t:%H:%M} ({tz}), {CADENCE.get(per_month or 3, f'{per_month} times a month')}"


def content(pod: dict, sessions: list[dict], tutor_name: str, members: int, tz: str) -> dict:
    first = _ts(sessions[0]["scheduled_at"]) if sessions else None
    start_day = (first or _now()).date()
    season, year = ec.season_of(start_day)
    month = ec.from_gregorian(start_day)
    return {
        "subject": f"You're in a pod: {pod.get('name') or 'your Tauka pod'}",
        "pod_name": pod.get("name") or "Your pod",
        "tutor_name": tutor_name,
        "members": members,
        "first_session": local_time(first, tz) if first else None,
        "slot": _slot_line(first, tz, pod.get("sessions_per_month")) if first else None,
        "minutes": int((sessions[0].get("duration_minutes") if sessions else None) or 50),
        "season": SEASON.get(season, season.title()),
        "ethiopic_month": f"{month.month_name.title()} {year}",
        "session_count": len(sessions),
        "web_base_url": settings.web_base_url,
    }


def _upcoming(pod_id: str, now: datetime) -> list[dict]:
    # app.cohort_session (tauka-flutter): generated by app.generate_cohort_sessions.
    rows = (_t("cohort_session").select("id, scheduled_at, duration_minutes, status")
            .eq("cohort_id", pod_id).gte("scheduled_at", now.isoformat()).order("scheduled_at")
            .execute()).data or []
    return [r for r in rows if r.get("status") == "scheduled"]


async def _send_one(pod: dict, sessions: list[dict], tutor_name: str, members: int,
                    student_id: str, tz: str, now: datetime) -> bool:
    to = _email_of(student_id)
    if not to:
        return False
    attachments = [{"filename": "tauka-pod.ics", "content": ics(pod, sessions, now),
                    "content_type": "text/calendar"}] if sessions else None
    res = await email_service.send_template_email(
        to, "placement", content(pod, sessions, tutor_name, members, tz),
        tags=["placement"], attachments=attachments)
    return (res or {}).get("status") != "failed"


async def notify_cohort(cohort_id: str, now: datetime | None = None) -> int:
    """Email every member of a LIVE pod who hasn't had their placement notice.
    Called after an accept and by the daily job. Returns how many were sent."""
    now = now or _now()
    pods = (_t("cohorts").select("id, name, tutor_id, status, sessions_per_month")
            .eq("id", cohort_id).limit(1).execute()).data or []
    if not pods or pods[0].get("status") != "active":
        return 0
    pod = pods[0]
    members = (_t("cohort_memberships").select("user_id, joined_at")
               .eq("cohort_id", cohort_id).eq("status", "active").execute()).data or []
    recent = [m["user_id"] for m in members if not m.get("joined_at") or _ts(m["joined_at"]) >= now - RECENT]
    if not recent:
        return 0
    claims = {r["student_id"]: r for r in (_t("placement_notice").select("student_id, sent_at, claimed_at")
              .eq("cohort_id", cohort_id).execute()).data or []}

    todo = []
    for sid in recent:
        c = claims.get(sid)
        if c is None:
            try:
                _t("placement_notice").insert({"cohort_id": cohort_id, "student_id": sid,
                                               "claimed_at": now.isoformat()}).execute()
            except Exception as exc:  # noqa: BLE001
                # The primary key: someone else claimed it a moment ago.
                if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                    logger.error("placement claim for %s failed: %s", sid, exc)
                continue
            todo.append(sid)
        elif not c.get("sent_at") and _ts(c["claimed_at"]) <= now - STALE_CLAIM:
            # Re-claim a stale one, conditionally, so only one retry wins.
            got = (_t("placement_notice").update({"claimed_at": now.isoformat()})
                   .eq("cohort_id", cohort_id).eq("student_id", sid).is_("sent_at", "null")
                   .eq("claimed_at", c["claimed_at"]).execute()).data or []
            if got:
                todo.append(sid)
    if not todo:
        return 0

    sessions = _upcoming(cohort_id, now)
    tutor_name = _first_names([pod.get("tutor_id")]).get(pod.get("tutor_id"), "your tutor")
    zones = _zones(todo)
    sent = 0
    sent_to: list[str] = []
    for sid in todo:
        ok = False
        try:
            ok = await _send_one(pod, sessions, tutor_name, len(members), sid, zones.get(sid, "UTC"), now)
        except Exception as exc:  # noqa: BLE001
            logger.error("placement email for %s failed: %s", sid, exc)
        if ok:
            _t("placement_notice").update({"sent_at": _now().isoformat()}) \
                .eq("cohort_id", cohort_id).eq("student_id", sid).execute()
            sent += 1
            sent_to.append(sid)
        else:
            # Release the claim so tomorrow's job tries again.
            _t("placement_notice").delete().eq("cohort_id", cohort_id).eq("student_id", sid) \
                .is_("sent_at", "null").execute()
    if sent_to:
        await _notify_tutor(pod, sessions, sent_to, len(members), new_pod=len(sent_to) == len(members))
    return sent


async def _notify_tutor(pod: dict, sessions: list[dict], new_ids: list[str], members: int, new_pod: bool) -> None:
    """Plan 3.5, the tutor's half: one email per batch of learners just told
    they're in the pod (each learner is told once, so the tutor hears of each
    once). First names only. Best-effort: a failure is logged, not retried."""
    tutor = pod.get("tutor_id")
    to = _email_of(tutor) if tutor else None
    if not to:
        return
    tz = _zones([tutor]).get(tutor, "UTC")
    first = _ts(sessions[0]["scheduled_at"]) if sessions else None
    names = [n for n in (_first_names(new_ids).get(i) for i in new_ids) if n] or ["A learner"]
    name = pod.get("name") or "Your pod"
    try:
        await email_service.send_template_email(to, "placement_tutor", {
            "subject": f"{name} is live" if new_pod else f"New learner{'s' if len(new_ids) != 1 else ''} in {name}",
            "pod_name": name, "names": names, "members": members, "new_pod": new_pod,
            "first_session": local_time(first, tz) if first else None,
            "slot": _slot_line(first, tz, pod.get("sessions_per_month")) if first else None,
            "minutes": int((sessions[0].get("duration_minutes") if sessions else None) or 50),
            "web_base_url": settings.web_base_url,
        }, tags=["placement", "tutor"])
    except Exception as exc:  # noqa: BLE001
        logger.error("tutor placement email for %s failed: %s", pod.get("id"), exc)


async def daily(now: datetime | None = None) -> int:
    """Catch-up: live pods with a membership from the last 14 days."""
    now = now or _now()
    rows = (_t("cohort_memberships").select("cohort_id, joined_at").eq("status", "active")
            .gte("joined_at", (now - RECENT).isoformat()).execute()).data or []
    sent = 0
    for pod_id in sorted({r["cohort_id"] for r in rows}):
        try:
            sent += await notify_cohort(pod_id, now)
        except Exception as exc:  # noqa: BLE001
            logger.error("placement notices for pod %s failed: %s", pod_id, exc)
    return sent
