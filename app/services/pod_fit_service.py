"""First-session norms and the two-week fit survey (enrolment plan 4.1, §118).

Playbook step 12: the tutor "agrees pod norms (camera, lateness, language
use)" in the first session. Step 13: "a short survey after session two checks
fit; serious mismatches are moved before the first checkpoint."

Decided 2026-10-18 (developer):
  * NORMS are fixed choices the pod's tutor records; every member sees them.
  * The SURVEY is four taps — level, time, group, staying — no free text. It
    is due once a learner's pod has HELD two sessions since they joined, and
    stays open for their first 30 days in the pod.
  * A MISMATCH (level not right, time doesn't work, not comfortable, or "I'd
    like to move") is FLAGGED for ops. Ops may move the learner to the wait
    pool — first priority at the next formation, and offered open seats in
    other pods in the refill window (§112). Nothing moves by itself.
  * Only OPS sees a learner's answers, never the tutor.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

NORMS = {
    "camera": ("on", "when_possible", "optional"),
    "lateness": ("on_time", "door_5", "door_10"),
    "language": ("amharic_only", "amharic_first", "either"),
}
ANSWERS = {
    "level_fit": ("too_easy", "right", "too_hard"),
    "time_fit": ("works", "sometimes", "doesnt_work"),
    "group_fit": ("comfortable", "mostly", "not_comfortable"),
    "staying": ("stay", "not_sure", "move"),
}
SESSIONS_BEFORE_SURVEY = 2
SURVEY_WINDOW = timedelta(days=30)


class PodFitRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def is_mismatch(a: dict) -> bool:
    """The rule the generated column applies (§118), for the API's own use."""
    return (a.get("level_fit") != "right" or a.get("time_fit") == "doesnt_work"
            or a.get("group_fit") == "not_comfortable" or a.get("staying") == "move")


# ── norms ────────────────────────────────────────────────────────────────────

def _teaches(tutor_id: str, cohort_id: str) -> bool:
    pod = (_t("cohorts").select("tutor_id").eq("id", cohort_id).limit(1).execute()).data or []
    if pod and pod[0].get("tutor_id") == tutor_id:
        return True
    return bool((_t("cohort_session").select("id").eq("cohort_id", cohort_id)
                 .eq("lead_tutor_id", tutor_id).limit(1).execute()).data)


def norms(cohort_id: str) -> dict | None:
    rows = (_t("pod_norms").select("cohort_id, camera, lateness, language, agreed_at")
            .eq("cohort_id", cohort_id).limit(1).execute()).data or []
    return rows[0] if rows else None


def set_norms(actor: str, cohort_id: str, camera: str, lateness: str, language: str) -> dict:
    if not (_teaches(actor, cohort_id) or hs.admin_can(actor, "ops")):
        raise PodFitRefused("not_your_pod")
    picked = {"camera": camera, "lateness": lateness, "language": language}
    if any(v not in NORMS[k] for k, v in picked.items()):
        raise PodFitRefused("bad_choice")
    now = _now().isoformat()
    if norms(cohort_id):
        _t("pod_norms").update({**picked, "updated_at": now}).eq("cohort_id", cohort_id).execute()
    else:
        _t("pod_norms").insert({"cohort_id": cohort_id, **picked, "agreed_by": actor}).execute()
    return norms(cohort_id)


def pending_for_tutor(tutor_id: str) -> list[dict]:
    """The tutor's live pods without norms yet, with the first session's time:
    'agree your pod's norms at the first session'."""
    pods = (_t("cohorts").select("id, name, status").eq("tutor_id", tutor_id)
            .in_("status", ["forming", "active"]).execute()).data or []
    if not pods:
        return []
    have = {r["cohort_id"] for r in (_t("pod_norms").select("cohort_id")
            .in_("cohort_id", [p["id"] for p in pods]).execute()).data or []}
    out = []
    for p in pods:
        if p["id"] in have:
            continue
        first = sorted((s["scheduled_at"] for s in (_t("cohort_session").select("scheduled_at, status")
                        .eq("cohort_id", p["id"]).execute()).data or [] if s.get("status") != "cancelled"))
        out.append({"cohort_id": p["id"], "name": p.get("name"), "first_session_at": first[0] if first else None})
    return out


