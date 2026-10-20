"""Monthly pod checkpoints (enrolment plan 4.2, §2c, migration §109).

§2c: "Pod checkpoints are monthly, on formation day (the first day of the
Ethiopic month): tutor rotation (= guest rota review, E-09), pace moves and
refills happen there."

  * The daily job opens one checkpoint per live pod per Ethiopic month 1-12, on
    or after day 1 (a missed cron day is caught up; Pagume has none). A pod
    that started on or after day 1 waits for next month's.
  * The row carries a snapshot of the month just gone, built ONLY from inputs
    E-16 allows: tutor-marked attendance, the sessions and who led them,
    members, open seats, the lesson each member is on (unit_progress) and the
    pace pledge. Never activity_summary or ai_usage. The flags in it are a
    reading aid for a person; nothing acts on them.
  * An ops admin reviews it once: an outcome, whether the guest rota was
    reviewed, and which members move to the wait pool (a pace move). A move
    and a dissolve go through the CONFLICT-048 write path (membership status,
    left_at, removal_reason; pod status) and enrolment_state in_pod -> wait_pool.

Open seats are refilled automatically in the first 14 days after a formation
day (refill_service, plan 3.6, §112): a move or a pause here opens a seat that
the daily refill offers to a waiting learner who fits.
"""

import logging
from datetime import date, datetime, timezone

from app.core import ethiopic_calendar as ec
from app.services import hierarchy_service as hs
from app.services import phase0_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

POD_STATUSES = ("forming", "active")
OUTCOMES = ("continue", "adjust", "pause", "dissolve")
# A member who attended fewer than half of the month's marked sessions is
# flagged for the reviewer. Tutor-marked attendance only (E-16).
LOW_ATTENDANCE = 0.5


class CheckpointRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _d(v) -> date | None:
    if v is None:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def month_of(today: date) -> tuple[int, int, date, date] | None:
    """(ec_year, ec_month, due_on, window_start) for the checkpoint month that
    contains `today`, or None in Pagume. The window is the month just gone:
    from the previous checkpoint's day 1 (Nehase's, for Meskerem, so Pagume's
    sessions count) up to this day 1."""
    e = ec.from_gregorian(today)
    if e.month == 13:
        return None
    due_on = ec.to_gregorian(ec.EthiopicDate(e.year, e.month, 1))
    py, pm = (e.year, e.month - 1) if e.month > 1 else (e.year - 1, 12)
    start = ec.to_gregorian(ec.EthiopicDate(py, pm, 1))
    return e.year, e.month, due_on, start


# ── The snapshot ─────────────────────────────────────────────────────────────

def snapshot(pod: dict, start: date, end: date) -> dict:
    """The month [start, end) for one pod, for a person to read."""
    from app.services.intake_service import _current_lesson_map
    from app.services.makeup_service import _names

    pod_id = pod["id"]
    # app.cohort_memberships (tauka-flutter): user_id, status, joined_at.
    members = (_t("cohort_memberships").select("user_id, joined_at")
               .eq("cohort_id", pod_id).eq("status", "active").execute()).data or []
    ids = [m["user_id"] for m in members]

    # app.cohort_session (tauka-flutter): the month's occurrences and who led them.
    sessions = [s for s in (_t("cohort_session").select("id, scheduled_at, status, lead_tutor_id, lead_role")
                            .eq("cohort_id", pod_id).gte("scheduled_at", start.isoformat())
                            .lt("scheduled_at", end.isoformat()).execute()).data or []]
    held = [s for s in sessions if s.get("status") == "completed"]
    attendance: dict[str, dict[str, int]] = {i: {"attended": 0, "absent": 0, "excused": 0} for i in ids}
    if held:
        # app.cohort_session_attendance (tauka-flutter): marked by the tutor.
        for a in (_t("cohort_session_attendance").select("cohort_session_id, user_id, status")
                  .in_("cohort_session_id", [s["id"] for s in held]).execute()).data or []:
            row = attendance.get(a["user_id"])
            if row is not None and a.get("status") in row:
                row[a["status"]] += 1

    pledges = {r["student_id"]: r.get("pace_pledge_hours") for r in
               (_t("enrolment_state").select("student_id, pace_pledge_hours")
                .in_("student_id", ids).execute()).data or []} if ids else {}
    lessons = _current_lesson_map(ids) if ids else {}
    leads = [s.get("lead_tutor_id") for s in held]
    names = _names(ids + [t for t in leads if t] + [pod.get("tutor_id")])

    low, high = pod.get("band_lesson_low"), pod.get("band_lesson_high")
    out_members = []
    for i in ids:
        a = attendance[i]
        marked = a["attended"] + a["absent"] + a["excused"]
        lesson = lessons.get(i)
        flags = []
        if marked and a["attended"] / marked < LOW_ATTENDANCE:
            flags.append("low_attendance")
        if lesson is not None and high is not None and lesson > high:
            flags.append("ahead_of_band")
        if lesson is not None and low is not None and lesson < low:
            flags.append("behind_band")
        # Plan 4.7: the tracker's status (display only, E-16), best effort.
        try:
            from app.services.tracker_service import for_student
            tracker = for_student(i, datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc))["status"]
        except Exception:  # noqa: BLE001
            tracker = None
        # §141: the latest unit assessment (playbook: "unit assessment" is part of the review).
        try:
            from app.services.unit_assessment_service import latest
            assessment = latest(i)
        except Exception:  # noqa: BLE001
            assessment = None
        if assessment and assessment.get("outcome") == "not_yet":
            flags.append("assessment_not_yet")
        out_members.append({"student_id": i, "name": names.get(i), "lesson": lesson,
                            "pace_pledge_hours": pledges.get(i), **a, "flags": flags, "tracker": tracker,
                            "assessment": assessment})

    rota: dict[str, dict] = {}
    for s in held:
        t = s.get("lead_tutor_id")
        if not t:
            continue
        r = rota.setdefault(t, {"tutor_id": t, "name": names.get(t), "role": s.get("lead_role"), "sessions": 0})
        r["sessions"] += 1
    lesson_values = [m["lesson"] for m in out_members if m["lesson"] is not None]
    seats = int(pod.get("max_students") or 4)
    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "home_tutor": {"tutor_id": pod.get("tutor_id"), "name": names.get(pod.get("tutor_id"))},
        "sessions": {"scheduled": len(sessions), "held": len(held),
                     "cancelled": sum(1 for s in sessions if s.get("status") == "cancelled")},
        "rota": sorted(rota.values(), key=lambda r: -r["sessions"]),
        "members": out_members,
        "open_seats": max(0, seats - len(ids)),
        "band": {"low": low, "high": high},
        "lesson_spread": (max(lesson_values) - min(lesson_values)) if lesson_values else 0,
    }


