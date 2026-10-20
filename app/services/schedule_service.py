"""Moving a pod's time, with everyone's consent (enrolment plan 4.5, E-14,
migration §113, CONFLICT-056).

E-14: no ad-hoc moves. Two kinds of change, both proposed by ops (decided
2026-10-18) and applied only when EVERY active member has agreed:

  * PERMANENT — a new weekly slot from a checkpoint (an Ethiopic day 1). On
    that day the pod's future sessions at the old time are cancelled, the slot
    on `cohorts` is changed, and sessions are generated at the new time. The
    month's count is kept: a month never gets more sessions than the pod's
    cadence because the time changed in the middle of it.
  * TEMPORARY — a different weekly slot for the sessions inside a dated
    window of at most 60 days (Ramadan, Hudade). Each session in the window is
    replaced by one at the new weekday and time in the same week; the original
    is CANCELLED, not moved, so the generator's (cohort, time) uniqueness keeps
    it from coming back. After the window the usual slot simply continues.

Anyone declining ends the proposal. A proposal nobody finished answering by its
start date expires. A member who joins while a proposal is open must answer it
too. The new time must sit inside the home tutor's availability.

tauka-python writes `cohorts.slot_*` and future scheduled `cohort_session` rows
under CONFLICT-056 (approved 2026-10-18); never a session that has started.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.core import ethiopic_calendar as ec
from app.services import email_service
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 60
REASONS = ("ramadan", "hudade", "tutor", "members", "other")
SESSION_MINUTES = 50
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ScheduleRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _hhmm(v) -> time:
    s = str(v)
    return time(int(s[:2]), int(s[3:5]))


def _pod(cohort_id: str) -> dict:
    rows = (_t("cohorts").select("id, name, tutor_id, status, slot_day_of_week, slot_time, slot_timezone, "
                                 "sessions_per_month").eq("id", cohort_id).limit(1).execute()).data or []
    if not rows:
        raise ScheduleRefused("no_such_pod")
    return rows[0]


def _members(cohort_id: str) -> list[str]:
    return [m["user_id"] for m in (_t("cohort_memberships").select("user_id").eq("cohort_id", cohort_id)
                                   .eq("status", "active").execute()).data or []]


def slot_label(day: int, at, tz: str) -> str:
    return f"{DAY_NAMES[day]}s at {str(at)[:5]} ({tz})"


# ── The tutor's availability ────────────────────────────────────────────────

def tutor_covers(tutor_id: str | None, day: int, at: time, tz: str, on: date | None = None) -> bool:
    """Does one of the home tutor's availability windows hold the whole
    session? Checked on a concrete date so the two zones' DST is right."""
    if not tutor_id:
        return False
    on = on or _now().date()
    start_local = datetime.combine(on + timedelta(days=(day - on.weekday()) % 7), at, ZoneInfo(tz))
    # app.tutor_availability (tauka-react-web): day_of_week 0 = Monday, times in `timezone`.
    for w in (_t("tutor_availability").select("day_of_week, start_time, end_time, timezone")
              .eq("tutor_id", tutor_id).eq("active", True).execute()).data or []:
        try:
            z = ZoneInfo(w.get("timezone") or "UTC")
        except Exception:  # noqa: BLE001
            continue
        s = start_local.astimezone(z)
        if int(w["day_of_week"]) != s.weekday():
            continue
        lo, hi = _hhmm(w["start_time"]), _hhmm(w["end_time"])
        lo_min = lo.hour * 60 + lo.minute
        hi_min = hi.hour * 60 + hi.minute or 1440        # 00:00 as an end means midnight
        s_min = s.hour * 60 + s.minute
        if lo_min <= s_min and s_min + SESSION_MINUTES <= hi_min:
            return True
    return False


# ── Ops ──────────────────────────────────────────────────────────────────────

def _require_ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise ScheduleRefused("not_allowed")