def for_member(student_id: str) -> list[dict]:
    """The norms of the learner's active pod(s), for the pod page."""
    pods = [m["cohort_id"] for m in (_t("cohort_memberships").select("cohort_id").eq("user_id", student_id)
                                     .eq("status", "active").execute()).data or []]
    return [n for n in (norms(p) for p in pods) if n]


# ── the survey ───────────────────────────────────────────────────────────────

def due(student_id: str, now: datetime | None = None) -> list[dict]:
    """Pods where the learner's survey is due: two sessions held since they
    joined, not yet answered, within their first 30 days."""
    now = now or _now()
    seats = (_t("cohort_memberships").select("cohort_id, joined_at").eq("user_id", student_id)
             .eq("status", "active").execute()).data or []
    out = []
    for m in seats:
        joined = _ts(m.get("joined_at")) or now
        if now - joined > SURVEY_WINDOW:
            continue
        answered = (_t("pod_fit_survey").select("cohort_id").eq("cohort_id", m["cohort_id"])
                    .eq("student_id", student_id).limit(1).execute()).data
        if answered:
            continue
        held = [s for s in (_t("cohort_session").select("scheduled_at, status").eq("cohort_id", m["cohort_id"])
                            .execute()).data or []
                if s.get("status") == "completed" and (_ts(s["scheduled_at"]) or now) >= joined]
        if len(held) >= SESSIONS_BEFORE_SURVEY:
            pod = (_t("cohorts").select("name").eq("id", m["cohort_id"]).limit(1).execute()).data or [{}]
            out.append({"cohort_id": m["cohort_id"], "name": pod[0].get("name")})
    return out


def submit(student_id: str, cohort_id: str, answers: dict) -> dict:
    if not any(d["cohort_id"] == cohort_id for d in due(student_id)):
        # Not a member, too early, too late, or already answered.
        raise PodFitRefused("not_due")
    picked = {k: answers.get(k) for k in ANSWERS}
    if any(v not in ANSWERS[k] for k, v in picked.items()):
        raise PodFitRefused("bad_choice")
    try:
        _t("pod_fit_survey").insert({"cohort_id": cohort_id, "student_id": student_id, **picked}).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise PodFitRefused("not_due") from exc
        raise
    return {"cohort_id": cohort_id, **picked, "mismatch": is_mismatch(picked)}


# ── ops ──────────────────────────────────────────────────────────────────────

def _require_ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise PodFitRefused("not_allowed")


def queue(actor: str) -> list[dict]:
    """Mismatches not yet reviewed, oldest first, with names and the pod."""
    _require_ops(actor)
    from app.services.makeup_service import _names
    rows = [r for r in (_t("pod_fit_survey").select("*").order("created_at").execute()).data or []
            if r.get("mismatch") and r.get("action") is None]
    if not rows:
        return []
    pods = {p["id"]: p for p in (_t("cohorts").select("id, name, level, status")
            .in_("id", sorted({r["cohort_id"] for r in rows})).execute()).data or []}
    names = _names([r["student_id"] for r in rows])
    return [{**r, "student_name": names.get(r["student_id"]), "pod": pods.get(r["cohort_id"])} for r in rows]


def review(actor: str, cohort_id: str, student_id: str, action: str) -> dict:
    """`none` leaves the learner where they are; `moved` takes them out of the
    pod and back to the wait pool (the checkpoint's move, §109)."""
    _require_ops(actor)
    if action not in ("none", "moved"):
        raise PodFitRefused("bad_choice")
    rows = (_t("pod_fit_survey").select("*").eq("cohort_id", cohort_id).eq("student_id", student_id)
            .limit(1).execute()).data or []
    if not rows:
        raise PodFitRefused("no_such_survey")
    if rows[0].get("action") is not None:
        raise PodFitRefused("reviewed")
    if action == "moved":
        from app.services.checkpoint_service import _leave
        _leave(cohort_id, [student_id], "transferred",
               "moved to the wait pool after the two-week fit survey (plan 4.1)")
    got = (_t("pod_fit_survey").update({"action": action, "reviewed_by": actor, "reviewed_at": _now().isoformat()})
           .eq("cohort_id", cohort_id).eq("student_id", student_id).is_("action", "null").execute()).data or []
    return got[0] if got else {**rows[0], "action": action}
