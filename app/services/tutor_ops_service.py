"""The tutor side — enrolment plan 8.3, 8.4, 8.7, 8.8 (§134).

8.3  Commitment blocks (weekly hours for a ~5-week unit, at least 6, and no
     more than their weekly availability holds) and holidays (declared 8 weeks
     ahead; shorter notice waits for ops). `on_holiday` is what matching, cover
     and standby read.
8.4  Work items ops records (gatherings, other); QA observations add their own.
8.7  QA sampling: monthly, one upcoming pod session per active tutor, observed
     live by a rung >= 3 reviewer (or ops) who isn't the tutor; a fixed rubric.
     Informs rung promotion (E-10); never pay or matching (E-16).
8.8  Public profiles: the tutor writes and publishes; anyone reads a published one.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MIN_HOURS = 6
UNIT_WEEKS = 5
HOLIDAY_NOTICE = timedelta(weeks=8)
RUBRIC = ("structure", "speaking_time", "correction", "warmth", "target_language")
QA_REVIEWER_RUNG = 3


class TutorOpsRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def _require_tutor(tutor_id: str) -> dict:
    rows = (_t("tutor").select("id, status, deleted_at").eq("id", tutor_id).limit(1).execute()).data or []
    if not rows or rows[0].get("deleted_at") or rows[0].get("status") == "offboarded":
        raise TutorOpsRefused("not_a_tutor")
    return rows[0]


def _require_ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise TutorOpsRefused("not_allowed")


# ── 8.3 commitment blocks ───────────────────────────────────────────────────

def weekly_available_hours(tutor_id: str) -> float:
    total = 0
    for w in (_t("tutor_availability").select("start_time, end_time").eq("tutor_id", tutor_id).eq("active", True)
              .execute()).data or []:
        a, b = str(w["start_time"])[:5], str(w["end_time"])[:5]
        mins = (int(b[:2]) * 60 + int(b[3:]) or 1440) - (int(a[:2]) * 60 + int(a[3:]))
        total += max(0, mins)
    return total / 60


def commit(tutor_id: str, starts_on: date, hours: int) -> dict:
    _require_tutor(tutor_id)
    if hours < MIN_HOURS:
        raise TutorOpsRefused("too_few_hours")
    if hours > weekly_available_hours(tutor_id):
        raise TutorOpsRefused("more_than_available")
    ends = starts_on + timedelta(weeks=UNIT_WEEKS)
    for b in (_t("tutor_commitment_block").select("starts_on, ends_on, status").eq("tutor_id", tutor_id)
              .eq("status", "committed").execute()).data or []:
        if _d(b["starts_on"]) < ends and starts_on < _d(b["ends_on"]):
            raise TutorOpsRefused("overlaps")
    return (_t("tutor_commitment_block").insert({"tutor_id": tutor_id, "starts_on": starts_on.isoformat(),
                                                  "ends_on": ends.isoformat(), "hours_per_week": hours,
                                                  "status": "committed"}).execute()).data[0]


# ── 8.3 holidays ────────────────────────────────────────────────────────────

def declare_holiday(tutor_id: str, starts_on: date, ends_on: date, today: date | None = None) -> dict:
    _require_tutor(tutor_id)
    today = today or _today()
    if ends_on < starts_on or starts_on < today:
        raise TutorOpsRefused("bad_dates")
    status = "approved" if starts_on - today >= HOLIDAY_NOTICE else "needs_approval"
    row = (_t("tutor_holiday").insert({"tutor_id": tutor_id, "starts_on": starts_on.isoformat(),
                                       "ends_on": ends_on.isoformat(), "status": status}).execute()).data[0]
    return row   # needs_approval shows in ops' list (pending_holidays)


def decide_holiday(actor: str, holiday_id: str, approve: bool) -> dict:
    _require_ops(actor)
    got = (_t("tutor_holiday").update({"status": "approved" if approve else "declined", "decided_by": actor})
           .eq("id", holiday_id).eq("status", "needs_approval").execute()).data or []
    if not got:
        raise TutorOpsRefused("not_pending")
    return got[0]


def on_holiday(tutor_ids: list[str], on: date) -> set[str]:
    """Tutors on an approved holiday that day: matching, cover and standby skip them."""
    if not tutor_ids:
        return set()
    try:
        rows = (_t("tutor_holiday").select("tutor_id, starts_on, ends_on, status").in_("tutor_id", list(tutor_ids))
                .eq("status", "approved").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("holiday read failed: %s", exc)
        return set()
    if not isinstance(rows, list):
        return set()
    return {r["tutor_id"] for r in rows if isinstance(r, dict) and _d(r["starts_on"]) <= on <= _d(r["ends_on"])}


def mine(tutor_id: str) -> dict:
    today = _today().isoformat()
    return {
        "blocks": [b for b in (_t("tutor_commitment_block").select("id, starts_on, ends_on, hours_per_week, status")
                   .eq("tutor_id", tutor_id).execute()).data or [] if str(b["ends_on"]) >= today],
        "holidays": [h for h in (_t("tutor_holiday").select("id, starts_on, ends_on, status")
                     .eq("tutor_id", tutor_id).execute()).data or [] if str(h["ends_on"]) >= today],
        "available_hours": round(weekly_available_hours(tutor_id), 1),
        "min_hours": MIN_HOURS,
    }


def pending_holidays(actor: str) -> list[dict]:
    _require_ops(actor)
    from app.services.makeup_service import _names
    rows = (_t("tutor_holiday").select("*").eq("status", "needs_approval").execute()).data or []
    names = _names([r["tutor_id"] for r in rows]) if rows else {}
    return [{**r, "tutor_name": names.get(r["tutor_id"])} for r in rows]


# ── 8.4 work items ──────────────────────────────────────────────────────────

def add_work(actor: str, tutor_id: str, kind: str, occurred_on: date, quantity: int = 1,
             ref_code: str | None = None) -> dict:
    _require_ops(actor)
    if kind not in ("gathering", "other"):
        raise TutorOpsRefused("bad_kind")
    if ref_code and not re.fullmatch(r"[A-Za-z0-9_./:-]{1,60}", ref_code):
        raise TutorOpsRefused("bad_ref")
    _require_tutor(tutor_id)
    return (_t("tutor_work_item").insert({"tutor_id": tutor_id, "kind": kind, "quantity": quantity,
                                          "occurred_on": occurred_on.isoformat(), "ref_code": ref_code,
                                          "added_by": actor}).execute()).data[0]


# ── 8.7 QA sampling ─────────────────────────────────────────────────────────

def qa_reviewers() -> list[str]:
    rows = (_t("tutor").select("id, status, deleted_at").eq("status", "active").execute()).data or []
    return [r["id"] for r in rows if not r.get("deleted_at") and (hs.rung_of(r["id"]) or 0) >= QA_REVIEWER_RUNG]


def sample_month(now: datetime | None = None) -> dict:
    """One upcoming pod session per active tutor per month, a reviewer each
    (fewest assignments first, never themselves). Idempotent per month."""
    now = now or datetime.now(timezone.utc)
    month = now.date().replace(day=1)
    out = {"assigned": 0, "no_reviewer": 0, "no_session": 0}
    reviewers = qa_reviewers()
    load = {r: 0 for r in reviewers}
    done = {r["tutor_id"] for r in (_t("qa_review").select("tutor_id, reviewer_id").eq("month", month.isoformat())
            .execute()).data or []}
    for r in (_t("qa_review").select("reviewer_id").eq("month", month.isoformat()).execute()).data or []:
        load[r["reviewer_id"]] = load.get(r["reviewer_id"], 0) + 1
    tutors = [t["id"] for t in (_t("tutor").select("id, status, deleted_at").eq("status", "active").execute()).data or []
              if not t.get("deleted_at")]
    horizon = (now + timedelta(days=14)).isoformat()
    for tid in sorted(tutors):
        if tid in done:
            continue
        sessions = sorted((s for s in (_t("cohort_session").select("id, scheduled_at, status, lead_tutor_id")
                           .eq("lead_tutor_id", tid).eq("status", "scheduled").gte("scheduled_at", now.isoformat())
                           .execute()).data or [] if str(s["scheduled_at"]) <= horizon),
                          key=lambda s: str(s["scheduled_at"]))
        if not sessions:
            out["no_session"] += 1
            continue
        pool = sorted((r for r in load if r != tid), key=lambda r: (load[r], r))
        if not pool:
            out["no_reviewer"] += 1
            continue
        reviewer = pool[0]
        try:
            _t("qa_review").insert({"tutor_id": tid, "session_type": "cohort_session", "session_id": sessions[0]["id"],
                                    "session_at": sessions[0]["scheduled_at"], "reviewer_id": reviewer,
                                    "month": month.isoformat(), "status": "assigned"}).execute()
            load[reviewer] += 1
            out["assigned"] += 1
        except Exception as exc:  # noqa: BLE001
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                logger.error("QA sample for %s failed: %s", tid, exc)
    return out


def my_qa(user_id: str) -> dict:
    """As reviewer: assignments; as tutor: their reviews (scores once done)."""
    return {
        "to_observe": (_t("qa_review").select("id, tutor_id, session_id, session_at, status")
                       .eq("reviewer_id", user_id).eq("status", "assigned").execute()).data or [],
        "about_me": [r for r in (_t("qa_review").select("id, month, status, scores, done_at")
                     .eq("tutor_id", user_id).execute()).data or [] if r.get("status") == "done"],
    }


def submit_qa(reviewer_id: str, review_id: str, scores: dict) -> dict:
    if set(scores) != set(RUBRIC) or any(not isinstance(v, int) or not 1 <= v <= 4 for v in scores.values()):
        raise TutorOpsRefused("bad_scores")
    rows = (_t("qa_review").select("*").eq("id", review_id).limit(1).execute()).data or []
    if not rows or (rows[0]["reviewer_id"] != reviewer_id and not hs.admin_can(reviewer_id, "ops")):
        raise TutorOpsRefused("not_yours")
    got = (_t("qa_review").update({"status": "done", "scores": scores, "done_at": datetime.now(timezone.utc).isoformat()})
           .eq("id", review_id).eq("status", "assigned").execute()).data or []
    if not got:
        raise TutorOpsRefused("not_open")
    # 8.4: the observation is paid work for the reviewer (a tutor, not ops).
    if rows[0]["reviewer_id"] == reviewer_id and not hs.admin_can(reviewer_id, "ops"):
        try:
            _t("tutor_work_item").insert({"tutor_id": reviewer_id, "kind": "qa_observation", "quantity": 1,
                                          "occurred_on": str(rows[0]["session_at"])[:10],
                                          "ref_code": f"qa:{review_id}"}).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("QA work item for %s failed: %s", reviewer_id, exc)
    return got[0]


# ── 8.8 public profiles ─────────────────────────────────────────────────────

_SLUG = re.compile(r"^[a-z0-9-]{3,60}$")


def save_profile(tutor_id: str, slug: str, headline: str | None, bio: str | None, published: bool) -> dict:
    _require_tutor(tutor_id)
    slug = (slug or "").strip().lower()
    if not _SLUG.match(slug):
        raise TutorOpsRefused("bad_slug")
    taken = (_t("tutor_public_profile").select("tutor_id").eq("slug", slug).limit(1).execute()).data or []
    if taken and taken[0]["tutor_id"] != tutor_id:
        raise TutorOpsRefused("slug_taken")
    if published and not is_ready("tutor_public_profiles"):
        raise TutorOpsRefused("not_enabled")
    _t("tutor_public_profile").upsert({"tutor_id": tutor_id, "slug": slug, "headline": (headline or "")[:120] or None,
                                       "bio": (bio or "")[:1200] or None, "published": bool(published),
                                       "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    return public_profile(slug, include_unpublished_for=tutor_id) or {}


def public_profile(slug: str, include_unpublished_for: str | None = None) -> dict | None:
    """What anyone may see: first name, headline, bio, language, specialisations,
    rated average from 3+ learners. No surname, no contact details."""
    from app.services.makeup_service import _names
    rows = (_t("tutor_public_profile").select("*").eq("slug", (slug or "").lower()).limit(1).execute()).data or []
    if not rows:
        return None
    p = rows[0]
    if not p.get("published") and p["tutor_id"] != include_unpublished_for:
        return None
    if p.get("published") and include_unpublished_for is None and not is_ready("tutor_public_profiles"):
        return None
    tutor = (_t("tutor").select("language, status").eq("id", p["tutor_id"]).limit(1).execute()).data or [{}]
    if tutor[0].get("status") != "active" and include_unpublished_for is None:
        return None
    teaching = (_t("tutor_teaching_profile").select("specialisations, years_experience").eq("tutor_id", p["tutor_id"])
                .limit(1).execute()).data or [{}]
    first = (_names([p["tutor_id"]]).get(p["tutor_id"]) or "").split(" ")[0] or "Tutor"
    return {"slug": p["slug"], "first_name": first, "headline": p.get("headline"), "bio": p.get("bio"),
            "language": tutor[0].get("language"), "specialisations": teaching[0].get("specialisations") or [],
            "years_experience": teaching[0].get("years_experience"), "published": bool(p.get("published"))}


def public_index() -> list[dict]:
    if not is_ready("tutor_public_profiles"):
        return []
    out = []
    for p in (_t("tutor_public_profile").select("slug, published").eq("published", True).execute()).data or []:
        prof = public_profile(p["slug"])
        if prof:
            out.append({k: prof[k] for k in ("slug", "first_name", "headline", "language")})
    return sorted(out, key=lambda x: x["first_name"])