async def propose(actor: str, cohort_id: str, kind: str, day: int, at: str, starts_on: date,
                  ends_on: date | None, reason: str, today: date | None = None) -> dict:
    _require_ops(actor)
    today = today or _now().date()
    pod = _pod(cohort_id)
    if pod["status"] != "active":
        raise ScheduleRefused("pod_not_active")
    if reason not in REASONS or kind not in ("permanent", "temporary") or not 0 <= day <= 6:
        raise ScheduleRefused("bad_request")
    new_time = _hhmm(at)
    if starts_on <= today:
        raise ScheduleRefused("starts_too_soon")
    if kind == "permanent":
        e = ec.from_gregorian(starts_on)
        if e.day != 1 or e.month == 13:
            raise ScheduleRefused("not_a_checkpoint")    # E-14: at a checkpoint
        ends_on = None
    else:
        if not ends_on or ends_on <= starts_on or (ends_on - starts_on).days > MAX_WINDOW_DAYS:
            raise ScheduleRefused("bad_window")
    tz = pod.get("slot_timezone") or "UTC"
    if day == pod.get("slot_day_of_week") and str(pod.get("slot_time"))[:5] == at[:5]:
        raise ScheduleRefused("same_slot")
    if not tutor_covers(pod.get("tutor_id"), day, new_time, tz, starts_on):
        raise ScheduleRefused("tutor_not_available")
    try:
        row = (_t("pod_schedule_change").insert({
            "cohort_id": cohort_id, "kind": kind, "new_day_of_week": day, "new_time": at[:5],
            "timezone": tz, "starts_on": starts_on.isoformat(),
            "ends_on": ends_on.isoformat() if ends_on else None, "reason": reason, "proposed_by": actor,
        }).execute()).data[0]
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise ScheduleRefused("already_proposed") from exc
        raise
    await _mail(cohort_id, row, "proposed")
    return row


def withdraw(actor: str, change_id: str) -> dict:
    _require_ops(actor)
    got = (_t("pod_schedule_change").update({"status": "withdrawn", "decided_at": _now().isoformat()})
           .eq("id", change_id).in_("status", ["proposed", "agreed"]).execute()).data or []
    if not got:
        raise ScheduleRefused("not_open")
    return got[0]


def queue(actor: str) -> dict:
    """Live proposals with every member's answer, recent outcomes, and the
    active pods a change can be proposed for."""
    _require_ops(actor)
    from app.services.makeup_service import _names
    rows = (_t("pod_schedule_change").select("*").order("created_at", desc=True).limit(50)
            .execute()).data or []
    pods = (_t("cohorts").select("id, name, tutor_id, level, slot_day_of_week, slot_time, slot_timezone, status")
            .eq("status", "active").execute()).data or []
    pod_by = {p["id"]: p for p in pods}
    out = []
    for r in rows:
        members = _members(r["cohort_id"])
        answers = {a["student_id"]: a["answer"] for a in (_t("pod_schedule_consent").select("student_id, answer")
                   .eq("change_id", r["id"]).execute()).data or []}
        names = _names(members + list(answers))
        out.append({**r, "pod": pod_by.get(r["cohort_id"]),
                    "answers": [{"name": names.get(m), "answer": answers.get(m)} for m in members]})
    return {"changes": out, "pods": [{**p, "slot": slot_label(p["slot_day_of_week"], p["slot_time"],
                                                                p["slot_timezone"])}
                                     for p in pods if p.get("slot_timezone") and p.get("slot_time") is not None
                                     and p.get("slot_day_of_week") is not None]}


# ── Members ──────────────────────────────────────────────────────────────────

