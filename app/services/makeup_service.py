"""Make-up sessions — loading, eligibility, proposals, sweeps (plan §2e–§2g, §92–§93).

WHO NEEDS A MAKE-UP
  A learner on a paid tier, inside a paid month, NOT in an active pod, who has
  had fewer make-up seats this month than the tier's sessions
  (Learner 1, Tutor 3, Intensive 3) — or anyone, podded or not, holding a
  carried-forward credit.

  The paid month comes from `app.lesson_entitlement` (§91): every paid or gift
  month already writes one row there, so there is one record of "which month
  did they pay for", not two.

ONE SEAT AT A TIME
  A learner gets a new seat only when they hold no open one (proposed, or
  scheduled and not yet marked) and none was made for them in the last
  `SPACING`. A Tutor learner's three make-ups arrive one after another.

WHO AND WHEN (§2g)
  The planner proposes a tutor from every member's own pool (home + guests) and
  a time inside the tutor's availability and every member's preferred windows.
  An admin confirms or overrides with `schedule()`; an override is still held to
  the pool rule and the tutor's other sessions.

WHAT THE DATABASE DOES, NOT THIS FILE (§92 triggers)
  Minutes from head-count; the three-learner cap; a credit spent when a
  credit-funded seat is attended or missed; credits granted to month-funded
  learners when a session is cancelled for any reason but "nobody left".
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta

from app.services.makeup_planner import OpenGroup, Plan, Waiting, Window, plan_makeups
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

SESSIONS_PER_MONTH = {
    "learner_tier": 1,
    "tutor_tier": 3,
    "intensive_tier": 3,   # group sessions; the 2 private 1:1s don't need a pod
}
SPACING = timedelta(days=7)
CREDIT_SEAT_WINDOW = timedelta(days=14)
GIFT_MONTH = timedelta(days=30)


class ScheduleRefused(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def _ts(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _min(raw) -> int:
    h, m = str(raw).split(":")[:2]
    return int(h) * 60 + int(m)


# ── pure: facts → eligibility ───────────────────────────────────────────────

@dataclass(frozen=True)
class Seat:
    session_id: str
    funded_by: str
    period_start: datetime | None
    status: str                      # participant status
    session_status: str
    created_at: datetime


@dataclass(frozen=True)
class LearnerFacts:
    student_id: str
    tier: str | None
    course_id: str | None
    podded: bool
    paid_months: list[tuple[datetime, str]]   # (available_from, source)
    seats: list[Seat]
    credit_balance: int
    # Start times of the learner's active pod's sessions (not cancelled) near
    # now. A learner podded late in their paid month gets fewer pod sessions
    # than the tier includes; make-ups cover the shortfall (§2f item 8,
    # developer 2026-10-18).
    pod_sessions: tuple[datetime, ...] = ()


def current_period(paid_months: list[tuple[datetime, str]], now: datetime):
    """(start, end) of the paid month `now` falls in, or None."""
    started = [(s, src) for s, src in paid_months if s <= now]
    if not started:
        return None
    start, source = max(started, key=lambda p: p[0])
    end = start + (GIFT_MONTH if source == "gift_month" else relativedelta(months=1))
    return (start, end) if now < end else None


def needs_seat(f: LearnerFacts, now: datetime) -> Waiting | None:
    live = [s for s in f.seats if s.status != "withdrawn"]
    if any(s.session_status in ("proposed", "scheduled") and s.status == "pending" for s in live):
        return None                                   # one open seat at a time
    if any(now - s.created_at < SPACING for s in live):
        return None

    per_month = SESSIONS_PER_MONTH.get(f.tier or "", 0)
    period = current_period(f.paid_months, now)
    if per_month and period:
        start, end = period
        used = sum(1 for s in live if s.funded_by == "month" and s.period_start == start)
        # Pod sessions inside this paid month count towards it; the rest is
        # owed as make-ups (§2f item 8).
        pod = sum(1 for t in f.pod_sessions if start <= t < end) if f.podded else 0
        if used + pod < per_month:
            return Waiting(f.student_id, f.course_id, "month", start, end)

    if f.credit_balance > 0:
        return Waiting(f.student_id, f.course_id, "credit", None, now + CREDIT_SEAT_WINDOW)
    return None


# ── loading ─────────────────────────────────────────────────────────────────

def _pod_session_times(student_ids: list[str], now: datetime) -> dict[str, tuple[datetime, ...]]:
    """Start times of each learner's active pod's sessions within ~a paid month
    either side of now, cancelled ones excluded (§2f item 8)."""
    active = db.schema("app").table("cohorts").select("id").eq("status", "active").execute().data or []
    cohort_ids = [c["id"] for c in active]
    if not cohort_ids or not student_ids:
        return {}
    members = (db.schema("app").table("cohort_memberships").select("user_id, cohort_id")
               .in_("cohort_id", cohort_ids).in_("user_id", student_ids)
               .eq("status", "active").execute().data or [])
    if not members:
        return {}
    window = timedelta(days=40)
    # app.cohort_session (tauka-flutter): cohort_id, scheduled_at, status.
    rows = (db.schema("app").table("cohort_session").select("cohort_id, scheduled_at, status")
            .in_("cohort_id", list({m["cohort_id"] for m in members}))
            .gte("scheduled_at", (now - window).isoformat()).lt("scheduled_at", (now + window).isoformat())
            .execute().data or [])
    by_cohort: dict[str, list[datetime]] = {}
    for r in rows:
        if r.get("status") != "cancelled":
            by_cohort.setdefault(r["cohort_id"], []).append(_ts(r["scheduled_at"]))
    return {m["user_id"]: tuple(sorted(by_cohort.get(m["cohort_id"], ()))) for m in members}


def _podded_ids(student_ids: list[str]) -> set[str]:
    active = db.schema("app").table("cohorts").select("id").eq("status", "active").execute().data or []
    cohort_ids = [c["id"] for c in active]
    if not cohort_ids or not student_ids:
        return set()
    rows = (db.schema("app").table("cohort_memberships").select("user_id")
            .in_("cohort_id", cohort_ids).in_("user_id", student_ids)
            .eq("status", "active").execute().data or [])
    return {r["user_id"] for r in rows}


def load_facts() -> list[LearnerFacts]:
    students = (db.schema("app").table("student").select("id, tier")
                .in_("tier", list(SESSIONS_PER_MONTH)).is_("deleted_at", "null")
                .execute().data or [])
    credit_rows = db.schema("app").table("session_credit") \
        .select("student_id, direction, quantity").execute().data or []
    balances: dict[str, int] = {}
    for r in credit_rows:
        sign = 1 if r["direction"] == "grant" else -1
        balances[r["student_id"]] = balances.get(r["student_id"], 0) + sign * r["quantity"]

    tiers = {s["id"]: s.get("tier") for s in students}
    for sid, bal in balances.items():
        if bal > 0 and sid not in tiers:
            tiers[sid] = None
    # A paused learner keeps their credits but isn't grouped (plan 4.4, §111).
    paused = {r["student_id"] for r in (db.schema("app").table("enrolment_pause").select("student_id")
              .eq("status", "active").execute().data or [])}
    tiers = {sid: t for sid, t in tiers.items() if sid not in paused}
    # Nor is a lapsed one (missed payment past its grace, plan 2.3a, §126).
    from app.services.grace_service import lapsed_ids
    lapsed = lapsed_ids(list(tiers))
    tiers = {sid: t for sid, t in tiers.items() if sid not in lapsed}
    ids = list(tiers)
    if not ids:
        return []

    podded = _podded_ids(ids)
    courses = {}
    for r in (db.schema("app").table("student_course").select("student_id, course_id")
              .in_("student_id", ids).execute().data or []):
        courses.setdefault(r["student_id"], r["course_id"])

    months: dict[str, list] = {}
    for r in (db.schema("app").table("lesson_entitlement")
              .select("student_id, available_from, source")
              .in_("student_id", ids).in_("source", ["paid_month", "gift_month"])
              .is_("revoked_at", "null").execute().data or []):
        months.setdefault(r["student_id"], []).append((_ts(r["available_from"]), r["source"]))

    seat_rows = (db.schema("app").table("makeup_session_participant")
                 .select("makeup_session_id, student_id, funded_by, period_start, status, created_at")
                 .in_("student_id", ids).execute().data or [])
    session_ids = list({r["makeup_session_id"] for r in seat_rows})
    session_status = {}
    if session_ids:
        for r in (db.schema("app").table("makeup_session").select("id, status")
                  .in_("id", session_ids).execute().data or []):
            session_status[r["id"]] = r["status"]
    seats: dict[str, list[Seat]] = {}
    for r in seat_rows:
        seats.setdefault(r["student_id"], []).append(Seat(
            r["makeup_session_id"], r["funded_by"], _ts(r.get("period_start")),
            r["status"], session_status.get(r["makeup_session_id"], "cancelled"),
            _ts(r["created_at"])))

    pod_times = _pod_session_times(list(podded), datetime.now(timezone.utc)) if podded else {}
    return [LearnerFacts(sid, tiers[sid], courses.get(sid), sid in podded,
                         months.get(sid, []), seats.get(sid, []), balances.get(sid, 0),
                         pod_times.get(sid, ()))
            for sid in ids]


def load_pools(student_ids: list[str]) -> dict[str, tuple[str, ...]]:
    """Each learner's own tutors: the home tutor first, then guests."""
    if not student_ids:
        return {}
    rows = (db.schema("app").table("student_tutor_link").select("student_id, tutor_id, role")
            .in_("student_id", student_ids).eq("state", "active").execute().data or [])
    pools: dict[str, list] = {}
    for r in sorted(rows, key=lambda r: 0 if r["role"] == "home" else 1):
        pool = pools.setdefault(r["student_id"], [])
        if r["tutor_id"] not in pool:
            pool.append(r["tutor_id"])
    return {k: tuple(v) for k, v in pools.items()}


