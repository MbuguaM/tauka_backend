"""Catch-up for a learner who missed a pod session (enrolment plan 4.8, §114).

Decided 2026-10-18 (developer):
  * A pod session RUNS with any learner present. "Runs with >= 2" reads as
    "never cancelled for low turnout"; nothing is built for it.
  * An absent (or excused) learner gets the tutor's session notes and an
    ASYNC TASK: the practice of the lesson the session covered
    (`cohort_session.lesson_ref`), using exercises that already exist — no
    extra tutor work.
  * Tutor no-shows wait for the standby roster (plan 8.6).

WHEN. Notes are written after the session, so the email waits for them: it
goes once the tutor has written notes, or 48 hours after the session either
way. Only sessions from the last 7 days, and only to learners still in the pod.

ONCE. A `catchup_notice` row is claimed before sending and removed if the send
fails, as for placement notices (§110).
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import email_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(days=7)
WAIT_FOR_NOTES = timedelta(hours=48)
MISSED = ("absent", "excused")


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def ready(session: dict, now: datetime) -> bool:
    """Ended, within the lookback, and either written up or 48 h old."""
    start = _ts(session["scheduled_at"])
    end = start + timedelta(minutes=int(session.get("duration_minutes") or 50))
    if session.get("status") == "cancelled" or end > now or now - start > LOOKBACK:
        return False
    return bool((session.get("tutor_notes") or "").strip()) or now - end >= WAIT_FOR_NOTES


def _lesson_title(course_id: str | None, lesson: int | None) -> str | None:
    if not course_id or lesson is None:
        return None
    # app.unit (tauka-flutter): content units carry `level`; preliminary ones don't (§85.4).
    rows = (_t("unit").select("title, level, deleted_at").eq("course_id", course_id)
            .eq("unit_order", lesson).execute()).data or []
    rows = [r for r in rows if r.get("level") is not None and not r.get("deleted_at")]
    return rows[0].get("title") if rows else None


def content(session: dict, pod: dict, lesson_title: str | None, when: str, now: datetime) -> dict:
    rec_until = _ts(session.get("recording_expires_at"))
    lesson = session.get("lesson_ref")
    return {
        "subject": "What you missed in your pod session",
        "pod_name": pod.get("name") or "Your pod",
        "when": when,
        "notes": (session.get("tutor_notes") or "").strip() or None,
        "lesson": lesson,
        "lesson_title": lesson_title,
        "recording_until": (rec_until.strftime("%d %b %Y")
                            if session.get("recording_path") and rec_until and rec_until > now else None),
        "web_base_url": settings.web_base_url,
    }


async def daily(now: datetime | None = None) -> int:
    from app.services.makeup_delivery import _email_of, _zones, local_time

    now = now or _now()
    # app.cohort_session (tauka-flutter): the last week's sittings.
    sessions = [s for s in (_t("cohort_session").select(
        "id, cohort_id, scheduled_at, duration_minutes, status, lesson_ref, tutor_notes, recording_path, "
        "recording_expires_at").gte("scheduled_at", (now - LOOKBACK).isoformat()).execute()).data or []
        if ready(s, now)]
    if not sessions:
        return 0
    by_id = {s["id"]: s for s in sessions}
    # app.cohort_session_attendance (tauka-flutter): marked by the tutor.
    missed = [a for a in (_t("cohort_session_attendance").select("cohort_session_id, user_id, status")
              .in_("cohort_session_id", list(by_id)).execute()).data or [] if a.get("status") in MISSED]
    if not missed:
        return 0
    done = {(r["cohort_session_id"], r["student_id"]) for r in (_t("catchup_notice")
            .select("cohort_session_id, student_id").in_("cohort_session_id", list(by_id)).execute()).data or []}
    pods = {p["id"]: p for p in (_t("cohorts").select("id, name, course_id")
            .in_("id", sorted({s["cohort_id"] for s in sessions})).execute()).data or []}
    still_in = {(m["cohort_id"], m["user_id"]) for m in (_t("cohort_memberships").select("cohort_id, user_id")
                .in_("cohort_id", list(pods)).eq("status", "active").execute()).data or []}
    zones = _zones(sorted({a["user_id"] for a in missed}))
    titles: dict[tuple, str | None] = {}

    sent = 0
    for a in missed:
        s = by_id[a["cohort_session_id"]]
        sid = a["user_id"]
        if (s["id"], sid) in done or (s["cohort_id"], sid) not in still_in:
            continue
        try:
            _t("catchup_notice").insert({"cohort_session_id": s["id"], "student_id": sid}).execute()
        except Exception as exc:  # noqa: BLE001
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                logger.error("catch-up claim for %s failed: %s", sid, exc)
            continue
        pod = pods.get(s["cohort_id"], {})
        key = (pod.get("course_id"), s.get("lesson_ref"))
        if key not in titles:
            titles[key] = _lesson_title(*key)
        ok = False
        to = _email_of(sid)
        if to:
            res = await email_service.send_template_email(
                to, "catchup", content(s, pod, titles[key], local_time(_ts(s["scheduled_at"]), zones.get(sid, "UTC")),
                                       now), tags=["catchup"])
            ok = (res or {}).get("status") != "failed"
        if ok:
            sent += 1
        else:
            _t("catchup_notice").delete().eq("cohort_session_id", s["id"]).eq("student_id", sid).execute()
    return sent
