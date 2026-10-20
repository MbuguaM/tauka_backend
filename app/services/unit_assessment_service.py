"""Unit assessments — playbook "At each checkpoint" and the milestone tracker, §141.

Decided 2026-10-19 (developer):
  * one per BLOCK of ten content lessons: block k covers lessons 10k-9 .. 10k,
    opened once the learner has completed lesson 10k (lesson 60 = block 6 = B2);
  * two parts:
      quiz     — taken in the app, 20 items drawn from the block's own drills;
                 the app reports how many were right and in which lessons
                 something went wrong;
      speaking — rated by one of the learner's tutors (home, guest, or the
                 tutor of their pod) or ops: pronunciation, accuracy, fluency,
                 comprehension, 1-4 each;
  * the result INFORMS only (E-16): the learner, their tutors and ops see it;
    the checkpoint snapshot carries it; a not_yet nudges the tracker's date.
    Nothing here locks a lesson, moves a learner or touches pay.

Outcome, once both parts are in: the weaker of the two.
  quiz:     >= 80 % secure, >= 60 % developing, else not_yet
  speaking: mean >= 3 secure, >= 2 developing, else not_yet

The quiz score is reported by the app. That is acceptable only because the
result decides nothing; if it ever gates anything it must be scored here.
"""

import logging
from datetime import datetime, timezone

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

BLOCK = 10
MAX_BLOCKS = 6                     # lesson 60 is B2 (tracker_service)
RUBRIC = ("pronunciation", "accuracy", "fluency", "comprehension")
OUTCOMES = ("not_yet", "developing", "secure")      # worst first


class UnitAssessmentRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── the pure part ────────────────────────────────────────────────────────────

def block_span(block: int) -> tuple[int, int]:
    return BLOCK * (block - 1) + 1, BLOCK * block


