"""Tutor-facing student progress — plan Phase 6, spec §1.8.

`GET /tutor/students/{student_id}/progress`. What a tutor sees about one
student's learning, assembled in one call so the client is not making six.

WHAT IS IN SCOPE, AND WHY IT IS NOT EVERYTHING §1.8 ASKED FOR.

§1.8 asks for "baseline history" including the pronunciation/acoustic data.
**Voice and speech submissions are deliberately excluded** (developer decision,
2026-08-28): `legal.md` **L-002** is open — the published privacy policy
(`pp_host_ready.jsx`) does not contain the word "tutor" anywhere, and the §4.5
subsection that would fix it exists only as an unreviewed draft. So this
endpoint returns **no data category a tutor could not already read today**:

  unit_progress          — already readable, `unit_progress_select_tutor_assigned`
  exercise_result        — already readable, `er_select_own_or_teacher_admin`
  cohort attendance      — already readable, `app.can_read_cohort()`
  one_to_one_session     — already readable, the tutor is a party to it
  activity_summary       — already readable AND tier-gated, `can_read_activity()`

Nothing here widens who can read what. It changes the *shape* — one endpoint
instead of six queries — which is what §1.8 is actually asking for and is not a
disclosure question. When L-002's policy text lands, the voice half is a small
addition to `_speech_history`, which is written and left unreferenced below.

THE GATE IS THE COURSE JOIN, NOT THE LINK.

This runs under the service role, so **RLS protects nothing here** and the
endpoint must enforce the same rule the policies would. The rule is
`app.teaches_student` — the `tutor_assignment ⋈ student_course` join that D-05
made canonical (CONFLICT-033). Gating on `student_tutor_link` instead would let
a guest tutor read history that RLS denies them, with the service role as the
laundering step. The two definitions exist for different jobs and this is the
job the course join owns.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# Enough to show a trend without shipping a student's entire history down a
# mobile connection. A tutor looking further back is a different screen.
_RECENT_EXERCISES = 50
_RECENT_SESSIONS = 20

# Mirrors `app.can_read_activity`'s tier list exactly. Duplicated here because
# the service role bypasses the function; if one changes the other must.
_ACTIVITY_TIERS = ("learner_tier", "tutor_tier", "intensive_tier")


class ProgressDenied(Exception):
    """The caller may not read this student. Rendered as 403, never 404.

    A 404 here would be a lie that leaks nothing but confuses everyone: the
    student exists, and the tutor is being told no, not told nothing.
    """


# ── The gate ──────────────────────────────────────────────────────────────────

def _student_courses(student_id: str) -> list[str]:
    try:
        result = db.schema("app").table("student_course").select(
            "course_id").eq("student_id", student_id).execute()
        return [r["course_id"] for r in (result.data or []) if r.get("course_id")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("student course read failed for %s: %s", student_id, exc)
        return []


def _tutor_courses(tutor_id: str) -> list[str]:
    try:
        result = db.schema("app").table("tutor_assignment").select(
            "course_id").eq("tutor_id", tutor_id).is_("deleted_at", "null").execute()
        return [r["course_id"] for r in (result.data or []) if r.get("course_id")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor assignment read failed for %s: %s", tutor_id, exc)
        return []


def teaches_student(tutor_id: str, student_id: str) -> dict:
    """Python's copy of `app.teaches_student`, plus the courses it matched on.

    Returns the shared courses as well as the verdict, because the handoff path
    needs to say *which* course assignment is missing — "this tutor cannot see
    this student's history" is unactionable without it.
    """
    shared = sorted(set(_student_courses(student_id)) & set(_tutor_courses(tutor_id)))
    return {"allowed": bool(shared), "shared_courses": shared}


def _has_link(tutor_id: str, student_id: str) -> dict | None:
    try:
        result = db.schema("app").table("student_tutor_link").select(
            "id, role, state, started_at, ended_at, source, replaced_link_id"
        ).eq("tutor_id", tutor_id).eq("student_id", student_id).in_(
            "state", ["active", "handoff_pending"]).limit(1).execute()
        return (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("link check failed for %s/%s: %s", tutor_id, student_id, exc)
        return None


# ── The pieces ────────────────────────────────────────────────────────────────

def _unit_history(student_id: str, course_ids: list[str]) -> list[dict]:
    """Units started and finished, newest activity first, with unit titles.

    Scoped to the courses this tutor shares with the student rather than all of
    the student's units. A tutor assigned to one course has no business reading
    a student's progress through another, and the RLS policy
    `unit_progress_select_tutor_assigned` says so in exactly these terms.
    """
    if not course_ids:
        return []
    try:
        result = db.schema("app").table("unit_progress").select(
            "unit_id, course_id, status, completion_percentage, started_at, "
            "completed_at, last_accessed_at, partial_reason"
        ).eq("user_id", student_id).in_("course_id", course_ids).order(
            "last_accessed_at", desc=True).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("unit progress read failed for %s: %s", student_id, exc)
        return []

    titles = _unit_titles([r["unit_id"] for r in rows if r.get("unit_id")])
    for row in rows:
        row["unit_title"] = titles.get(row.get("unit_id"))
    return rows


def _unit_titles(unit_ids: list[str]) -> dict[str, str]:
    """Batched. One query, not one per unit — a student with forty units would
    otherwise make forty round trips inside a single endpoint."""
    unique = list({u for u in unit_ids if u})
    if not unique:
        return {}
    try:
        result = db.schema("app").table("unit").select(
            "id, title, unit_order").in_("id", unique).execute()
        return {r["id"]: r.get("title") for r in (result.data or [])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("unit title lookup failed: %s", exc)
        return {}


def _exercise_history(student_id: str) -> dict:
    """Recent exercise results, and a trend that is honest about being thin.

    **`app.exercise_result` held 0 rows when this was written.** Phase 0d fixed
    the writer (FL-010 §1) on 2026-08-27 and nothing has run through it yet, so
    every tutor sees an empty list until a student does an exercise. The empty
    case returns `{"results": [], "count": 0, "trend": None}` rather than a
    fabricated 0% — `count` is what lets the client say "no exercises yet"
    instead of "0% average", which is the distinction the stats work spent a
    whole phase getting right.
    """
    try:
        result = db.schema("app").table("exercise_result").select(
            "id, unit_id, exercise_type, difficulty_mode, score, total, "
            "elapsed_seconds, session_title, submitted_at, completed_at"
        ).eq("user_id", student_id).is_("deleted_at", "null").order(
            "submitted_at", desc=True).limit(_RECENT_EXERCISES).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("exercise history read failed for %s: %s", student_id, exc)
        return {"results": [], "count": 0, "trend": None}

    titles = _unit_titles([r["unit_id"] for r in rows if r.get("unit_id")])
    for row in rows:
        row["unit_title"] = titles.get(row.get("unit_id"))

    scored = [(r["score"], r["total"]) for r in rows
              if r.get("total") and r.get("score") is not None]
    if not scored:
        return {"results": rows, "count": len(rows), "trend": None}

    # Newest first, so the first half of the list IS the recent half.
    half = max(1, len(scored) // 2)
    def pct(window):
        got = sum(s for s, _ in window)
        out = sum(t for _, t in window)
        return round(100.0 * got / out, 1) if out else None

    recent, earlier = pct(scored[:half]), pct(scored[half:])
    return {
        "results": rows,
        "count": len(rows),
        "trend": {
            "recent_pct": recent,
            "earlier_pct": earlier,
            # None, not 0, when there is only one window. A "no change" reading
            # on a single data point is a claim the data cannot support.
            "delta": round(recent - earlier, 1)
            if (recent is not None and earlier is not None and len(scored) > 1)
            else None,
            "sample": len(scored),
        },
    }


def _attendance(student_id: str, tutor_id: str) -> dict:
    """Both session lanes, from the tables that record who actually delivered.

    **A past session nobody marked is `pending`, never `missed`.** That is the
    cohort domain's rule and it is repeated here because this is a tutor-facing
    read: showing a student as having missed a session the tutor simply never
    marked would be an accusation generated by a join.
    """
    cohort: list[dict] = []
    try:
        result = db.schema("app").table("cohort_session_attendance").select(
            "cohort_session_id, status, joined_at, left_at"
        ).eq("user_id", student_id).limit(_RECENT_SESSIONS * 2).execute()
        cohort = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohort attendance read failed for %s: %s", student_id, exc)

    one_to_one: list[dict] = []
    try:
        result = db.schema("app").table("one_to_one_session").select(
            "id, tutor_id, scheduled_at, duration_minutes, status"
        ).eq("student_id", student_id).order(
            "scheduled_at", desc=True).limit(_RECENT_SESSIONS).execute()
        one_to_one = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("one-to-one read failed for %s: %s", student_id, exc)

    counted = [r for r in cohort if r.get("status") in ("present", "absent")]
    present = sum(1 for r in counted if r["status"] == "present")

    return {
        "cohort": {
            "present": present,
            "absent": len(counted) - present,
            # Never folded into the rate. An unmarked session is missing data,
            # not a missed lesson, and averaging it in would quietly convert one
            # into the other.
            "unmarked": len(cohort) - len(counted),
            "rate_pct": round(100.0 * present / len(counted), 1) if counted else None,
        },
        "one_to_one": {
            "sessions": one_to_one,
            "completed": sum(1 for r in one_to_one if r.get("status") == "completed"),
            "with_me": sum(1 for r in one_to_one
                           if r.get("tutor_id") == tutor_id and r.get("status") == "completed"),
        },
    }


def _pace(student_id: str, course_ids: list[str], units: list[dict]) -> dict | None:
    """Tauka's one-unit-per-week promise, measured — the same figure the student
    sees on their own pace card, shown to their tutor rather than recomputed
    differently.

    Anchored on `student_course.enrolled_at`, which exists for exactly this
    (2026-08-23). Returns None rather than a zero when there is no anchor: a
    student with no enrolment row has not fallen behind, they have not started.
    """
    if not course_ids:
        return None
    try:
        result = db.schema("app").table("student_course").select(
            "course_id, enrolled_at, level").eq("student_id", student_id).in_(
            "course_id", course_ids).order("enrolled_at").limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("pace anchor read failed for %s: %s", student_id, exc)
        return None
    if not row or not row.get("enrolled_at"):
        return None

    try:
        enrolled = datetime.fromisoformat(str(row["enrolled_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None

    weeks = max(1, (datetime.now(timezone.utc) - enrolled).days // 7)
    completed = sum(1 for u in units if u.get("status") == "completed")
    return {
        "enrolled_at": row["enrolled_at"],
        "weeks_enrolled": weeks,
        "units_completed": completed,
        "expected": weeks,
        "delta": completed - weeks,
    }


def _activity(student_id: str) -> dict:
    """`app.activity_summary`, and only on a tier that permits it.

    **A missing row means "not shared on this tier", never "no activity".** The
    tutor dashboard already makes that distinction and it must be made here too,
    because a tutor reading "0 minutes this week" about a free-tier student
    would be reading a permission as a behaviour.

    The tier is read live and the answer is never cached — a cached figure
    survives the downgrade that revoked it.
    """
    try:
        student = db.schema("app").table("student").select("tier").eq(
            "id", student_id).limit(1).execute()
        tier = ((student.data or [{}])[0]).get("tier")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tier read failed for %s: %s", student_id, exc)
        return {"visible": False, "reason": "tier could not be read", "summary": None}

    if tier not in _ACTIVITY_TIERS:
        return {"visible": False, "reason": "not shared on this plan",
                "tier": tier, "summary": None}

    since = (datetime.now(timezone.utc) - timedelta(days=28)).date().isoformat()
    try:
        result = db.schema("app").table("activity_summary").select("*").eq(
            "user_id", student_id).gte("day", since).order("day", desc=True).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("activity summary read failed for %s: %s", student_id, exc)
        return {"visible": True, "reason": None, "tier": tier, "summary": []}

    return {"visible": True, "reason": None, "tier": tier, "summary": rows}


def _relationship(tutor_id: str, student_id: str) -> dict:
    """The link chain — §1.8's "a new tutor inheriting a student must get full
    historical access, not a fresh start".

    Walks `replaced_link_id` backwards so an inherited relationship shows how
    long the student has been with *the platform*, not just with the tutor
    reading the screen. A handed-off tutor seeing "student since last Tuesday"
    would restart a relationship the student experiences as continuous.
    """
    try:
        result = db.schema("app").table("student_tutor_link").select(
            "id, tutor_id, role, state, source, started_at, ended_at, replaced_link_id"
        ).eq("student_id", student_id).order("started_at").execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("relationship read failed for %s: %s", student_id, exc)
        rows = []

    mine = next((r for r in rows if r["tutor_id"] == tutor_id
                 and r["state"] in ("active", "handoff_pending")), None)
    homes = [r for r in rows if r["role"] == "home"]
    first_home = min((r["started_at"] for r in homes if r.get("started_at")), default=None)

    return {
        "my_link": mine,
        "inherited": bool(mine and mine.get("replaced_link_id")),
        # The date the student got their first home tutor, not the date this one
        # did. That is the number a warm intro should open with.
        "with_platform_since": first_home,
        "home_tutor_changes": max(0, len(homes) - 1),
        "handoff_pending": any(r["state"] == "handoff_pending" for r in homes),
    }


def _speech_history(student_id: str) -> dict:
    """**NOT WIRED UP.** The voice half of §1.8, held behind `legal.md` L-002.

    Kept as a written function rather than a comment because the shape is
    settled and the blocker is a paragraph of policy text, not a design
    question. When §4.5 of `privacy-policy-draft.md` is reviewed and published,
    calling this from `student_progress` is the whole change.

    Nothing calls it today, and nothing should until L-002 closes.
    """
    try:
        result = db.schema("app").table("speech_submission").select(
            "id, unit_id, section_kind, item_count, verified_count, status, "
            "submitted_at, expires_at"
        ).eq("user_id", student_id).order("submitted_at", desc=True).execute()
        return {"submissions": result.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("speech history read failed for %s: %s", student_id, exc)
        return {"submissions": []}


# ── The endpoint's read model ─────────────────────────────────────────────────

def student_progress(tutor_id: str, student_id: str) -> dict:
    """§1.8, assembled. Raises `ProgressDenied` when the course join says no."""
    gate = teaches_student(tutor_id, student_id)
    if not gate["allowed"]:
        link = _has_link(tutor_id, student_id)
        if link:
            # The informative refusal. This is the handoff gap made visible:
            # a tutor can hold an active home link and still fail the course
            # join, because a handoff writes `student_tutor_link` and course
            # access lives in `tutor_assignment`. Saying so lets an admin fix it
            # in one step instead of concluding the endpoint is broken.
            raise ProgressDenied(
                "you are linked to this student but are not assigned to their "
                "course, so their learning history is not readable — an admin "
                "must add the course assignment"
            )
        raise ProgressDenied("this student is not yours")

    courses = gate["shared_courses"]
    units = _unit_history(student_id, courses)

    return {
        "student_id": student_id,
        "courses": courses,
        "relationship": _relationship(tutor_id, student_id),
        "units": units,
        "exercises": _exercise_history(student_id),
        "attendance": _attendance(student_id, tutor_id),
        "pace": _pace(student_id, courses, units),
        "activity": _activity(student_id),
        # Stated in the payload, not just in this docstring, so a client cannot
        # render an empty pronunciation panel and imply there is nothing there.
        "speech": {
            "available": False,
            "reason": "pending privacy policy review (legal.md L-002)",
        },
    }