def load_learner_windows(student_ids: list[str]) -> dict[str, tuple[Window, ...]]:
    """Preferred times, in the learner's profile timezone (§85.1)."""
    if not student_ids:
        return {}
    zones = {r["id"]: r.get("timezone") or "UTC" for r in
             (db.schema("app").table("user_profiles").select("id, timezone")
              .in_("id", student_ids).execute().data or [])}
    out: dict[str, list] = {}
    for r in (db.schema("app").table("student_time_preference")
              .select("student_id, day_of_week, start_time, end_time, preference_rank")
              .in_("student_id", student_ids).eq("active", True).execute().data or []):
        out.setdefault(r["student_id"], []).append(Window(
            int(r["day_of_week"]), _min(r["start_time"]), _min(r["end_time"]),
            zones.get(r["student_id"], "UTC"), int(r.get("preference_rank") or 1)))
    return {k: tuple(v) for k, v in out.items()}


def load_tutor_windows(tutor_ids: list[str]) -> dict[str, list[Window]]:
    if not tutor_ids:
        return {}
    out: dict[str, list[Window]] = {}
    for r in (db.schema("app").table("tutor_availability")
              .select("tutor_id, day_of_week, start_time, end_time, timezone")
              .in_("tutor_id", tutor_ids).eq("active", True).execute().data or []):
        out.setdefault(r["tutor_id"], []).append(Window(
            int(r["day_of_week"]), _min(r["start_time"]), _min(r["end_time"]) or 1440,
            r.get("timezone") or "UTC"))
    return out