def blocks_reached(lessons_done: int, max_blocks: int = MAX_BLOCKS) -> list[int]:
    return list(range(1, min(max_blocks, lessons_done // BLOCK) + 1))


def quiz_band(correct: int, total: int) -> str:
    pct = correct / total if total else 0
    return "secure" if pct >= 0.8 else "developing" if pct >= 0.6 else "not_yet"


def speaking_band(scores: dict) -> str:
    mean = sum(int(scores[k]) for k in RUBRIC) / len(RUBRIC)
    return "secure" if mean >= 3 else "developing" if mean >= 2 else "not_yet"


def outcome(row: dict) -> str | None:
    if row.get("quiz_total") is None or not row.get("speaking"):
        return None
    a = quiz_band(int(row["quiz_correct"]), int(row["quiz_total"]))
    b = speaking_band(row["speaking"])
    return min(a, b, key=OUTCOMES.index)


# ── loading ──────────────────────────────────────────────────────────────────

def _course_of(student_id: str) -> str | None:
    # app.student_course (tauka-flutter): one row per enrolled course.
    rows = (_t("student_course").select("course_id").eq("student_id", student_id).limit(1).execute()).data or []
    return (rows[0] if rows else {}).get("course_id")


def _lessons(course_id: str, lo: int, hi: int) -> list[dict]:
    """The content lessons lo..hi. Content units are those with a level (§85.4);
    preliminary units collide on unit_order and are left out."""
    # app.unit (tauka-flutter): id, title, unit_order, level, deleted_at.
    rows = (_t("unit").select("id, title, unit_order, level, deleted_at").eq("course_id", course_id)
            .gte("unit_order", lo).lte("unit_order", hi).execute()).data or []
    return sorted(({"unit_id": r["id"], "lesson": r["unit_order"], "title": r.get("title")}
                   for r in rows if r.get("level") is not None and not r.get("deleted_at")),
                  key=lambda r: r["lesson"])


def _done(student_id: str) -> int:
    from app.services.intake_service import _current_lesson_map
    return max(0, _current_lesson_map([student_id]).get(student_id, 1) - 1)


def _rows(student_id: str) -> list[dict]:
    return (_t("unit_assessment").select("*").eq("student_id", student_id).execute()).data or []


def sync(student_id: str) -> list[dict]:
    """Open every block the learner has reached and doesn't have yet."""
    course = _course_of(student_id)
    if not course:
        return []
    have = {r["block"] for r in _rows(student_id) if r.get("course_id") == course}
    opened = []
    for b in blocks_reached(_done(student_id)):
        if b in have:
            continue
        lo, hi = block_span(b)
        try:
            opened.append((_t("unit_assessment").insert({
                "student_id": student_id, "course_id": course, "block": b,
                "lesson_from": lo, "lesson_to": hi, "opened_at": _now().isoformat(),
            }).execute()).data[0])
        except Exception as exc:  # noqa: BLE001 — a concurrent open of the same block
            logger.info("unit assessment %s/%s already open: %s", student_id, b, exc)
    return opened


def _view(r: dict, with_lessons: bool = False) -> dict:
    out = {k: r.get(k) for k in ("id", "course_id", "block", "lesson_from", "lesson_to", "opened_at", "quiz_total",
                                 "quiz_correct", "quiz_at", "weak_lessons", "speaking", "speaking_at",
                                 "outcome", "decided_at")}
    out["quiz_band"] = quiz_band(r["quiz_correct"], r["quiz_total"]) if r.get("quiz_total") else None
    out["speaking_band"] = speaking_band(r["speaking"]) if r.get("speaking") else None
    if with_lessons and r.get("quiz_total") is None:
        out["lessons"] = _lessons(r["course_id"], r["lesson_from"], r["lesson_to"])
    return out


def mine(student_id: str) -> dict:
    try:
        sync(student_id)
    except Exception as exc:  # noqa: BLE001 — showing what exists still works
        logger.warning("unit assessment sync for %s failed: %s", student_id, exc)
    rows = sorted(_rows(student_id), key=lambda r: r["block"])
    return {"assessments": [_view(r, with_lessons=True) for r in rows]}


def latest(student_id: str) -> dict | None:
    """The most recent decided assessment, for the tracker and checkpoints."""
    done = [r for r in _rows(student_id) if r.get("outcome")]
    if not done:
        return None
    r = max(done, key=lambda r: r["block"])
    return {"block": r["block"], "outcome": r["outcome"], "weak_lessons": r.get("weak_lessons") or [],
            "lesson_to": r["lesson_to"]}


# ── the quiz ─────────────────────────────────────────────────────────────────

def _one(assessment_id: str) -> dict:
    rows = (_t("unit_assessment").select("*").eq("id", assessment_id).limit(1).execute()).data or []
    if not rows:
        raise UnitAssessmentRefused("not_found")
    return rows[0]


def _decide(row: dict) -> dict:
    o = outcome(row)
    if o is None:
        return row
    return (_t("unit_assessment").update({"outcome": o, "decided_at": _now().isoformat()})
            .eq("id", row["id"]).execute()).data[0]


def submit_quiz(student_id: str, assessment_id: str, correct: int, total: int, weak_lessons: list[int]) -> dict:
    row = _one(assessment_id)
    if row["student_id"] != student_id:
        raise UnitAssessmentRefused("not_your_assessment")
    if row.get("quiz_total") is not None:
        raise UnitAssessmentRefused("already_taken")
    if not 5 <= total <= 40 or not 0 <= correct <= total:
        raise UnitAssessmentRefused("bad_score")
    weak = sorted({int(n) for n in weak_lessons if row["lesson_from"] <= int(n) <= row["lesson_to"]})
    row = (_t("unit_assessment").update({"quiz_total": total, "quiz_correct": correct,
                                         "weak_lessons": weak, "quiz_at": _now().isoformat()})
           .eq("id", assessment_id).execute()).data[0]
    return _view(_decide(row))


# ── the speaking check ───────────────────────────────────────────────────────

def _pod_tutor(student_id: str) -> str | None:
    # app.cohort_memberships / app.cohorts (tauka-flutter): the learner's live pod and its tutor.
    m = (_t("cohort_memberships").select("cohort_id").eq("user_id", student_id).eq("status", "active")
         .limit(1).execute()).data or []
    if not m:
        return None
    c = (_t("cohorts").select("tutor_id").eq("id", m[0]["cohort_id"]).limit(1).execute()).data or []
    return (c[0] if c else {}).get("tutor_id")


def may_rate(actor: str, student_id: str) -> bool:
    if actor == student_id:
        return False
    return (actor in hs.linked_tutor_ids(student_id) or actor == _pod_tutor(student_id)
            or hs.admin_can(actor, "ops"))


def rate_speaking(actor: str, assessment_id: str, scores: dict) -> dict:
    row = _one(assessment_id)
    if not may_rate(actor, row["student_id"]):
        raise UnitAssessmentRefused("not_allowed")
    if row.get("speaking"):
        raise UnitAssessmentRefused("already_rated")
    if set(scores) != set(RUBRIC) or not all(1 <= int(v) <= 4 for v in scores.values()):
        raise UnitAssessmentRefused("bad_score")
    clean = {k: int(scores[k]) for k in RUBRIC}
    row = (_t("unit_assessment").update({"speaking": clean, "speaking_by": actor,
                                         "speaking_at": _now().isoformat()})
           .eq("id", assessment_id).execute()).data[0]
    return _view(_decide(row))


def to_rate(actor: str) -> list[dict]:
    """Speaking checks waiting for this tutor: their linked learners and pod members."""
    from app.services.makeup_service import _names
    # app.student_tutor_link (tauka-flutter schema, tauka-python writes): active links.
    learners = {r["student_id"] for r in (_t("student_tutor_link").select("student_id").eq("tutor_id", actor)
                                          .eq("state", "active").execute()).data or []}
    for p in (_t("cohorts").select("id").eq("tutor_id", actor).eq("status", "active").execute()).data or []:
        learners |= {m["user_id"] for m in (_t("cohort_memberships").select("user_id").eq("cohort_id", p["id"])
                                            .eq("status", "active").execute()).data or []}
    if not learners:
        return []
    rows = [r for r in (_t("unit_assessment").select("*").in_("student_id", sorted(learners))
                        .is_("speaking", "null").execute()).data or [] if not r.get("speaking")]
    names = _names([r["student_id"] for r in rows])
    return sorted(({**_view(r), "student_id": r["student_id"], "name": names.get(r["student_id"])} for r in rows),
                  key=lambda r: r["opened_at"] or "")


# ── daily ────────────────────────────────────────────────────────────────────

def daily() -> int:
    """Open reached blocks for learners who haven't opened the app, so their
    tutors see the speaking check due."""
    rows = (_t("unit_progress").select("user_id").eq("status", "completed").execute()).data or []
    opened = 0
    for sid in sorted({r["user_id"] for r in rows if r.get("user_id")}):
        try:
            opened += len(sync(sid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("unit assessment sync for %s failed: %s", sid, exc)
    return opened
