"""Handoff notes (enrolment plan 4.3, E-09, migration §119).

Playbook: "Handoff notes at each rotation: per learner, recurring errors, goals
and confidence." E-09: rotation is the guest rota, so the HOME tutor writes
for whoever leads a guest session, and the warm handoff carries the note to an
incoming home tutor (a `handoff_pending` or new link can read it).

Decided 2026-10-18 (developer):
  * One LIVING note per learner: recurring errors and goals (<= 280 each) and
    confidence hesitant | building | confident.
  * The home tutor keeps it current. The dashboard prompts them when a
    guest-led session of the pod is within 7 days and a member's note is
    missing or older than 30 days. Never a hard block.
  * Readers: the learner's own tutors (home and guest links, active or
    handoff_pending), a tutor leading one of their pod's sessions in the next
    14 days, ops, and the learner. The same rule is `app.can_read_handoff_note`
    for direct reads; this module applies it for the service role.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CONFIDENCE = ("hesitant", "building", "confident")
MAX_TEXT = 280
PROMPT_AHEAD = timedelta(days=7)
STALE = timedelta(days=30)
LEAD_AHEAD = timedelta(days=14)


class HandoffNoteRefused(Exception):
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


def _links(student_id: str) -> list[dict]:
    # app.student_tutor_link (tauka-flutter DDL, tauka-python writes): role home|guest.
    return (_t("student_tutor_link").select("tutor_id, role, state").eq("student_id", student_id)
            .in_("state", ["active", "handoff_pending"]).execute()).data or []


def _pods(student_id: str) -> list[str]:
    return [m["cohort_id"] for m in (_t("cohort_memberships").select("cohort_id").eq("user_id", student_id)
                                     .eq("status", "active").execute()).data or []]


def _leads_soon(tutor_id: str, student_id: str, now: datetime) -> bool:
    pods = _pods(student_id)
    if not pods:
        return False
    rows = (_t("cohort_session").select("scheduled_at, status").in_("cohort_id", pods)
            .eq("lead_tutor_id", tutor_id).eq("status", "scheduled").execute()).data or []
    return any(now - timedelta(days=1) <= _ts(r["scheduled_at"]) <= now + LEAD_AHEAD for r in rows)


def can_read(actor: str, student_id: str, now: datetime | None = None) -> bool:
    now = now or _now()
    return (actor == student_id
            or any(l["tutor_id"] == actor for l in _links(student_id))
            or _leads_soon(actor, student_id, now)
            or hs.admin_can(actor, "ops"))


def can_write(actor: str, student_id: str) -> bool:
    """The learner's home tutor (also while a handoff is pending), or ops."""
    return (any(l["tutor_id"] == actor and l["role"] == "home" for l in _links(student_id))
            or hs.admin_can(actor, "ops"))


def _note(student_id: str) -> dict | None:
    rows = (_t("handoff_note").select("student_id, recurring_errors, goals, confidence, written_by, updated_at")
            .eq("student_id", student_id).limit(1).execute()).data or []
    return rows[0] if rows else None


def get(actor: str, student_id: str) -> dict | None:
    if not can_read(actor, student_id):
        raise HandoffNoteRefused("not_allowed")
    return _note(student_id)


def save(actor: str, student_id: str, recurring_errors: str | None, goals: str | None, confidence: str) -> dict:
    if not can_write(actor, student_id):
        raise HandoffNoteRefused("not_home_tutor")
    if confidence not in CONFIDENCE:
        raise HandoffNoteRefused("bad_choice")
    errors = (recurring_errors or "").strip() or None
    goal = (goals or "").strip() or None
    if (errors and len(errors) > MAX_TEXT) or (goal and len(goal) > MAX_TEXT):
        raise HandoffNoteRefused("too_long")
    fields = {"recurring_errors": errors, "goals": goal, "confidence": confidence,
              "written_by": actor, "updated_at": _now().isoformat()}
    if _note(student_id):
        _t("handoff_note").update(fields).eq("student_id", student_id).execute()
    else:
        _t("handoff_note").insert({"student_id": student_id, **fields}).execute()
    return _note(student_id)