def mine(student_id: str) -> list[dict]:
    """Open proposals for the learner's pod: old and new time, dates, their own
    answer, and how many have answered (never who)."""
    pods = [m["cohort_id"] for m in (_t("cohort_memberships").select("cohort_id").eq("user_id", student_id)
                                     .eq("status", "active").execute()).data or []]
    if not pods:
        return []
    rows = (_t("pod_schedule_change").select("*").in_("cohort_id", pods)
            .in_("status", ["proposed", "agreed"]).execute()).data or []
    out = []
    for r in rows:
        pod = _pod(r["cohort_id"])
        members = _members(r["cohort_id"])
        answers = {a["student_id"]: a["answer"] for a in (_t("pod_schedule_consent").select("student_id, answer")
                   .eq("change_id", r["id"]).execute()).data or []}
        out.append({
            "id": r["id"], "kind": r["kind"], "reason": r["reason"], "status": r["status"],
            "starts_on": r["starts_on"], "ends_on": r.get("ends_on"),
            "current": {"day_of_week": pod.get("slot_day_of_week"), "time": str(pod.get("slot_time"))[:5],
                        "timezone": pod.get("slot_timezone")},
            "proposed": {"day_of_week": r["new_day_of_week"], "time": str(r["new_time"])[:5],
                         "timezone": r["timezone"]},
            # An instant, so the app shows it in the device's own time.
            "first_new_at": first_new(r).astimezone(timezone.utc).isoformat(),
            "your_answer": answers.get(student_id),
            "answered": sum(1 for m in members if m in answers), "members": len(members),
        })
    return out


def answer(student_id: str, change_id: str, agree: bool) -> dict:
    rows = (_t("pod_schedule_change").select("*").eq("id", change_id).limit(1).execute()).data or []
    if not rows:
        raise ScheduleRefused("no_such_change")
    ch = rows[0]
    members = _members(ch["cohort_id"])
    if student_id not in members:
        raise ScheduleRefused("not_your_pod")
    if ch["status"] not in ("proposed", "agreed"):
        raise ScheduleRefused("closed")
    try:
        _t("pod_schedule_consent").insert({"change_id": change_id, "student_id": student_id,
                                           "answer": "agree" if agree else "decline"}).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise ScheduleRefused("already_answered") from exc
        raise
    now = _now().isoformat()
    if not agree:
        _t("pod_schedule_change").update({"status": "declined", "decided_at": now}) \
            .eq("id", change_id).in_("status", ["proposed", "agreed"]).execute()
        return {**ch, "status": "declined"}
    if ch["status"] == "proposed" and _all_agreed(change_id, members):
        _t("pod_schedule_change").update({"status": "agreed", "decided_at": now}) \
            .eq("id", change_id).eq("status", "proposed").execute()
        return {**ch, "status": "agreed"}
    return ch


def _all_agreed(change_id: str, members: list[str]) -> bool:
    yes = {a["student_id"] for a in (_t("pod_schedule_consent").select("student_id, answer")
           .eq("change_id", change_id).execute()).data or [] if a["answer"] == "agree"}
    return bool(members) and all(m in yes for m in members)


# ── Applying ─────────────────────────────────────────────────────────────────

def _future_sessions(cohort_id: str, now: datetime) -> list[dict]:
    # app.cohort_session (tauka-flutter): CONFLICT-056 — future `scheduled` rows only.
    return [s for s in (_t("cohort_session").select("id, session_number, scheduled_at, duration_minutes, status, "
                                                    "lead_tutor_id, lead_role")
                        .eq("cohort_id", cohort_id).gte("scheduled_at", now.isoformat()).execute()).data or []
            if s.get("status") == "scheduled" and _ts(s["scheduled_at"]) > now]


def _cancel(session_id: str, why: str) -> None:
    _t("cohort_session").update({"status": "cancelled", "cancelled_reason": why}) \
        .eq("id", session_id).eq("status", "scheduled").execute()


def _keep_month_count(pod: dict, now: datetime) -> int:
    """After a permanent change, cancel any session beyond the pod's cadence in
    the current calendar month (pod zone): held + scheduled never exceeds it."""
    tz = ZoneInfo(pod.get("slot_timezone") or "UTC")
    local = now.astimezone(tz)
    start = datetime(local.year, local.month, 1, tzinfo=tz)
    end = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=tz)
    rows = [s for s in (_t("cohort_session").select("id, scheduled_at, status").eq("cohort_id", pod["id"])
                        .gte("scheduled_at", start.astimezone(timezone.utc).isoformat())
                        .lt("scheduled_at", end.astimezone(timezone.utc).isoformat()).execute()).data or []
            if s.get("status") != "cancelled"]
    per_month = int(pod.get("sessions_per_month") or 3)
    extra = len(rows) - per_month
    if extra <= 0:
        return 0
    latest = sorted((s for s in rows if s["status"] == "scheduled" and _ts(s["scheduled_at"]) > now),
                    key=lambda s: s["scheduled_at"], reverse=True)[:extra]
    for s in latest:
        _cancel(s["id"], "not needed: this month's sessions were already held (pod time changed, plan 4.5)")
    return len(latest)


