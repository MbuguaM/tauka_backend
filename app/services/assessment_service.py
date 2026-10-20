"""Exit assessments, certificates and the milestone guarantee — enrolment plan
Track 10, §2b, E-10, E-16; migration §138. Behind the `exit_assessment` flag.

  request(student)            after completing lesson 60 (B2), the learner asks
  schedule(ops, id, at, r1, r2)  two rung-4 raters, active, not the learner's own
                              tutors (plan: "enforced in the RPC")
  rate(rater, id, rating)     listening/speaking/reading/writing on CEFR + an
                              overall; when both are in: pass = both at the level
  pass -> certificate         a 10-character public code; /verify/<code> shows
                              name, level and date only
  not yet -> guarantee        Intensive only (§2b): attendance >= 85% from
                              tutor-marked pod attendance + peer attendance
                              (E-16, never telemetry) earns up to 2 free months
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CEFR = ("A1", "A2", "B1", "B2", "C1", "C2")
SKILLS = ("listening", "speaking", "reading", "writing")
RATER_RUNG = 4
GUARANTEE_BP = 8500
GUARANTEE_MONTHS = 2
GUARANTEE_WINDOW = timedelta(days=365)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class AssessmentRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _require_on() -> None:
    if not is_ready("exit_assessment"):
        raise AssessmentRefused("not_enabled")


def _lessons_done(student_id: str) -> int:
    from app.services.tracker_service import for_student
    try:
        return int(for_student(student_id).get("lessons_done") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker read failed for %s: %s", student_id, exc)
        return 0


def mine(student_id: str) -> dict:
    if not is_ready("exit_assessment"):
        return {"enabled": False}
    rows = (_t("exit_assessment").select("id, level, status, scheduled_at, outcome, decided_at")
            .eq("student_id", student_id).execute()).data or []
    cert = (_t("certificate").select("level, verify_code, issued_at, revoked_at").eq("student_id", student_id)
            .execute()).data or []
    from app.services.tracker_service import _b2_lesson
    done, b2 = _lessons_done(student_id), _b2_lesson()
    return {"enabled": True, "eligible": done >= b2 and not any(r["status"] in ("requested", "scheduled") for r in rows),
            "lessons_done": done, "b2_lesson": b2, "assessments": rows,
            "certificates": [c for c in cert if not c.get("revoked_at")]}


def request(student_id: str, level: str = "B2") -> dict:
    _require_on()
    from app.services.tracker_service import _b2_lesson
    if level != "B2":
        raise AssessmentRefused("c1_not_open")       # C1 isn't dated until Phase 2 exists (plan 4.7)
    if _lessons_done(student_id) < _b2_lesson():
        raise AssessmentRefused("not_yet_eligible")
    try:
        return (_t("exit_assessment").insert({"student_id": student_id, "level": level, "status": "requested"})
                .execute()).data[0]
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise AssessmentRefused("already_open") from exc
        raise


def eligible_raters(student_id: str) -> list[str]:
    own = set(hs.linked_tutor_ids(student_id))
    rows = (_t("tutor").select("id, status, deleted_at").eq("status", "active").execute()).data or []
    return sorted(r["id"] for r in rows if not r.get("deleted_at") and r["id"] not in own
                  and (hs.rung_of(r["id"]) or 0) >= RATER_RUNG)


def schedule(actor: str, assessment_id: str, at: datetime, rater_1: str, rater_2: str) -> dict:
    _require_on()
    if not hs.admin_can(actor, "ops"):
        raise AssessmentRefused("not_allowed")
    a = (_t("exit_assessment").select("*").eq("id", assessment_id).limit(1).execute()).data or []
    if not a or a[0]["status"] not in ("requested", "scheduled"):
        raise AssessmentRefused("not_open")
    ok = set(eligible_raters(a[0]["student_id"]))
    if rater_1 == rater_2 or rater_1 not in ok or rater_2 not in ok:
        raise AssessmentRefused("bad_raters")
    return (_t("exit_assessment").update({"status": "scheduled", "scheduled_at": at.isoformat(),
                                          "rater_1": rater_1, "rater_2": rater_2})
            .eq("id", assessment_id).execute()).data[0]


def _valid(rating: dict) -> bool:
    return (set(rating) == set(SKILLS) | {"overall"}) and all(rating[k] in CEFR for k in rating)


def rate(rater: str, assessment_id: str, rating: dict, now: datetime | None = None) -> dict:
    _require_on()
    now = now or _now()
    if not _valid(rating):
        raise AssessmentRefused("bad_rating")
    a = (_t("exit_assessment").select("*").eq("id", assessment_id).limit(1).execute()).data or []
    if not a or a[0]["status"] != "scheduled":
        raise AssessmentRefused("not_open")
    a = a[0]
    col = "rating_1" if a.get("rater_1") == rater else "rating_2" if a.get("rater_2") == rater else None
    if not col:
        raise AssessmentRefused("not_your_assessment")
    if a.get(col):
        raise AssessmentRefused("already_rated")
    a = (_t("exit_assessment").update({col: rating}).eq("id", assessment_id).execute()).data[0]
    if a.get("rating_1") and a.get("rating_2"):
        return _decide(a, now)
    return a


def _decide(a: dict, now: datetime) -> dict:
    target = CEFR.index(a["level"])
    passed = all(CEFR.index(r["overall"]) >= target for r in (a["rating_1"], a["rating_2"]))
    got = (_t("exit_assessment").update({"status": "done", "outcome": "pass" if passed else "not_yet",
                                         "decided_at": now.isoformat()})
           .eq("id", a["id"]).eq("status", "scheduled").execute()).data or []
    if not got:
        return a
    a = got[0]
    if passed:
        issue_certificate(a)
    else:
        guarantee(a, now)
    return a


# ── certificates ────────────────────────────────────────────────────────────

def _code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


def issue_certificate(a: dict) -> dict:
    from app.services.makeup_service import _names
    name = _names([a["student_id"]]).get(a["student_id"]) or "Tauka learner"
    for _ in range(5):
        try:
            return (_t("certificate").insert({"student_id": a["student_id"], "assessment_id": a["id"],
                                              "level": a["level"], "verify_code": _code(),
                                              "holder_name": name[:160],
                                              "issued_at": _now().isoformat()}).execute()).data[0]
        except Exception as exc:  # noqa: BLE001
            if "assessment_id" in str(exc):
                raise
    raise AssessmentRefused("code_collision")


def verify(code: str) -> dict | None:
    """Public: name, level, issue date — or None. Nothing else about the person."""
    rows = (_t("certificate").select("holder_name, level, issued_at, revoked_at")
            .eq("verify_code", (code or "").strip().upper()).limit(1).execute()).data or []
    if not rows or rows[0].get("revoked_at"):
        return None
    r = rows[0]
    return {"holder_name": r["holder_name"], "level": r["level"], "issued_at": str(r["issued_at"])[:10],
            "issuer": "Tauka"}


# ── the guarantee (§2b, E-16) ───────────────────────────────────────────────

def attendance_bp(student_id: str, since: datetime) -> int:
    """Tutor-marked pod attendance plus peer-session and exchange attendance, in basis points.
    Excused counts as neither (left out of both sides)."""
    marks = (_t("cohort_session_attendance").select("cohort_session_id, status").eq("user_id", student_id)
             .execute()).data or []
    sessions = {s["id"]: s for s in (_t("cohort_session").select("id, scheduled_at")
                .in_("id", [m["cohort_session_id"] for m in marks] or ["00000000-0000-0000-0000-000000000000"]).execute()).data or []}
    held = [m for m in marks if m["cohort_session_id"] in sessions
            and _ts(sessions[m["cohort_session_id"]]["scheduled_at"]) >= since and m["status"] in ("attended", "absent")]
    attended = sum(1 for m in held if m["status"] == "attended")
    from app.services.peer_service import attended as peer_attended
    try:
        peers = peer_attended(student_id, since)
    except Exception:  # noqa: BLE001
        peers = 0
    # X-10 (features addendum, 2026-10-20): completed exchanges count as peer
    # practice too. Their completion is both people's ratings, never telemetry (E-16).
    from app.services.exchange_service import attended as exchange_attended
    try:
        peers += exchange_attended(student_id, since)
    except Exception:  # noqa: BLE001
        pass
    total = len(held)
    return int(10000 * (attended + peers) / (total + peers)) if (total + peers) else 0


def guarantee(a: dict, now: datetime) -> dict | None:
    tier = ((_t("student").select("tier").eq("id", a["student_id"]).limit(1).execute()).data or [{}])[0].get("tier")
    if tier != "intensive_tier":
        return None
    bp = attendance_bp(a["student_id"], now - GUARANTEE_WINDOW)
    months = GUARANTEE_MONTHS if bp >= GUARANTEE_BP else 0
    row = (_t("guarantee_claim").insert({"student_id": a["student_id"], "assessment_id": a["id"], "attendance_bp": bp,
                                         "months_granted": months,
                                         "status": "granted" if months else "declined"}).execute()).data[0]
    if months:
        apply_guarantee(row)
    return row


def apply_guarantee(claim: dict) -> None:
    """A 100%-off coupon for the granted months on the learner's subscription,
    when STRIPE_COUPON_GUARANTEE is set; otherwise ops applies it by hand (the
    claim stays 'granted' and shows on the ops list)."""
    if not settings.stripe_coupon_guarantee:
        return
    try:
        import stripe
        st = (_t("student").select("stripe_subscription_id").eq("id", claim["student_id"]).limit(1).execute()).data or [{}]
        sub = st[0].get("stripe_subscription_id")
        if not sub:
            return
        stripe.Subscription.modify(sub, coupon=settings.stripe_coupon_guarantee)
        _t("guarantee_claim").update({"status": "applied", "applied_ref": sub}).eq("id", claim["id"]).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("guarantee coupon for %s failed: %s", claim["student_id"], exc)


# ── ops ──────────────────────────────────────────────────────────────────────

def ops_list(actor: str) -> dict:
    if not hs.admin_can(actor, "ops"):
        raise AssessmentRefused("not_allowed")
    from app.services.makeup_service import _names
    rows = (_t("exit_assessment").select("*").execute()).data or []
    open_ = [r for r in rows if r["status"] in ("requested", "scheduled")]
    names = _names([r["student_id"] for r in open_]) if open_ else {}
    claims = (_t("guarantee_claim").select("*").eq("status", "granted").execute()).data or []
    return {"open": [{**r, "student_name": names.get(r["student_id"]),
                      "raters": eligible_raters(r["student_id"])} for r in open_],
            "guarantees_to_apply": claims}


def to_rate(rater: str) -> list[dict]:
    """A rater's scheduled assessments still waiting for their rating."""
    rows = (_t("exit_assessment").select("id, level, scheduled_at, status, rater_1, rater_2, rating_1, rating_2")
            .eq("status", "scheduled").execute()).data or []
    out = []
    for r in rows:
        col = "rating_1" if r.get("rater_1") == rater else "rating_2" if r.get("rater_2") == rater else None
        if col and not r.get(col):
            out.append({"id": r["id"], "level": r["level"], "scheduled_at": r["scheduled_at"]})
    return sorted(out, key=lambda r: str(r["scheduled_at"]))
