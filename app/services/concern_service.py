"""Concern reports (enrolment plan 4.6, migration §117).

Playbook: safeguarding or conduct issues are "handled immediately by the
safeguarding lead, not deferred to a checkpoint" — "within 24 hours".

Decided 2026-10-18 (developer): a concern report is SEPARATE from a session
complaint (§106). A learner can report anything that worries them, in or out of
a session: another learner, a tutor, contact-detail swapping, their own
wellbeing. It goes ONLY to the `safeguarding` capability; an alert email goes
at once and again if it isn't acknowledged within 24 hours; it never grants a
credit.

WHO IT CAN BE ABOUT: a tutor or learner the reporter knows through Tauka (a pod
mate, a pod's tutor, one of their own tutors, someone from a make-up), or
"someone else" / "myself" with no person named. A session, if given, must be
one they were in during the last 14 days (the complaint rule).

PRIVACY: the note is free text about a person. Only the reporter and
safeguarding read the report; the person it is about never does, nor does ops.
The alert email carries the category only, never the note or any name.
Retention is open (L-015).
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import email_service
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CATEGORIES = ("harassment", "contact_details", "inappropriate_content", "wellbeing", "safety", "other")
ABOUT = ("tutor", "learner", "someone_else", "myself")
OUTCOMES = ("no_action", "advice_given", "warning_issued", "removed", "escalated")
OVERDUE = timedelta(hours=24)
CATEGORY_LABEL = {
    "harassment": "Harassment or bullying", "contact_details": "Asked for contact details",
    "inappropriate_content": "Inappropriate content", "wellbeing": "Wellbeing",
    "safety": "Safety", "other": "Something else",
}


class ConcernRefused(Exception):
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


# ── who a learner knows ──────────────────────────────────────────────────────

def people(student_id: str) -> list[dict]:
    """Tutors and learners the reporter knows through Tauka: first names only."""
    from app.services.makeup_service import _names
    tutors: set[str] = set(hs.linked_tutor_ids(student_id))
    learners: set[str] = set()
    pods = [m["cohort_id"] for m in (_t("cohort_memberships").select("cohort_id")
                                     .eq("user_id", student_id).execute()).data or []]
    if pods:
        learners |= {m["user_id"] for m in (_t("cohort_memberships").select("user_id")
                                           .in_("cohort_id", pods).execute()).data or []}
        tutors |= {c["tutor_id"] for c in (_t("cohorts").select("tutor_id").in_("id", pods)
                                          .execute()).data or [] if c.get("tutor_id")}
        tutors |= {s["lead_tutor_id"] for s in (_t("cohort_session").select("lead_tutor_id")
                                               .in_("cohort_id", pods).execute()).data or [] if s.get("lead_tutor_id")}
    seats = [s["makeup_session_id"] for s in (_t("makeup_session_participant").select("makeup_session_id")
                                              .eq("student_id", student_id).execute()).data or []]
    if seats:
        learners |= {s["student_id"] for s in (_t("makeup_session_participant").select("student_id")
                                               .in_("makeup_session_id", seats).execute()).data or []}
        tutors |= {m["tutor_id"] for m in (_t("makeup_session").select("tutor_id").in_("id", seats)
                                          .execute()).data or [] if m.get("tutor_id")}
    learners.discard(student_id)
    tutors.discard(student_id)
    names = _names(list(tutors | learners))
    first = {k: (v or "").split(" ")[0] or "Someone" for k, v in names.items()}
    return ([{"id": t, "name": first.get(t, "A tutor"), "kind": "tutor"} for t in sorted(tutors)]
            + [{"id": u, "name": first.get(u, "A learner"), "kind": "learner"} for u in sorted(learners - tutors)])


# ── learner ──────────────────────────────────────────────────────────────────

def create(student_id: str, about_kind: str, about_user_id: str | None, category: str,
           detail: str | None, session_type: str | None = None, session_id: str | None = None) -> dict:
    if about_kind not in ABOUT or category not in CATEGORIES:
        raise ConcernRefused("bad_request")
    if about_kind in ("tutor", "learner"):
        known = {p["id"]: p["kind"] for p in people(student_id)}
        if about_user_id and known.get(about_user_id) != about_kind:
            raise ConcernRefused("unknown_person")
    else:
        about_user_id = None
    if (session_type is None) != (session_id is None):
        raise ConcernRefused("bad_request")
    if session_id:
        from app.services.complaint_service import recent_sessions
        mine = {(s["session_type"], s["session_id"]) for s in recent_sessions(student_id)}
        if (session_type, session_id) not in mine:
            raise ConcernRefused("not_your_session")
    detail = (detail or "").strip()[:1000] or None
    row = (_t("concern_report").insert({
        "reporter_id": student_id, "about_kind": about_kind, "about_user_id": about_user_id,
        "category": category, "detail": detail, "session_type": session_type, "session_id": session_id,
    }).execute()).data[0]
    return row


def mine(student_id: str) -> list[dict]:
    """The reporter's own reports: category, when, and where it stands."""
    return (_t("concern_report").select("id, about_kind, category, status, created_at, acknowledged_at, closed_at")
            .eq("reporter_id", student_id).order("created_at", desc=True).execute()).data or []


