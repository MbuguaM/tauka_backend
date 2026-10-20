"""Tutor status — enrolment plan §2j J4 (migration §122; CONFLICT-057).

After approval a tutor is `onboarding`. Ops moves them on:

    onboarding -> active | offboarded
    active     -> suspended | offboarded
    suspended  -> active | offboarded
    offboarded    (final; sets deleted_at)

Every change is an append-only `tutor_status_event` with a reason CODE.
Nothing else changes automatically: matching, make-ups, referrals and clinics
already only use `active` tutors. Suspending or offboarding a tutor who still
leads sessions returns how many are upcoming, so ops arranges cover (a cover
request, §2h, is the tutor's own).

J5 (§124): onboarding -> active needs the five checklist steps done: ID check,
references, contract, training, calibration. Ops ticks each (append-only
`tutor_onboarding_check`, latest row per step wins); calibration carries the
agreement score, >= 85 to pass. No documents are kept in Tauka.

Ops also sets the two things an application can't supply cleanly: the
tutor's language and country (the rate card is chosen by country).

`me()` is the tutor's own view: their status and, while onboarding, what's left.
"""

import logging
import re
from datetime import datetime, timezone

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

TRANSITIONS = {
    "onboarding": ("active", "offboarded"),
    "active": ("suspended", "offboarded"),
    "suspended": ("active", "offboarded"),
}
CHECK_STEPS = ("id_check", "references", "contract", "training", "calibration")
CALIBRATION_PASS = 85
_REF = re.compile(r"^[A-Za-z0-9_./-]{1,40}$")
REASONS = ("onboarding_complete", "returning", "leave", "conduct", "reliability", "left", "other")


class TutorStatusRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise TutorStatusRefused("not_allowed")


def _tutor(tutor_id: str) -> dict:
    rows = (_t("tutor").select("id, status, deleted_at, language, country_code").eq("id", tutor_id)
            .limit(1).execute()).data or []
    if not rows:
        raise TutorStatusRefused("no_such_tutor")
    return rows[0]


def record(tutor_id: str, from_status: str | None, to_status: str, reason: str, actor: str | None) -> None:
    _t("tutor_status_event").insert({"tutor_id": tutor_id, "from_status": from_status, "to_status": to_status,
                                     "reason_code": reason, "actor": actor}).execute()


def upcoming_led(tutor_id: str, now: datetime | None = None) -> dict:
    """Sessions this tutor still leads: what needs cover if they stop."""
    now = (now or _now()).isoformat()
    pod = (_t("cohort_session").select("id").eq("lead_tutor_id", tutor_id).eq("status", "scheduled")
           .gte("scheduled_at", now).execute()).data or []
    mk = (_t("makeup_session").select("id").eq("tutor_id", tutor_id).eq("status", "scheduled")
          .gte("scheduled_at", now).execute()).data or []
    cl = (_t("clinic").select("id").eq("tutor_id", tutor_id).eq("status", "scheduled")
          .gte("starts_at", now).execute()).data or []
    return {"pod_sessions": len(pod), "makeups": len(mk), "clinics": len(cl)}


def checklist(tutor_id: str) -> dict:
    """Each step's current state: its latest row, or not done."""
    rows = (_t("tutor_onboarding_check").select("step, done, score, ref_code, created_at")
            .eq("tutor_id", tutor_id).order("created_at").execute()).data or []
    out = {s: {"step": s, "done": False, "score": None, "ref_code": None, "at": None} for s in CHECK_STEPS}
    for r in sorted(rows, key=lambda r: str(r.get("created_at"))):
        if r.get("step") in out:
            out[r["step"]] = {"step": r["step"], "done": bool(r.get("done")), "score": r.get("score"),
                              "ref_code": r.get("ref_code"), "at": r.get("created_at")}
    return out


def missing_checks(tutor_id: str) -> list[str]:
    c = checklist(tutor_id)
    return [s for s in CHECK_STEPS if not c[s]["done"]]


def mark_check(actor: str, tutor_id: str, step: str, done: bool,
               score: int | None = None, ref_code: str | None = None) -> dict:
    """Ops ticks (or unticks, to correct) one onboarding step."""
    _require_ops(actor)
    if step not in CHECK_STEPS:
        raise TutorStatusRefused("bad_step")
    _tutor(tutor_id)
    ref = (ref_code or "").strip() or None
    if ref and not _REF.match(ref):
        raise TutorStatusRefused("bad_ref")
    if step == "calibration":
        if done and (score is None or not 0 <= score <= 100):
            raise TutorStatusRefused("score_required")
        if done and score < CALIBRATION_PASS:
            done = False  # recorded, but a fail: calibration is not done
    else:
        score = None
    _t("tutor_onboarding_check").insert({"tutor_id": tutor_id, "step": step, "done": done, "score": score,
                                         "ref_code": ref, "actor": actor}).execute()
    return {"tutor_id": tutor_id, "checklist": list(checklist(tutor_id).values())}


def set_status(actor: str, tutor_id: str, to_status: str, reason: str) -> dict:
    _require_ops(actor)
    if reason not in REASONS:
        raise TutorStatusRefused("bad_reason")
    t = _tutor(tutor_id)
    frm = t.get("status") or "active"
    if to_status not in TRANSITIONS.get(frm, ()):
        raise TutorStatusRefused("not_allowed_from_" + frm)
    if frm == "onboarding" and to_status == "active" and missing_checks(tutor_id):
        raise TutorStatusRefused("checks_incomplete")
    patch = {"status": to_status}
    if to_status == "offboarded":
        patch["deleted_at"] = _now().isoformat()
    # CONFLICT-057: service role; guard_tutor_self_update lets it through.
    _t("tutor").update(patch).eq("id", tutor_id).eq("status", frm).execute()
    record(tutor_id, frm, to_status, reason, actor)
    out = {"tutor_id": tutor_id, "from": frm, "status": to_status}
    if to_status in ("suspended", "offboarded"):
        out["needs_cover"] = upcoming_led(tutor_id)
    return out


def set_details(actor: str, tutor_id: str, language: str | None, country_code: str | None) -> dict:
    _require_ops(actor)
    _tutor(tutor_id)
    patch = {}
    if language is not None:
        patch["language"] = language.strip()[:60] or None
    if country_code is not None:
        cc = country_code.strip().upper()
        if cc and not re.fullmatch(r"[A-Z]{2}", cc):
            raise TutorStatusRefused("bad_country")
        patch["country_code"] = cc or None
    if patch:
        _t("tutor").update(patch).eq("id", tutor_id).execute()
    return _tutor(tutor_id)


def history(actor: str, tutor_id: str) -> list[dict]:
    _require_ops(actor)
    return (_t("tutor_status_event").select("from_status, to_status, reason_code, actor, created_at")
            .eq("tutor_id", tutor_id).order("created_at").execute()).data or []


def me(tutor_id: str) -> dict:
    """The tutor's own status and, while onboarding, the steps left.
    Availability is theirs to set in the app; the five checks are ticked by
    the team (J5, §124)."""
    rows = (_t("tutor").select("status").eq("id", tutor_id).limit(1).execute()).data or []
    if not rows:
        return {"is_tutor": False}
    status = rows[0].get("status") or "active"
    availability = bool((_t("tutor_availability").select("id").eq("tutor_id", tutor_id).eq("active", True)
                         .limit(1).execute()).data)
    checks = checklist(tutor_id)
    steps = [{"key": "availability", "done": availability}] + [
        {"key": s, "done": status == "active" or checks[s]["done"]} for s in CHECK_STEPS]
    return {"is_tutor": True, "status": status, "steps": steps}