# ── the dashboard ────────────────────────────────────────────────────────────

def _members(pod_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not pod_ids:
        return out
    for m in (_t("cohort_memberships").select("cohort_id, user_id").in_("cohort_id", pod_ids)
              .eq("status", "active").execute()).data or []:
        out.setdefault(m["cohort_id"], []).append(m["user_id"])
    return out


def _notes(student_ids: list[str]) -> dict[str, dict]:
    if not student_ids:
        return {}
    return {n["student_id"]: n for n in (_t("handoff_note").select(
        "student_id, recurring_errors, goals, confidence, updated_at").in_("student_id", student_ids)
        .execute()).data or []}


def to_write(tutor_id: str, now: datetime | None = None) -> list[dict]:
    """For a home tutor: learners in their pods with a guest-led session in
    the next 7 days whose note is missing or older than 30 days."""
    from app.services.makeup_service import _names
    now = now or _now()
    pods = (_t("cohorts").select("id, name").eq("tutor_id", tutor_id).eq("status", "active").execute()).data or []
    if not pods:
        return []
    guest_soon = {}
    for s in (_t("cohort_session").select("cohort_id, scheduled_at, status, lead_role")
              .in_("cohort_id", [p["id"] for p in pods]).eq("status", "scheduled").execute()).data or []:
        at = _ts(s["scheduled_at"])
        if s.get("lead_role") == "guest" and now <= at <= now + PROMPT_AHEAD:
            guest_soon[s["cohort_id"]] = min(guest_soon.get(s["cohort_id"], at), at)
    members = _members(list(guest_soon))
    learners = sorted({u for us in members.values() for u in us})
    notes = _notes(learners)
    names = _names(learners)
    pod_name = {p["id"]: p.get("name") for p in pods}
    out = []
    for pod_id, at in sorted(guest_soon.items(), key=lambda kv: kv[1]):
        for sid in members.get(pod_id, []):
            n = notes.get(sid)
            if n is None or now - _ts(n["updated_at"]) > STALE:
                out.append({"student_id": sid, "name": names.get(sid), "pod_name": pod_name.get(pod_id),
                            "guest_session_at": at.isoformat(), "note": n,
                            "reason": "missing" if n is None else "stale"})
    return out


def guest_sessions(tutor_id: str, now: datetime | None = None) -> list[dict]:
    """For a tutor leading a guest session in the next 14 days: the session,
    its pod, and each member's note — read in the minute before it starts."""
    from app.services.makeup_service import _names
    now = now or _now()
    rows = [s for s in (_t("cohort_session").select("id, cohort_id, scheduled_at, status, lead_role")
                        .eq("lead_tutor_id", tutor_id).eq("status", "scheduled").execute()).data or []
            if s.get("lead_role") == "guest" and now - timedelta(hours=2) <= _ts(s["scheduled_at"]) <= now + LEAD_AHEAD]
    if not rows:
        return []
    pods = {p["id"]: p for p in (_t("cohorts").select("id, name, tutor_id")
            .in_("id", sorted({r["cohort_id"] for r in rows})).execute()).data or []}
    members = _members(list(pods))
    learners = sorted({u for us in members.values() for u in us})
    notes = _notes(learners)
    names = _names(learners + [p.get("tutor_id") for p in pods.values() if p.get("tutor_id")])
    return [{"session_id": r["id"], "scheduled_at": r["scheduled_at"],
             "pod_name": pods.get(r["cohort_id"], {}).get("name"),
             "home_tutor": names.get(pods.get(r["cohort_id"], {}).get("tutor_id")),
             "learners": [{"student_id": sid, "name": names.get(sid), "note": notes.get(sid)}
                          for sid in members.get(r["cohort_id"], [])]}
            for r in sorted(rows, key=lambda r: r["scheduled_at"])]