def _apply_permanent(ch: dict, pod: dict, now: datetime) -> dict:
    cancelled = 0
    for s in _future_sessions(pod["id"], now):
        _cancel(s["id"], "pod time changed with every member's agreement (plan 4.5)")
        cancelled += 1
    # CONFLICT-056: the slot columns only.
    _t("cohorts").update({"slot_day_of_week": ch["new_day_of_week"], "slot_time": str(ch["new_time"])[:5],
                          "slot_timezone": ch["timezone"]}).eq("id", pod["id"]).execute()
    created = db.schema("app").rpc("generate_cohort_sessions", {"p_cohort": pod["id"], "p_months": 2}).execute().data
    trimmed = _keep_month_count({**pod, "slot_timezone": ch["timezone"]}, now)
    return {"cancelled": cancelled, "created": created, "trimmed": trimmed}


def _window_time(at: datetime, ch: dict) -> datetime:
    """The same week's new weekday and time, in the change's zone."""
    tz = ZoneInfo(ch["timezone"])
    local = at.astimezone(tz)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday + timedelta(days=ch["new_day_of_week"]), _hhmm(ch["new_time"]), tz)


def first_new(ch: dict) -> datetime:
    """The first session at the new time: the new weekday/time on or after
    the start date."""
    ref = _window_time(datetime.combine(_d(ch["starts_on"]), time(12), ZoneInfo(ch["timezone"])), ch)
    return ref + timedelta(days=7) if ref.date() < _d(ch["starts_on"]) else ref


def _apply_temporary(ch: dict, pod: dict, now: datetime) -> dict:
    tz = ZoneInfo(ch["timezone"])
    lo, hi = _d(ch["starts_on"]), _d(ch["ends_on"])
    moved = 0
    sessions = _future_sessions(pod["id"], now)
    next_num = max([int(s.get("session_number") or 0) for s in
                    (_t("cohort_session").select("session_number").eq("cohort_id", pod["id"]).execute()).data or []]
                   or [0]) + 1
    for s in sessions:
        at = _ts(s["scheduled_at"])
        if not lo <= at.astimezone(tz).date() < hi:
            continue
        new_at = _window_time(at, ch)
        if new_at == at or new_at <= now:
            continue            # already at the window's time, or too late to move
        try:
            _t("cohort_session").insert({
                "cohort_id": pod["id"], "session_number": next_num,
                "scheduled_at": new_at.astimezone(timezone.utc).isoformat(),
                "duration_minutes": s.get("duration_minutes") or SESSION_MINUTES, "status": "scheduled",
                "lead_tutor_id": s.get("lead_tutor_id"), "lead_role": s.get("lead_role"),
            }).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("window session for %s failed: %s", s["id"], exc)
            continue
        next_num += 1
        # Cancelled, not moved: the generator will not recreate a cancelled time.
        _cancel(s["id"], f"moved for a temporary window ({ch['reason']}, plan 4.5)")
        moved += 1
    return {"moved": moved}