def load_busy(tutor_ids: list[str], now: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    """Everything the tutors are already committed to from now on: pod sessions
    they lead, 1-on-1s, scheduled make-ups, and other groups' pending proposals."""
    busy: dict[str, list] = {}
    if not tutor_ids:
        return busy

    def add(tutor, start, minutes):
        if tutor and start:
            s = _ts(start)
            busy.setdefault(tutor, []).append((s, s + timedelta(minutes=int(minutes or 50))))

    since = now.isoformat()
    for r in (db.schema("app").table("cohort_session")
              .select("lead_tutor_id, scheduled_at, duration_minutes")
              .in_("lead_tutor_id", tutor_ids).eq("status", "scheduled")
              .gte("scheduled_at", since).execute().data or []):
        add(r["lead_tutor_id"], r["scheduled_at"], r["duration_minutes"])
    for r in (db.schema("app").table("one_to_one_session")
              .select("tutor_id, scheduled_at, duration_minutes")
              .in_("tutor_id", tutor_ids).eq("status", "booked")
              .gte("scheduled_at", since).execute().data or []):
        add(r["tutor_id"], r["scheduled_at"], r["duration_minutes"])
    for r in (db.schema("app").table("makeup_session")
              .select("tutor_id, scheduled_at, proposed_tutor_id, proposed_at, duration_minutes, status")
              .in_("status", ["proposed", "scheduled"]).execute().data or []):
        if r["status"] == "scheduled" and r.get("tutor_id") in tutor_ids:
            add(r["tutor_id"], r["scheduled_at"], r["duration_minutes"])
        elif r["status"] == "proposed" and r.get("proposed_tutor_id") in tutor_ids:
            add(r["proposed_tutor_id"], r["proposed_at"], r["duration_minutes"])
    return busy


def load_open_groups(now: datetime, pools, windows) -> list[OpenGroup]:
    rows = (db.schema("app").table("makeup_session")
            .select("id, course_id, due_by, proposed_tutor_id, proposed_at")
            .eq("status", "proposed").gt("due_by", now.isoformat()).execute().data or [])
    if not rows:
        return []
    members: dict[str, list] = {}
    for p in (db.schema("app").table("makeup_session_participant")
              .select("makeup_session_id, student_id, status")
              .in_("makeup_session_id", [r["id"] for r in rows]).execute().data or []):
        if p["status"] != "withdrawn":
            members.setdefault(p["makeup_session_id"], []).append(p["student_id"])
    out = []
    for r in rows:
        ids = members.get(r["id"], [])
        m = tuple(Waiting(sid, r.get("course_id"), "month", None, _ts(r["due_by"]),
                          pools.get(sid, ()), windows.get(sid, ())) for sid in ids)
        out.append(OpenGroup(r["id"], r.get("course_id"), len(ids), _ts(r["due_by"]),
                             r.get("proposed_tutor_id"), _ts(r.get("proposed_at")), m))
    return out


# ── persistence ─────────────────────────────────────────────────────────────

def _seat_row(session_id: str, w: Waiting) -> dict:
    return {
        "makeup_session_id": session_id,
        "student_id": w.student_id,
        "funded_by": w.funded_by,
        "period_start": w.period_start.isoformat() if w.period_start else None,
    }


def persist(plan: Plan, open_groups: list[OpenGroup]) -> dict:
    due = {g.session_id: g.due_by for g in open_groups}
    joined = created = proposed = 0
    for session_id, w, _proposal in plan.joins:
        db.schema("app").table("makeup_session_participant").insert(_seat_row(session_id, w)).execute()
        if w.due_by < due.get(session_id, w.due_by):
            db.schema("app").table("makeup_session").update({"due_by": w.due_by.isoformat()}) \
                .eq("id", session_id).execute()
            due[session_id] = w.due_by
        joined += 1
    for group, prop in plan.new_groups:
        session = db.schema("app").table("makeup_session").insert({
            "course_id": group[0].course_id,
            "due_by": min(w.due_by for w in group).isoformat(),
            "proposed_tutor_id": prop.tutor_id,
            "proposed_at": prop.starts_at.isoformat() if prop.starts_at else None,
            "proposal_note": prop.note,
        }).execute().data
        session_id = (session[0] if isinstance(session, list) else session)["id"]
        for w in group:
            db.schema("app").table("makeup_session_participant").insert(_seat_row(session_id, w)).execute()
        created += 1
        proposed += prop.tutor_id is not None
    return {"joined": joined, "created": created, "with_proposal": proposed}


# ── sweeps ──────────────────────────────────────────────────────────────────

def sweep(now: datetime) -> dict:
    """Run before planning. Order matters: withdraw first, then cancel what is
    empty, then cancel what ran out of time (which grants credits)."""
    out = {"withdrawn": 0, "emptied": 0, "not_scheduled": 0}

    proposed = (db.schema("app").table("makeup_session").select("id, due_by")
                .eq("status", "proposed").execute().data or [])
    if not proposed:
        return out
    ids = [r["id"] for r in proposed]
    seats = (db.schema("app").table("makeup_session_participant")
             .select("makeup_session_id, student_id, funded_by, status")
             .in_("makeup_session_id", ids).execute().data or [])

    # 1. A learner whose pod formed no longer needs a month-funded make-up that
    #    hasn't been scheduled yet — IF the pod's sessions in that paid month,
    #    plus their other make-ups for it, already cover the tier's count. A
    #    learner podded late keeps the make-up that covers the shortfall (§2f
    #    item 8).
    podded = _podded_ids(list({s["student_id"] for s in seats}))
    covered = _month_covered(seats, podded, now)
    for s in seats:
        if (s["status"] == "pending" and s["funded_by"] == "month" and s["student_id"] in podded
                and (s["makeup_session_id"], s["student_id"]) in covered):
            (db.schema("app").table("makeup_session_participant").update({"status": "withdrawn"})
             .eq("makeup_session_id", s["makeup_session_id"]).eq("student_id", s["student_id"]).execute())
            s["status"] = "withdrawn"
            out["withdrawn"] += 1

    live: dict[str, int] = {}
    for s in seats:
        if s["status"] != "withdrawn":
            live[s["makeup_session_id"]] = live.get(s["makeup_session_id"], 0) + 1

    for r in proposed:
        # 2. Nobody left in it.
        if live.get(r["id"], 0) == 0:
            _cancel(r["id"], "no_participants")
            out["emptied"] += 1
        # 3. Its deadline passed and nobody scheduled it: our failure, so the
        #    trigger turns each month-funded seat into a credit.
        elif _ts(r["due_by"]) <= now:
            _cancel(r["id"], "not_scheduled")
            out["not_scheduled"] += 1
    return out


def _month_covered(seats: list[dict], podded: set[str], now: datetime) -> set[tuple[str, str]]:
    """(session, student) pairs whose month-funded seat the pod has made
    unnecessary: pod sessions in that seat's paid month + the learner's OTHER
    live month seats for it >= the tier's sessions."""
    pending = [s for s in seats if s["status"] == "pending" and s["funded_by"] == "month"
               and s["student_id"] in podded]
    if not pending:
        return set()
    ids = list({s["student_id"] for s in pending})
    tiers = {r["id"]: r.get("tier") for r in (db.schema("app").table("student").select("id, tier")
                                               .in_("id", ids).execute().data or [])}
    mine = (db.schema("app").table("makeup_session_participant")
            .select("makeup_session_id, student_id, funded_by, period_start, status")
            .in_("student_id", ids).eq("funded_by", "month").execute().data or [])
    periods = {(r["makeup_session_id"], r["student_id"]): _ts(r.get("period_start")) for r in mine}
    pod_times = _pod_session_times(ids, now)
    out = set()
    for s in pending:
        key = (s["makeup_session_id"], s["student_id"])
        start = periods.get(key)
        per_month = SESSIONS_PER_MONTH.get(tiers.get(s["student_id"]) or "", 0)
        if start is None or not per_month:
            continue
        end = start + relativedelta(months=1)
        pod = sum(1 for t in pod_times.get(s["student_id"], ()) if start <= t < end)
        others = sum(1 for r in mine if r["student_id"] == s["student_id"] and r["status"] != "withdrawn"
                     and (r["makeup_session_id"], r["student_id"]) != key and _ts(r.get("period_start")) == start)
        if pod + others >= per_month:
            out.add(key)
    return out


def _cancel(session_id: str, reason: str) -> None:
    (db.schema("app").table("makeup_session")
     .update({"status": "cancelled", "cancelled_reason": reason})
     .eq("id", session_id).execute())


# ── admin: list proposals, schedule ─────────────────────────────────────────

def proposed_sessions() -> list[dict]:
    rows = (db.schema("app").table("makeup_session")
            .select("id, course_id, due_by, duration_minutes, proposed_tutor_id, proposed_at, proposal_note, created_at")
            .eq("status", "proposed").order("due_by").execute().data or [])
    if not rows:
        return []
    seats = (db.schema("app").table("makeup_session_participant")
             .select("makeup_session_id, student_id, funded_by, status")
             .in_("makeup_session_id", [r["id"] for r in rows]).execute().data or [])
    for r in rows:
        r["learners"] = [s for s in seats if s["makeup_session_id"] == r["id"] and s["status"] != "withdrawn"]

    # For the admin screen (§2f item 1): names, and the tutors an override may
    # pick — those in EVERY learner's own pool, the rule schedule() enforces.
    learner_ids = sorted({x["student_id"] for r in rows for x in r["learners"]})
    pools = load_pools(learner_ids) if learner_ids else {}
    for r in rows:
        ids = [x["student_id"] for x in r["learners"]]
        common = set.intersection(*(set(pools.get(i, ())) for i in ids)) if ids else set()
        r["pool_tutor_ids"] = sorted(common)
    names = _names(learner_ids + [t for r in rows for t in r["pool_tutor_ids"]]
                   + [r["proposed_tutor_id"] for r in rows if r.get("proposed_tutor_id")])
    for r in rows:
        for x in r["learners"]:
            x["name"] = names.get(x["student_id"])
        r["proposed_tutor_name"] = names.get(r.get("proposed_tutor_id"))
        r["pool_tutors"] = [{"tutor_id": t, "name": names.get(t)} for t in r.pop("pool_tutor_ids")]
    return rows


def _names(ids) -> dict[str, str]:
    """First and last name only: the rest of a profile row is not this screen's."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = (db.schema("app").table("user_profiles").select("id, first_name, last_name")
            .in_("id", ids).execute().data or [])
    return {r["id"]: " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or "Unnamed"
            for r in rows}


def schedule(session_id: str, tutor_id: str | None = None, starts_at: str | None = None) -> dict:
    """Confirm the proposal, or override it. Either way the tutor must be in
    EVERY learner's own pool (§2g), and must be free then."""
    rows = (db.schema("app").table("makeup_session")
            .select("id, status, due_by, duration_minutes, proposed_tutor_id, proposed_at")
            .eq("id", session_id).execute().data or [])
    if not rows:
        raise ScheduleRefused("no_such_session", "No such make-up session.")
    s = rows[0]
    if s["status"] != "proposed":
        raise ScheduleRefused("not_proposed", f"This make-up is already {s['status']}.")
    tutor = tutor_id or s.get("proposed_tutor_id")
    start = _ts(starts_at) if starts_at else _ts(s.get("proposed_at"))
    if not tutor or not start:
        raise ScheduleRefused("no_proposal", "There is no proposal to confirm; give a tutor and a time.")

    learners = [p["student_id"] for p in
                (db.schema("app").table("makeup_session_participant")
                 .select("student_id, status").eq("makeup_session_id", session_id).execute().data or [])
                if p["status"] != "withdrawn"]
    pools = load_pools(learners)
    outside = [sid for sid in learners if tutor not in pools.get(sid, ())]
    if outside:
        raise ScheduleRefused("tutor_not_in_pool",
                              "That tutor is not the home or a backup tutor of every learner in this make-up.")

    end = start + timedelta(minutes=int(s["duration_minutes"]))
    if end > _ts(s["due_by"]):
        raise ScheduleRefused("after_deadline", "That time ends after the make-up's deadline.")
    own = (s.get("proposed_tutor_id"), _ts(s.get("proposed_at")))
    for a, b in load_busy([tutor], start - timedelta(hours=2)).get(tutor, []):
        if (tutor, a) == own:
            continue    # this session's own pending proposal
        if a < end and start < b:
            raise ScheduleRefused("tutor_busy", "The tutor has another session at that time.")

    db.schema("app").table("makeup_session").update({
        "tutor_id": tutor, "scheduled_at": start.isoformat(), "status": "scheduled",
    }).eq("id", session_id).execute()
    return {"session_id": session_id, "tutor_id": tutor, "scheduled_at": start.isoformat(),
            "duration_minutes": s["duration_minutes"]}


# ── credits outside the planner ─────────────────────────────────────────────

def credit_balance(student_id: str) -> int:
    rows = (db.schema("app").table("session_credit").select("direction, quantity")
            .eq("student_id", student_id).execute().data or [])
    return sum(r["quantity"] if r["direction"] == "grant" else -r["quantity"] for r in rows)


def grant_credit(student_id: str, reason: str) -> None:
    """An upheld complaint or an admin correction. Session-linked grants
    (tutor issue, not scheduled) are made by the §92 trigger, not here."""
    if reason not in ("complaint", "admin"):
        raise ValueError("grant_credit: reason must be 'complaint' or 'admin'")
    db.schema("app").table("session_credit").insert({
        "student_id": student_id, "direction": "grant", "reason": reason,
    }).execute()


def forfeit_on_cancellation(student_id: str) -> int:
    """§2e: cancelling forfeits unused carried-forward credits. The learner was
    told before paying and again before confirming the cancel. Returns the
    number forfeited."""
    balance = credit_balance(student_id)
    if balance > 0:
        db.schema("app").table("session_credit").insert({
            "student_id": student_id, "direction": "forfeit",
            "reason": "cancellation", "quantity": balance,
        }).execute()
    return max(balance, 0)


# ── the daily run ───────────────────────────────────────────────────────────

def run(now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    swept = {} if dry_run else sweep(now)
    waiting = [w for f in load_facts() if (w := needs_seat(f, now)) is not None]

    groups = load_open_groups(now, {}, {})
    ids = {w.student_id for w in waiting} | {m.student_id for g in groups for m in g.members}
    pools = load_pools(list(ids))
    windows = load_learner_windows(list(ids))

    def enrich(w: Waiting) -> Waiting:
        return replace(w, pool=pools.get(w.student_id, ()), windows=windows.get(w.student_id, ()))

    waiting = [enrich(w) for w in waiting]
    groups = [replace(g, members=tuple(enrich(m) for m in g.members)) for g in groups]

    tutors = sorted({t for p in pools.values() for t in p})
    plan = plan_makeups(waiting, groups, load_tutor_windows(tutors), load_busy(tutors, now), now)
    result = {"swept": swept, "waiting": len(waiting), "joins": len(plan.joins),
              "new_groups": [{"size": len(g), "tutor": p.tutor_id, "at": p.starts_at.isoformat() if p.starts_at else None,
                              "note": p.note} for g, p in plan.new_groups]}
    if not dry_run:
        result["persisted"] = persist(plan, groups)
    return result