# ── safeguarding ─────────────────────────────────────────────────────────────

def _require(actor: str) -> None:
    if not hs.admin_can(actor, "safeguarding"):
        raise ConcernRefused("not_allowed")


def queue(actor: str) -> list[dict]:
    _require(actor)
    from app.services.makeup_service import _names
    rows = (_t("concern_report").select("*").order("created_at").execute()).data or []
    now = _now()
    rows = [r for r in rows if r["status"] != "closed" or (_ts(r.get("closed_at")) or now) >= now - timedelta(days=30)]
    names = _names([r["reporter_id"] for r in rows] + [r["about_user_id"] for r in rows if r.get("about_user_id")])
    return [{**r, "reporter_name": names.get(r["reporter_id"]), "about_name": names.get(r.get("about_user_id")),
             "overdue": r["status"] == "open" and now - _ts(r["created_at"]) >= OVERDUE} for r in rows]


def acknowledge(actor: str, concern_id: str) -> dict:
    _require(actor)
    got = (_t("concern_report").update({"status": "acknowledged", "acknowledged_at": _now().isoformat(),
                                        "handled_by": actor})
           .eq("id", concern_id).eq("status", "open").execute()).data or []
    if not got:
        raise ConcernRefused("not_open")
    return got[0]


def close(actor: str, concern_id: str, outcome: str) -> dict:
    _require(actor)
    if outcome not in OUTCOMES:
        raise ConcernRefused("bad_outcome")
    rows = (_t("concern_report").select("status, acknowledged_at").eq("id", concern_id).limit(1).execute()).data or []
    if not rows:
        raise ConcernRefused("no_such_concern")
    if rows[0]["status"] == "closed":
        raise ConcernRefused("closed")
    now = _now().isoformat()
    patch = {"status": "closed", "closed_at": now, "outcome": outcome, "handled_by": actor}
    got = (_t("concern_report").update(patch).eq("id", concern_id).in_("status", ["open", "acknowledged"])
           .execute()).data or []
    if not got:
        raise ConcernRefused("closed")
    return got[0]


# ── alerts ───────────────────────────────────────────────────────────────────

def _safeguarding_emails() -> list[str]:
    from app.services.makeup_delivery import _email_of
    admins = [a["id"] for a in (_t("admin_users").select("id, deleted_at").execute()).data or []
              if not a.get("deleted_at")]
    emails = [e for e in (_email_of(a) for a in admins if hs.admin_can(a, "safeguarding")) if e]
    return emails or ([settings.admin_email] if settings.admin_email else [])


async def _alert(r: dict, overdue: bool) -> bool:
    to = _safeguarding_emails()
    if not to:
        return False
    label = CATEGORY_LABEL.get(r["category"], r["category"])
    subject = ("Concern report not acknowledged after 24 hours: " if overdue else "New concern report: ") + label
    body = (f"<p>{'A concern report has waited 24 hours without being acknowledged.' if overdue else 'A learner has sent a concern report.'}</p>"
            f"<p>Category: {label}. The details are on the safeguarding page; they are not in this email.</p>"
            f"<p><a href=\"{settings.web_base_url}/admin/concerns\">Open the safeguarding queue</a></p>")
    res = await email_service.send_email(to, subject, body, tags=["concern", "overdue" if overdue else "new"])
    return (res or {}).get("status") != "failed"


async def alert_new(concern_id: str) -> None:
    rows = (_t("concern_report").select("*").eq("id", concern_id).limit(1).execute()).data or []
    if rows and not rows[0].get("alert_sent_at") and await _alert(rows[0], False):
        _t("concern_report").update({"alert_sent_at": _now().isoformat()}).eq("id", concern_id).execute()


async def daily(now: datetime | None = None) -> dict:
    """Resend any alert that failed, and the 24-hour overdue alert, once."""
    now = now or _now()
    out = {"alerts": 0, "overdue": 0}
    for r in (_t("concern_report").select("*").eq("status", "open").execute()).data or []:
        if not r.get("alert_sent_at") and await _alert(r, False):
            _t("concern_report").update({"alert_sent_at": now.isoformat()}).eq("id", r["id"]).execute()
            out["alerts"] += 1
        if (not r.get("overdue_alert_sent_at") and now - _ts(r["created_at"]) >= OVERDUE
                and await _alert(r, True)):
            _t("concern_report").update({"overdue_alert_sent_at": now.isoformat()}).eq("id", r["id"]).execute()
            out["overdue"] += 1
    return out