async def daily(now: datetime | None = None) -> dict:
    """Expire proposals not agreed by their start; apply agreed ones whose
    start has come (while every current member has agreed)."""
    now = now or _now()
    today = now.date()
    out = {"expired": 0, "applied": 0, "waiting": 0, "rewindowed": 0}
    # A running window is re-applied daily: sessions generated after it was
    # applied (the generator runs on every accept) would otherwise land at the
    # usual time inside the window.
    for ch in (_t("pod_schedule_change").select("*").eq("status", "applied").eq("kind", "temporary")
               .execute()).data or []:
        if _d(ch["starts_on"]) <= today < _d(ch["ends_on"]):
            try:
                pod = _pod(ch["cohort_id"])
                if pod["status"] == "active":
                    out["rewindowed"] += _apply_temporary(ch, pod, now)["moved"]
            except Exception as exc:  # noqa: BLE001
                logger.error("re-applying window %s: %s", ch.get("id"), exc)
    for ch in (_t("pod_schedule_change").select("*").in_("status", ["proposed", "agreed"]).execute()).data or []:
        try:
            starts = _d(ch["starts_on"])
            if ch["status"] == "proposed" and starts <= today:
                _t("pod_schedule_change").update({"status": "expired", "decided_at": now.isoformat()}) \
                    .eq("id", ch["id"]).eq("status", "proposed").execute()
                out["expired"] += 1
                continue
            if ch["status"] != "agreed" or starts > today:
                continue
            if ch["kind"] == "temporary" and _d(ch["ends_on"]) <= today:
                _t("pod_schedule_change").update({"status": "withdrawn", "decided_at": now.isoformat()}) \
                    .eq("id", ch["id"]).eq("status", "agreed").execute()
                continue
            # Someone who joined after the vote must agree too.
            if not _all_agreed(ch["id"], _members(ch["cohort_id"])):
                out["waiting"] += 1
                continue
            pod = _pod(ch["cohort_id"])
            if pod["status"] != "active":
                continue
            result = (_apply_permanent if ch["kind"] == "permanent" else _apply_temporary)(ch, pod, now)
            _t("pod_schedule_change").update({"status": "applied", "applied_at": now.isoformat()}) \
                .eq("id", ch["id"]).eq("status", "agreed").execute()
            logger.info("schedule change %s applied: %s", ch["id"], result)
            await _mail(ch["cohort_id"], ch, "applied")
            out["applied"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("schedule change %s: %s", ch.get("id"), exc)
    return out


async def _mail(cohort_id: str, ch: dict, kind: str) -> None:
    """Email every member (and the home tutor) in their own zone. Best effort."""
    from app.services.makeup_delivery import _email_of, _zones, local_time
    try:
        pod = _pod(cohort_id)
        people = _members(cohort_id) + ([pod["tutor_id"]] if pod.get("tutor_id") else [])
        zones = _zones(people)
        ref = first_new(ch)
        # Plan 4.5: once applied, an updated calendar file — the pod's sessions
        # now, and the ones the change cancelled (so calendars drop them).
        attachments = None
        if kind == "applied":
            from app.services.placement_service import ics
            now = _now()
            upcoming = (_t("cohort_session").select("id, scheduled_at, duration_minutes, status, cancelled_reason")
                        .eq("cohort_id", cohort_id).gte("scheduled_at", now.isoformat()).order("scheduled_at")
                        .execute()).data or []
            live = [s for s in upcoming if s.get("status") == "scheduled"]
            moved = [s for s in upcoming if s.get("status") == "cancelled"
                     and "plan 4.5" in (s.get("cancelled_reason") or "")]
            if live or moved:
                attachments = [{"filename": "tauka-pod.ics", "content": ics(pod, live, now, moved),
                                "content_type": "text/calendar"}]
        for uid in people:
            to = _email_of(uid)
            if not to:
                continue
            await email_service.send_template_email(to, "schedule_change", {
                "subject": ("Your pod is asked to change its time" if kind == "proposed"
                            else "Your pod's new time starts now"),
                "kind": kind, "change_kind": ch["kind"], "pod_name": pod.get("name") or "Your pod",
                "audience": "tutor" if uid == pod.get("tutor_id") else "learner",
                "first_new": local_time(ref, zones.get(uid, "UTC")),
                "starts_on": str(ch["starts_on"]), "ends_on": str(ch.get("ends_on") or ""),
                "web_base_url": settings.web_base_url,
                "calendar": bool(attachments),
            }, tags=["schedule_change", kind], attachments=attachments)
    except Exception as exc:  # noqa: BLE001
        logger.error("schedule change mail for %s failed: %s", cohort_id, exc)
