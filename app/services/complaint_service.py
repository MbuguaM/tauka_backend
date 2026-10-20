"""Session complaints (enrolment plan §2f item 7, migration §106).

§2e: a learner's live sessions carry forward only when the failure is ours — a
tutor issue, or **a complaint that is upheld**. This is where a complaint starts.

  * A learner picks one live session they were in during the last 14 days —
    pod, make-up, clinic or 1-on-1 — a reason code and an optional short note.
    One complaint per session.
  * **Conduct** complaints go to the `safeguarding` capability; everything else
    to `ops` (§2k keeps those apart once there are two admins). The database
    enforces the routing.
  * The routed admin upholds or rejects, once. Upheld → one session credit,
    referenced to the complaint so it can never be granted twice; the credit
    is then delivered as a make-up like any other.

The note is free text about a tutor. It is shown to the learner and the routed
admins only — never to the tutor — and its retention is an open legal item
(L-014). It is not an activity payload, so the telemetry free-text rule doesn't
apply; it is kept short (1,000 characters).
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services import hierarchy_service as hs
from app.services.makeup_service import _ts
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=14)
REASONS = ("tutor_absent", "tutor_late", "cut_short", "connection", "conduct", "other")
TYPES = ("cohort_session", "makeup_session", "clinic", "one_to_one_session")


class ComplaintRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recent_sessions(student_id: str, now: datetime | None = None) -> list[dict]:
    """The live sessions this learner was in that have started within the
    window, newest first: what a complaint can be about."""
    now = now or _now()
    since = (now - WINDOW).isoformat()
    out: list[dict] = []

    # Pod sessions of every pod they are (or were) a member of.
    pods = [m["cohort_id"] for m in (_t("cohort_memberships").select("cohort_id")
                                     .eq("user_id", student_id).execute()).data or []]
    if pods:
        for r in (_t("cohort_session").select("id, scheduled_at, duration_minutes, status")
                  .in_("cohort_id", pods).gte("scheduled_at", since).execute()).data or []:
            if r.get("status") != "cancelled":
                out.append({"session_type": "cohort_session", "session_id": r["id"],
                            "starts_at": r["scheduled_at"], "label": "Pod session"})

    seats = [s["makeup_session_id"] for s in (_t("makeup_session_participant")
             .select("makeup_session_id, status").eq("student_id", student_id).execute()).data or []
             if s["status"] != "withdrawn"]
    if seats:
        for r in (_t("makeup_session").select("id, scheduled_at, status").in_("id", seats).execute()).data or []:
            if r.get("scheduled_at") and r["status"] in ("scheduled", "in_progress", "completed") \
                    and str(r["scheduled_at"]) >= since:
                out.append({"session_type": "makeup_session", "session_id": r["id"],
                            "starts_at": r["scheduled_at"], "label": "Make-up session"})

    booked = [b["clinic_id"] for b in (_t("clinic_booking").select("clinic_id, status")
              .eq("student_id", student_id).execute()).data or [] if b["status"] != "cancelled"]
    if booked:
        for r in (_t("clinic").select("id, starts_at, status").in_("id", booked).execute()).data or []:
            if r["status"] != "cancelled" and str(r["starts_at"]) >= since:
                out.append({"session_type": "clinic", "session_id": r["id"],
                            "starts_at": r["starts_at"], "label": "Phase 0 clinic"})

    for r in (_t("one_to_one_session").select("id, scheduled_at, status")
              .eq("student_id", student_id).gte("scheduled_at", since).execute()).data or []:
        if r.get("status") != "cancelled":
            out.append({"session_type": "one_to_one_session", "session_id": r["id"],
                        "starts_at": r["scheduled_at"], "label": "1-on-1 session"})

    started = [s for s in out if _ts(s["starts_at"]) <= now]
    done = {(c["session_type"], c["session_id"]) for c in mine(student_id)}
    for s in started:
        s["complained"] = (s["session_type"], s["session_id"]) in done
    return sorted(started, key=lambda s: s["starts_at"], reverse=True)


def mine(student_id: str) -> list[dict]:
    return (_t("session_complaint").select(
        "id, session_type, session_id, reason_code, status, created_at, reviewed_at"
    ).eq("student_id", student_id).order("created_at", desc=True).execute()).data or []


def create(student_id: str, session_type: str, session_id: str, reason_code: str,
           detail: str | None) -> dict:
    if session_type not in TYPES:
        raise ComplaintRefused("invalid_session_type")
    if reason_code not in REASONS:
        raise ComplaintRefused("invalid_reason")
    detail = (detail or "").strip() or None
    if detail and len(detail) > 1000:
        raise ComplaintRefused("detail_too_long")
    if not any(s["session_type"] == session_type and s["session_id"] == session_id
               for s in recent_sessions(student_id)):
        raise ComplaintRefused("not_your_session")
    try:
        made = _t("session_complaint").insert({
            "student_id": student_id, "session_type": session_type, "session_id": session_id,
            "reason_code": reason_code, "detail": detail,
            "routed_to": "safeguarding" if reason_code == "conduct" else "ops",
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise ComplaintRefused("already_complained") from exc
        raise
    return (made.data or [{}])[0]


def _capabilities(actor: str) -> list[str]:
    return [c for c in ("ops", "safeguarding") if hs.admin_can(actor, c)]


def queue(actor: str) -> list[dict]:
    """Open complaints the admin may decide: ops sees ops-routed, safeguarding
    sees conduct. Names of the learner and the session's tutor are included;
    the tutor never sees any of it."""
    caps = _capabilities(actor)
    if not caps:
        raise ComplaintRefused("not_allowed")
    rows = (_t("session_complaint").select("*").eq("status", "open").in_("routed_to", caps)
            .order("created_at").execute()).data or []
    from app.services.makeup_service import _names
    tutors = {r["id"]: _tutor_of(r["session_type"], r["session_id"]) for r in rows}
    names = _names([r["student_id"] for r in rows] + [t for t in tutors.values() if t])
    return [{**r, "student_name": names.get(r["student_id"]),
             "tutor_id": tutors[r["id"]], "tutor_name": names.get(tutors[r["id"]])} for r in rows]


def _tutor_of(session_type: str, session_id: str) -> str | None:
    table, col = {"cohort_session": ("cohort_session", "lead_tutor_id"),
                  "makeup_session": ("makeup_session", "tutor_id"),
                  "clinic": ("clinic", "tutor_id"),
                  "one_to_one_session": ("one_to_one_session", "tutor_id")}[session_type]
    rows = (_t(table).select(col).eq("id", session_id).limit(1).execute()).data or []
    return (rows[0] if rows else {}).get(col)


def decide(actor: str, complaint_id: str, upheld: bool) -> dict:
    rows = (_t("session_complaint").select("*").eq("id", complaint_id).limit(1).execute()).data or []
    if not rows:
        raise ComplaintRefused("no_such_complaint")
    c = rows[0]
    if c["routed_to"] not in _capabilities(actor):
        raise ComplaintRefused("not_allowed")
    if c["status"] != "open":
        raise ComplaintRefused("decided")
    status = "upheld" if upheld else "rejected"
    _t("session_complaint").update({
        "status": status, "reviewed_by": actor, "reviewed_at": _now().isoformat(),
    }).eq("id", complaint_id).eq("status", "open").execute()
    if upheld:
        try:
            _t("session_credit").insert({
                "student_id": c["student_id"], "direction": "grant", "reason": "complaint",
                "reference_type": "session_complaint", "reference_id": complaint_id,
            }).execute()
        except Exception as exc:  # noqa: BLE001
            # The unique index: the credit for this complaint already exists.
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
    return {**c, "status": status}
