"""Make-ups after booking: the room, the screens, attendance, the emails
(enrolment plan §2f items 2-5, migration §103).

`makeup_service` decides who waits, groups them and books them. This module is
what happens once a make-up has a tutor and a time.

THE ROOM (item 3). On booking, a `video_session` is created for the tutor with
its learners as participants — the same shape a tutor-made session has, so the
app's existing video page joins it. tauka-python writing that tauka-flutter
table is a declared shared write (CONFLICT-055). The room URL comes from Daily
when it can; otherwise it is left empty and the tutor pastes a link in the app,
which the existing "tutors manage own sessions" policy already allows.

THE SCREENS (items 2, 4). `mine()` answers both: a tutor gets their make-ups
with each learner's name and seat; a learner gets their next session, any make-up
still being arranged, and their credit balance. A learner never sees the other
learners' names — the group size only.

ATTENDANCE. Only the session's tutor (or an ops admin), only once it has
started (§103 trigger). Marking the last seat completes the session (§103).

EMAIL, NOT PUSH (item 5). Push is suspended in the app (FC-008). A booked notice
goes out once (`booked_notice_sent_at`) and a reminder the day before, once
(`reminder_sent_at`), each time written in the recipient's own timezone.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import calling_service, email_service
from app.services import hierarchy_service as hs
from app.services.makeup_service import _names, _ts, credit_balance
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MARKS = ("attended", "absent", "excused")
RECENT = timedelta(days=14)        # how far back a tutor's list reaches (to mark late)
REMIND_AHEAD = timedelta(hours=24)


class MakeupRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session(session_id: str) -> dict:
    rows = (_t("makeup_session").select(
        "id, course_id, tutor_id, scheduled_at, due_by, duration_minutes, status, video_session_id, "
        "booked_notice_sent_at, reminder_sent_at"
    ).eq("id", session_id).limit(1).execute()).data or []
    if not rows:
        raise MakeupRefused("no_such_session")
    return rows[0]


def _seats(session_ids: list[str]) -> list[dict]:
    if not session_ids:
        return []
    return [s for s in (_t("makeup_session_participant").select(
        "makeup_session_id, student_id, funded_by, status"
    ).in_("makeup_session_id", session_ids).execute()).data or [] if s["status"] != "withdrawn"]


# ── the room (item 3) ────────────────────────────────────────────────────────

async def provision_room(session_id: str) -> str | None:
    """Create the video session for a booked make-up, once. Best effort."""
    s = _session(session_id)
    if s["status"] != "scheduled" or s.get("video_session_id") or not s.get("tutor_id"):
        return s.get("video_session_id")
    start = _ts(s["scheduled_at"])
    end = start + timedelta(minutes=int(s["duration_minutes"]))
    url = await calling_service.create_daily_room(
        f"mu-{session_id.replace('-', '')}", int((end + timedelta(hours=1)).timestamp()))
    try:
        # app.video_session (tauka-flutter, video domain): tutor_id, title,
        # scheduled_at, duration_minutes, video_call_url. CONFLICT-055.
        created = _t("video_session").insert({
            "tutor_id": s["tutor_id"],
            "title": "Make-up session",
            "scheduled_at": start.isoformat(),
            "duration_minutes": int(s["duration_minutes"]),
            "video_call_url": url,
        }).execute()
        vs_id = (created.data or [{}])[0].get("id")
        if not vs_id:
            return None
        seats = _seats([session_id])
        if seats:
            # app.video_session_participant: (session_id, student_id) only.
            _t("video_session_participant").insert(
                [{"session_id": vs_id, "student_id": x["student_id"]} for x in seats]).execute()
        _t("makeup_session").update({"video_session_id": vs_id}).eq("id", session_id).execute()
        return vs_id
    except Exception as exc:  # noqa: BLE001
        logger.error("video room for make-up %s failed: %s", session_id, exc)
        return None


# ── the screens (items 2, 4) ─────────────────────────────────────────────────

def _video(ids: list[str]) -> dict[str, dict]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {r["id"]: r for r in (_t("video_session").select("id, video_call_url")
                                  .in_("id", ids).execute()).data or []}


def for_tutor(tutor_id: str, now: datetime | None = None) -> list[dict]:
    now = now or _now()
    rows = (_t("makeup_session").select(
        "id, scheduled_at, duration_minutes, status, video_session_id"
    ).eq("tutor_id", tutor_id).in_("status", ["scheduled", "in_progress", "completed"])
        .gte("scheduled_at", (now - RECENT).isoformat()).order("scheduled_at").execute()).data or []
    seats = _seats([r["id"] for r in rows])
    names = _names(x["student_id"] for x in seats)
    video = _video([r.get("video_session_id") for r in rows])
    out = []
    for r in rows:
        mine = [x for x in seats if x["makeup_session_id"] == r["id"]]
        started = _ts(r["scheduled_at"]) <= now
        out.append({
            "id": r["id"], "scheduled_at": r["scheduled_at"], "duration_minutes": r["duration_minutes"],
            "status": r["status"], "video_session_id": r.get("video_session_id"),
            "has_room": bool((video.get(r.get("video_session_id")) or {}).get("video_call_url")),
            "can_mark": started and r["status"] != "cancelled",
            "learners": [{"student_id": x["student_id"], "name": names.get(x["student_id"]),
                          "status": x["status"]} for x in mine],
        })
    # Unmarked past sessions first: they block the learner's next seat (§2f item 9).
    return sorted(out, key=lambda o: (not (o["can_mark"] and any(l["status"] == "pending" for l in o["learners"])),
                                      o["scheduled_at"]))


def for_learner(student_id: str, now: datetime | None = None) -> dict:
    now = now or _now()
    seats = [s for s in (_t("makeup_session_participant").select(
        "makeup_session_id, funded_by, status"
    ).eq("student_id", student_id).execute()).data or [] if s["status"] == "pending"]
    sessions = []
    if seats:
        sessions = (_t("makeup_session").select(
            "id, tutor_id, scheduled_at, due_by, duration_minutes, status, video_session_id"
        ).in_("id", [s["makeup_session_id"] for s in seats])
            .in_("status", ["proposed", "scheduled", "in_progress"]).execute()).data or []
    group = {}
    for x in _seats([s["id"] for s in sessions]):
        group[x["makeup_session_id"]] = group.get(x["makeup_session_id"], 0) + 1
    tutors = _names(s.get("tutor_id") for s in sessions)

    def view(s: dict) -> dict:
        return {"id": s["id"], "scheduled_at": s.get("scheduled_at"), "due_by": s["due_by"],
                "duration_minutes": s["duration_minutes"], "status": s["status"],
                "group_size": group.get(s["id"], 1), "tutor_name": tutors.get(s.get("tutor_id")),
                "video_session_id": s.get("video_session_id")}

    booked = sorted((s for s in sessions if s["status"] in ("scheduled", "in_progress")
                     and _ts(s["scheduled_at"]) + timedelta(minutes=int(s["duration_minutes"])) > now),
                    key=lambda s: s["scheduled_at"])
    arranging = sorted((s for s in sessions if s["status"] == "proposed"), key=lambda s: s["due_by"])
    return {
        "next": view(booked[0]) if booked else None,
        "upcoming": [view(s) for s in booked[1:]],
        "arranging": [view(s) for s in arranging],
        "credits": credit_balance(student_id),
    }


def mine(user_id: str) -> dict:
    """GET /makeups/mine — whichever halves apply to the caller."""
    is_tutor = bool((_t("tutor").select("id").eq("id", user_id).limit(1).execute()).data)
    return {
        "as_tutor": for_tutor(user_id) if is_tutor else None,
        "as_learner": None if is_tutor else for_learner(user_id),
    }


# ── attendance ───────────────────────────────────────────────────────────────

def mark_attendance(actor: str, session_id: str, marks: dict[str, str]) -> list[dict]:
    s = _session(session_id)
    if actor != s.get("tutor_id") and not hs.admin_can(actor, "ops"):
        raise MakeupRefused("not_allowed")
    if s["status"] not in ("scheduled", "in_progress", "completed"):
        raise MakeupRefused("not_scheduled")
    if _ts(s["scheduled_at"]) > _now():
        raise MakeupRefused("not_started")
    if any(v not in MARKS for v in marks.values()):
        raise MakeupRefused("invalid_mark")
    seated = {x["student_id"] for x in _seats([session_id])}
    if any(sid not in seated for sid in marks):
        raise MakeupRefused("not_in_session")
    for sid, status in marks.items():
        try:
            _t("makeup_session_participant").update({"status": status}) \
                .eq("makeup_session_id", session_id).eq("student_id", sid).execute()
        except Exception as exc:  # noqa: BLE001
            if "has started" in str(exc):
                raise MakeupRefused("not_started") from exc
            raise
    return for_tutor(s["tutor_id"])


# ── email (item 5) ───────────────────────────────────────────────────────────

def _email_of(user_id: str) -> str | None:
    try:
        return db.auth.admin.get_user_by_id(user_id).user.email
    except Exception as exc:  # noqa: BLE001
        logger.warning("no email for %s: %s", user_id, exc)
        return None


def _zones(ids: list[str]) -> dict[str, str]:
    rows = (_t("user_profiles").select("id, timezone").in_("id", ids).execute()).data or []
    return {r["id"]: r.get("timezone") or "UTC" for r in rows}


def local_time(at: datetime, tz: str) -> str:
    """'Tue 19 Jan, 18:00 (Africa/Nairobi)'. Falls back to UTC on a bad zone."""
    try:
        z = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        z, tz = timezone.utc, "UTC"
    t = at.astimezone(z)
    return f"{t:%a} {t.day} {t:%b}, {t:%H:%M} ({tz})"


async def _send(s: dict, kind: str) -> int:
    seats = _seats([s["id"]])
    people = [x["student_id"] for x in seats] + ([s["tutor_id"]] if s.get("tutor_id") else [])
    zones = _zones(people)
    start = _ts(s["scheduled_at"])
    sent = 0
    for uid in people:
        to = _email_of(uid)
        if not to:
            continue
        is_tutor = uid == s.get("tutor_id")
        res = await email_service.send_template_email(to, "makeup", {
            "subject": ("Make-up session booked" if kind == "booked" else "Your make-up session is tomorrow")
                       + (" (tutor)" if is_tutor else ""),
            "kind": kind, "audience": "tutor" if is_tutor else "learner",
            "when": local_time(start, zones.get(uid, "UTC")),
            "minutes": s["duration_minutes"], "group_size": len(seats),
            "web_base_url": settings.web_base_url,
        }, tags=["makeup", kind])
        sent += 1 if (res or {}).get("status") != "failed" else 0
    return sent


async def after_booking(session_id: str) -> None:
    """Background, after an admin books a make-up: the room, then the notice."""
    try:
        await provision_room(session_id)
        s = _session(session_id)
        if s["status"] == "scheduled" and not s.get("booked_notice_sent_at"):
            await _send(s, "booked")
            _t("makeup_session").update({"booked_notice_sent_at": _now().isoformat()}) \
                .eq("id", session_id).is_("booked_notice_sent_at", "null").execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("after-booking for make-up %s failed: %s", session_id, exc)


async def daily(now: datetime | None = None) -> dict:
    """From makeup_cycle: rooms for any booked make-up missing one (a failed
    background job), and the day-before reminders."""
    now = now or _now()
    rooms = reminders = 0
    upcoming = (_t("makeup_session").select(
        "id, tutor_id, scheduled_at, duration_minutes, status, video_session_id, booked_notice_sent_at, reminder_sent_at"
    ).eq("status", "scheduled").gte("scheduled_at", now.isoformat()).execute()).data or []
    for s in upcoming:
        if not s.get("video_session_id") and await provision_room(s["id"]):
            rooms += 1
        start = _ts(s["scheduled_at"])
        notice = _ts(s.get("booked_notice_sent_at"))
        if (start - now <= REMIND_AHEAD and not s.get("reminder_sent_at")
                # A make-up booked inside the last 12 h has just been announced.
                and not (notice and now - notice < timedelta(hours=12))):
            await _send(s, "reminder")
            _t("makeup_session").update({"reminder_sent_at": now.isoformat()}).eq("id", s["id"]).execute()
            reminders += 1
    return {"rooms": rooms, "reminders": reminders}


# ── unmarked (§2f item 9) ─────────────────────────────────────────────────────

def unmarked(hours: int = 24, now: datetime | None = None) -> list[dict]:
    """Booked make-ups that ended more than `hours` ago with a seat still
    pending. They are unpaid (payout counts `completed` only) and they block
    those learners' next make-up seat, so the daily report names them."""
    now = now or _now()
    rows = (_t("makeup_session").select("id, tutor_id, scheduled_at, duration_minutes, status")
            .eq("status", "scheduled").execute()).data or []
    late = [r for r in rows
            if _ts(r["scheduled_at"]) + timedelta(minutes=int(r["duration_minutes"]), hours=hours) <= now]
    pending = {x["makeup_session_id"] for x in _seats([r["id"] for r in late]) if x["status"] == "pending"}
    return sorted((r for r in late if r["id"] in pending), key=lambda r: r["scheduled_at"])
