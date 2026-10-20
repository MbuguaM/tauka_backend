"""Four-dimension session ratings, as tutors see them (enrolment plan 4.6, §116).

Decided 2026-10-18 (developer):
  * Learners keep the overall stars and MAY also score Clarity, Speaking time,
    Pace and Atmosphere (1-5 each). Tutor standing keeps reading the stars only.
  * A tutor sees monthly AVERAGES, never one learner's rating: each dimension
    over the last 30 days, and only once at least 3 different learners have
    rated, so no score can be traced back to who gave it. Feedback text is not
    shown to tutors.

§116 also narrowed the table's read policy to the rater and admins: it used to
let any tutor read every row, learner id and feedback included.

Attribution is `video_session.tutor_id`, as `payout_service.quality_inputs`
already does.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

DIMENSIONS = ("clarity", "speaking_time", "pace", "atmosphere")
WINDOW = timedelta(days=30)
MIN_LEARNERS = 3


def _t(name: str):
    return db.schema("app").table(name)


def averages(rows: list[dict]) -> dict:
    """Pure: mean stars and per-dimension means (ignoring blanks), or not enough."""
    learners = {r["student_id"] for r in rows}
    if len(learners) < MIN_LEARNERS:
        return {"enough": False, "learners": len(learners), "min_learners": MIN_LEARNERS}
    out = {"enough": True, "learners": len(learners), "ratings": len(rows), "min_learners": MIN_LEARNERS}
    stars = [int(r["stars"]) for r in rows if r.get("stars") is not None]
    out["stars"] = round(sum(stars) / len(stars), 2) if stars else None
    for d in DIMENSIONS:
        vals = [int(r[d]) for r in rows if r.get(d) is not None]
        # A dimension rated by fewer than MIN_LEARNERS people is hidden too.
        who = {r["student_id"] for r in rows if r.get(d) is not None}
        out[d] = round(sum(vals) / len(vals), 2) if len(who) >= MIN_LEARNERS else None
    return out


def for_tutor(tutor_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    # app.video_session (tauka-flutter): sessions this tutor ran.
    ids = [s["id"] for s in (_t("video_session").select("id").eq("tutor_id", tutor_id).execute()).data or []]
    if not ids:
        return averages([])
    rows = (_t("video_session_rating").select("student_id, stars, clarity, speaking_time, pace, atmosphere, created_at")
            .in_("session_id", ids).gte("created_at", (now - WINDOW).isoformat()).execute()).data or []
    return averages(rows)