def open_due(today: date | None = None) -> int:
    """Open this month's checkpoint for every live pod that doesn't have one."""
    today = today or _now().date()
    month = month_of(today)
    if month is None:
        return 0
    year, mon, due_on, start = month
    # app.cohorts (tauka-flutter): live pods and their band, seats, home tutor.
    pods = (_t("cohorts").select("id, name, tutor_id, max_students, start_date, created_at, "
                                 "band_lesson_low, band_lesson_high, status")
            .in_("status", list(POD_STATUSES)).execute()).data or []
    pods = [p for p in pods if (_d(p.get("start_date")) or _d(p.get("created_at")) or due_on) < due_on]
    if not pods:
        return 0
    have = {r["cohort_id"] for r in (_t("pod_checkpoint").select("cohort_id")
            .eq("ec_year", year).eq("ec_month", mon).in_("cohort_id", [p["id"] for p in pods])
            .execute()).data or []}
    opened = 0
    for p in pods:
        if p["id"] in have:
            continue
        try:
            snap = snapshot(p, start, due_on)
            _t("pod_checkpoint").insert({"cohort_id": p["id"], "ec_year": year, "ec_month": mon,
                                         "due_on": due_on.isoformat(), "snapshot": snap}).execute()
            opened += 1
        except Exception as exc:  # noqa: BLE001
            # The unique key: a parallel run opened it first. Anything else is logged
            # and retried tomorrow; one pod must not stop the rest.
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                logger.error("checkpoint for pod %s failed: %s", p["id"], exc)
    return opened


# ── Review ───────────────────────────────────────────────────────────────────

def _require_ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise CheckpointRefused("not_allowed")


def queue(actor: str, include_reviewed: bool = False) -> list[dict]:
    _require_ops(actor)
    q = _t("pod_checkpoint").select("*")
    if not include_reviewed:
        q = q.eq("status", "open")
    rows = (q.order("due_on").execute()).data or []
    if not rows:
        return []
    pods = {p["id"]: p for p in (_t("cohorts").select("id, name, level, status")
            .in_("id", sorted({r["cohort_id"] for r in rows})).execute()).data or []}
    return [{**r, "pod": pods.get(r["cohort_id"])} for r in rows]


def _leave(pod_id: str, student_ids: list[str], status: str, reason: str) -> None:
    """CONFLICT-048: tauka-python may UPDATE membership status/left_at/removal_reason."""
    _t("cohort_memberships").update({
        "status": status, "left_at": _now().isoformat(), "removal_reason": reason,
    }).eq("cohort_id", pod_id).eq("status", "active").in_("user_id", student_ids).execute()
    for sid in student_ids:
        phase0_service.advance(sid, ("in_pod",), "wait_pool")


def review(actor: str, checkpoint_id: str, outcome: str, rota_reviewed: bool,
           move_student_ids: list[str] | None = None) -> dict:
    _require_ops(actor)
    if outcome not in OUTCOMES:
        raise CheckpointRefused("bad_outcome")
    rows = (_t("pod_checkpoint").select("*").eq("id", checkpoint_id).limit(1).execute()).data or []
    if not rows:
        raise CheckpointRefused("no_such_checkpoint")
    cp = rows[0]
    if cp["status"] != "open":
        raise CheckpointRefused("reviewed")
    pod_id = cp["cohort_id"]
    active = {m["user_id"] for m in (_t("cohort_memberships").select("user_id")
              .eq("cohort_id", pod_id).eq("status", "active").execute()).data or []}
    moved = sorted(set(move_student_ids or []))
    if any(s not in active for s in moved):
        raise CheckpointRefused("not_a_member")
    if outcome == "continue" and moved:
        # "Continue" means nothing changed; a move is an adjustment.
        raise CheckpointRefused("move_needs_adjust")

    # Claim the review first, so two admins can't both carry it out.
    claimed = (_t("pod_checkpoint").update({
        "status": "reviewed", "outcome": outcome, "rota_reviewed": bool(rota_reviewed),
        "moved_student_ids": moved, "reviewed_by": actor, "reviewed_at": _now().isoformat(),
    }).eq("id", checkpoint_id).eq("status", "open").execute()).data or []
    if not claimed:
        raise CheckpointRefused("reviewed")

    if outcome == "dissolve":
        _leave(pod_id, sorted(active), "dropped", "pod dissolved at a monthly checkpoint")
        _t("cohorts").update({"status": "dissolved"}).eq("id", pod_id).execute()
    else:
        if moved:
            _leave(pod_id, moved, "transferred", "moved to the wait pool at a monthly checkpoint (pace)")
        if outcome == "pause":
            _t("cohorts").update({"status": "paused"}).eq("id", pod_id).execute()
    return {**cp, **claimed[0]}
