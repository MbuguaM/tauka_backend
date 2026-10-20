"""The milestone tracker (enrolment plan 4.7, playbook "live milestone tracker").

Decided 2026-10-18 (developer):
  * B2 ("fluent") is LESSON 60, the end of FSI Phase 1 (the pricing page's
    reading; the home page's "B1+" wording is the one to correct).
  * No C1 date yet: there is no Phase 2 curriculum to project from.
  * Status is measured against the learner's TIER UNLOCK PACE — Learner 2,
    Tutor 4, Intensive 6 lessons a paid month (`lesson_service`):
      expected = the lessons their paid months have unlocked, spread evenly
                 across each month (a month's four lessons are not all "due"
                 on the billing day);
      done     = content lessons completed (`unit_progress`);
      on track: behind by less than 1 · slipping: 1 to under 3 · behind: 3+.
    Each status comes with ONE concrete fix.

E-16. This is a DISPLAY. Nothing reads it to decide anything: not matching,
not pay, not the milestone guarantee (which is Intensive-only and must use
tutor-marked attendance and exercise results, never this). Its inputs are
lesson grants and lesson completions, never `activity_summary` or `ai_usage`;
and, since §141, the latest unit assessment: a not_yet adds two weeks to the
date and makes reviewing the weak lessons the fix.

The date is an ESTIMATE except on Intensive, the only tier the B2-in-a-year
promise covers (§2d). The clock starts at pod formation (playbook); the
formation date is returned for the app to show.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services.lesson_service import LESSONS_PER_MONTH
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

B2_LESSON = 60
MONTH = timedelta(days=30)
SLIPPING_AT = 1.0
BEHIND_AT = 3.0
NEXT_TIER = {"learner_tier": "tutor_tier", "tutor_tier": "intensive_tier"}
TIER_NAME = {"learner_tier": "Learner", "tutor_tier": "Tutor", "intensive_tier": "Intensive"}


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _b2_lesson() -> int:
    """`tracker.b2_lesson` in app.config if set, else 60."""
    try:
        rows = (_t("config").select("value").eq("key", "tracker.b2_lesson").limit(1).execute()).data or []
        return max(1, int((rows[0] if rows else {}).get("value") or B2_LESSON))
    except Exception:  # noqa: BLE001
        return B2_LESSON


# ── the pure part ────────────────────────────────────────────────────────────

def expected_slots(grants: list[dict], now: datetime) -> float:
    """Lesson slots a learner should have worked through by `now`: each grant's
    lessons spread evenly over its 30 days. Read-ahead is optional, so it
    doesn't count; revoked grants don't either."""
    total = 0.0
    for g in grants:
        if g.get("revoked_at") or g.get("source") == "read_ahead":
            continue
        start = _ts(g["available_from"])
        if start is None or start > now:
            continue
        count = int(g["slot_to"]) - int(g["slot_from"])
        total += count * min(1.0, (now - start) / MONTH)
    return total


def assess(tier: str | None, done: int, expected: float, now: datetime, b2: int = B2_LESSON) -> dict:
    """Status, projected B2 date and one fix, from numbers only. Pure."""
    pace = LESSONS_PER_MONTH.get(tier or "", 0)
    out: dict = {"tier": tier, "lessons_done": done, "b2_lesson": b2, "expected": round(expected, 1)}
    if done >= b2:
        return {**out, "status": "reached", "projected_b2": None, "estimate": False,
                "fix": "You've reached lesson %d. Well done." % b2}
    if pace <= 0:
        return {**out, "status": "no_pace", "projected_b2": None, "estimate": True,
                "fix": "A paid plan unlocks lessons each month and gives you a date to aim for."}
    gap = expected - done
    status = "on_track" if gap < SLIPPING_AT else "slipping" if gap < BEHIND_AT else "behind"
    months = (b2 - done) / pace
    projected = now + MONTH * months
    nxt = done + 1
    if status == "on_track":
        fix = f"Keep going: lesson {nxt} is next."
    elif status == "slipping":
        fix = f"Finish lesson {nxt} this week to get back on track."
    else:
        behind = int(gap)
        # One extra lesson a week over the weeks it takes to clear the backlog.
        fix = (f"You're {behind} lessons behind. One extra lesson a week clears that in about "
               f"{behind} weeks.")
    up = NEXT_TIER.get(tier or "")
    saves = None
    if up:
        faster = (b2 - done) / LESSONS_PER_MONTH[up]
        saves = max(0, round(months - faster))
        if status == "behind" or saves >= 3:
            fix += f" {TIER_NAME[up]} would bring lesson {b2} about {saves} months sooner."
    return {**out, "status": status, "projected_b2": projected.date().isoformat(),
            "estimate": tier != "intensive_tier", "fix": fix, "upgrade_saves_months": saves}


def apply_assessment(out: dict, latest: dict | None, not_yet_weeks: int = 2) -> dict:
    """Fold the latest unit assessment (§141) into the tracker. Pure.

    A not_yet pushes the projected date back `not_yet_weeks` (time to review)
    and makes the review the fix. The status itself stays the pace measure:
    an assessment says how well, the status says how far. Display only (E-16).
    """
    out = {**out, "assessment": latest}
    if not latest or latest.get("outcome") != "not_yet":
        return out
    if out.get("projected_b2"):
        d = datetime.fromisoformat(out["projected_b2"]) + timedelta(weeks=not_yet_weeks)
        out["projected_b2"] = d.date().isoformat()
    weak = latest.get("weak_lessons") or []
    which = ("lessons " + ", ".join(str(n) for n in weak)) if weak else         f"lessons {latest['lesson_to'] - 9}–{latest['lesson_to']}"
    out["fix"] = f"Review {which} before moving on: your last unit check said not yet. " + out["fix"]
    return out


# ── loading ──────────────────────────────────────────────────────────────────

def _preliminary_count(course_id: str | None) -> int:
    if not course_id:
        return 2
    rows = (_t("unit").select("id, is_preliminary, deleted_at").eq("course_id", course_id).execute()).data or []
    return sum(1 for r in rows if r.get("is_preliminary") and not r.get("deleted_at")) or 2


def for_student(student_id: str, now: datetime | None = None) -> dict:
    from app.services.intake_service import _current_lesson_map
    now = now or _now()
    st = (_t("student").select("tier").eq("id", student_id).limit(1).execute()).data or []
    tier = (st[0] if st else {}).get("tier")
    course = (_t("student_course").select("course_id").eq("student_id", student_id).limit(1).execute()).data or []
    prelim = _preliminary_count((course[0] if course else {}).get("course_id"))
    grants = (_t("lesson_entitlement").select("slot_from, slot_to, source, available_from, revoked_at")
              .eq("student_id", student_id).execute()).data or []
    # Phase 0's slots come first; only content lessons count towards lesson 60.
    expected = max(0.0, expected_slots(grants, now) - prelim)
    done = max(0, _current_lesson_map([student_id]).get(student_id, 1) - 1)
    out = assess(tier, done, expected, now, _b2_lesson())
    try:
        from app.services.unit_assessment_service import latest
        out = apply_assessment(out, latest(student_id))
    except Exception as exc:  # noqa: BLE001 — the tracker stands without it
        logger.warning("unit assessment for the tracker of %s failed: %s", student_id, exc)
    pod = (_t("cohort_memberships").select("cohort_id").eq("user_id", student_id).eq("status", "active")
           .limit(1).execute()).data or []
    formed = None
    if pod:
        c = (_t("cohorts").select("start_date").eq("id", pod[0]["cohort_id"]).limit(1).execute()).data or []
        formed = (c[0] if c else {}).get("start_date")
    return {**out, "clock_started": formed}


def for_tutor(tutor_id: str, now: datetime | None = None) -> list[dict]:
    """A tutor's pods with each member's STATUS and lesson — not dates (the
    playbook: tutors see their pod's statuses at each checkpoint)."""
    from app.services.makeup_service import _names
    now = now or _now()
    pods = (_t("cohorts").select("id, name").eq("tutor_id", tutor_id).eq("status", "active").execute()).data or []
    out = []
    for p in pods:
        members = [m["user_id"] for m in (_t("cohort_memberships").select("user_id").eq("cohort_id", p["id"])
                                          .eq("status", "active").execute()).data or []]
        names = _names(members)
        rows = []
        for sid in members:
            try:
                t = for_student(sid, now)
                rows.append({"student_id": sid, "name": names.get(sid), "status": t["status"],
                             "lessons_done": t["lessons_done"]})
            except Exception as exc:  # noqa: BLE001
                logger.warning("tracker for %s failed: %s", sid, exc)
        out.append({"cohort_id": p["id"], "name": p.get("name"), "members": rows})
    return out
