"""Admin capabilities and tutor rungs (enrolment plan §2k, migration §99).

Three things, kept apart on purpose:

- **Capabilities** (`app.admin_users.capabilities`): what an admin may do —
  owner / ops / safeguarding / content / finance, combinable. `owner` implies the
  rest, except safeguarding once there are two or more admins; then safeguarding
  is held by someone who holds neither ops nor owner (database-enforced).
- **Rungs** (`app.tutor_rung`): what work a tutor may do — 1 Phase 0 reviewer,
  2 session tutor, 3 pod tutor, 4 senior / anchor. `ops` sets 1–3; only an
  `owner` promotes to or demotes from 4. `tutor_standing` informs a promotion and
  never makes one.
- **Scope**: a tutor judges only their own learners — an active
  `student_tutor_link`, never the course-wide branch of `teaches_student()`.

Every rule lives in SQL (`app.admin_can`, `app.set_tutor_rung`). This module
passes the caller it verified from the JWT and turns refusals into stable words.
"""

import logging

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CAPABILITIES = ("owner", "ops", "safeguarding", "content", "finance")
RUNGS = {1: "Phase 0 reviewer", 2: "Session tutor", 3: "Pod tutor", 4: "Senior / anchor"}

# Substring of the SQL error → the word the API returns.
_REFUSALS = (
    ("only an owner", "owner_required"),
    ("only an ops admin", "ops_required"),
    ("their own rung", "not_self"),
    ("no such tutor", "no_such_tutor"),
    ("rung must be", "invalid_rung"),
    ("reason", "reason_required"),
)


class HierarchyRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _app():
    return db.schema("app")


def admin_can(actor: str, capability: str) -> bool:
    """Does this admin hold the capability? Fails closed."""
    if capability not in CAPABILITIES:
        return False
    try:
        return bool(_app().rpc("admin_can", {"p_actor": actor, "p_cap": capability}).execute().data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_can(%s, %s) failed: %s", actor, capability, exc)
        return False


def rung_of(tutor_id: str) -> int | None:
    """The tutor's current rung, or None if they have never been given one."""
    try:
        data = _app().rpc("tutor_rung_of", {"p_tutor": tutor_id}).execute().data
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor_rung_of(%s) failed: %s", tutor_id, exc)
        return None
    return int(data) if isinstance(data, int) else None


def rung_history(tutor_id: str) -> list[dict]:
    return (
        _app().table("tutor_rung")
        .select("rung, effective_from, effective_to, set_by, reason")
        .eq("tutor_id", tutor_id).order("effective_from", desc=True).execute()
    ).data or []


def set_rung(actor: str, tutor_id: str, rung: int, reason: str) -> dict:
    """Promote or demote. Idempotent when the rung is unchanged."""
    if not (reason or "").strip():
        raise HierarchyRefused("reason_required")
    try:
        result = _app().rpc("set_tutor_rung", {
            "p_actor": actor, "p_tutor": tutor_id, "p_rung": rung, "p_reason": reason.strip(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        for needle, word in _REFUSALS:
            if needle in text:
                raise HierarchyRefused(word) from exc
        logger.error("set_tutor_rung(%s -> %s=%s) failed: %s", actor, tutor_id, rung, exc)
        raise
    row = result.data[0] if isinstance(result.data, list) else result.data
    return row or {}


def linked_tutor_ids(student_id: str) -> set[str]:
    """The learner's own tutors: home and guest, active links only."""
    rows = (
        _app().table("student_tutor_link").select("tutor_id")
        .eq("student_id", student_id).eq("state", "active").execute()
    ).data or []
    return {r["tutor_id"] for r in rows}


def tutors() -> list[dict]:
    """Every tutor with their status and current rung, for the admin screen.
    Names only from user_profiles."""
    rows = (_app().table("tutor").select("id, status, deleted_at, language, country_code").execute()).data or []
    ids = [r["id"] for r in rows]
    rungs = {}
    if ids:
        for r in (_app().table("tutor_rung").select("tutor_id, rung")
                  .in_("tutor_id", ids).is_("effective_to", "null").execute()).data or []:
            rungs[r["tutor_id"]] = r["rung"]
    names = {}
    if ids:
        for p in (_app().table("user_profiles").select("id, first_name, last_name")
                  .in_("id", ids).execute()).data or []:
            names[p["id"]] = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or "Unnamed"
    # Plan 8.6 (§125): the 90-day reliability score, for ops to read. Never a gate.
    from app.services.standby_service import reliability
    rel = reliability(ids) if ids else {}
    return sorted(
        ({"tutor_id": r["id"], "name": names.get(r["id"], "Unnamed"), "status": r.get("status"),
          "language": r.get("language"), "country_code": r.get("country_code"),
          "rung": rungs.get(r["id"]), "deleted": bool(r.get("deleted_at")),
          "reliability": rel.get(r["id"])}
         for r in rows),
        key=lambda t: (t["deleted"], t["name"]),
    )
