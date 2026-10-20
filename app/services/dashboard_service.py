"""Ops dashboards — enrolment plan Track 9. Read-only; ops only.

  formation(intake)  the formation board: the pipeline by state, band and
                     level; the readiness queue; the match result.
  benchmarks()       the playbook's benchmark table, each with its target and
                     the two-consecutive-formations rule, plus the caveat
                     "directions, not triggers" below 6 pods per band.
  funnel()           waitlist -> placement -> Phase 0 -> paid -> pod ->
                     second paid month ("unit-2 renewal", the north star).

E-16: these DISPLAY numbers; nothing reads them to decide pay, matching or
access. No telemetry (activity_summary, ai_usage) is read here.
"""

import logging
from datetime import datetime, timedelta, timezone
from statistics import median

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

STATES = ("phase0", "paid", "seat_confirmed", "matched", "in_pod", "wait_pool", "paused", "cancelled")
CAVEAT_PODS = 6

# The playbook's benchmarks: (key, label, target, better)
BENCHMARKS = (
    ("learners_per_tutor", "Learners per active tutor", 12, "higher"),
    ("pod_size", "Average pod size", 3.6, "higher"),
    ("match_rate", "Match rate (placed / pool)", 0.85, "higher"),
    ("ready_to_pod_days", "Ready → pod, median days", 21, "lower"),
    ("review_turnaround_hours", "Fidel review turnaround, median hours", 48, "lower"),
    ("phase0_within_3_weeks", "Phase 0 finished within 3 weeks", 0.7, "higher"),
    ("enrol_to_first_clinic_days", "Enrolment → first clinic, median days", 7, "lower"),
    ("bench", "Active tutors with no pod (bench)", None, "info"),
)


class DashboardRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _ts(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _require(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise DashboardRefused("not_allowed")


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(median(xs), 1) if xs else None


# ── the formation board ─────────────────────────────────────────────────────

def formation(actor: str, intake_id: str | None = None, today: str | None = None) -> dict:
    from app.services.cohort_matcher import LEVEL_FROM_CEFR
    _require(actor)
    intakes = sorted((_t("intake").select("id, ethiopic_month, ec_year, formation_date, cutoff_closes, status, mode")
                      .execute()).data or [], key=lambda r: str(r["formation_date"]))
    if not intakes:
        return {"intakes": [], "intake": None}
    today = today or datetime.now(timezone.utc).date().isoformat()
    chosen = next((i for i in intakes if i["id"] == intake_id), None) or \
        next((i for i in intakes if str(i["formation_date"]) >= today and i.get("status") != "cancelled"), intakes[-1])
    rows = (_t("enrolment_state").select("student_id, state, band, phase0_started_at, phase0_completed_at")
            .eq("target_intake_id", chosen["id"]).execute()).data or []
    levels = {}
    ids = [r["student_id"] for r in rows]
    if ids:
        levels = {s["id"]: LEVEL_FROM_CEFR.get((s.get("assessed_level") or "").upper())
                  for s in (_t("student").select("id, assessed_level").in_("id", ids).execute()).data or []}
    grid: dict[str, dict] = {}
    for r in rows:
        key = f"{r.get('band') or 'unknown'}|{levels.get(r['student_id']) or '?'}"
        cell = grid.setdefault(key, {s: 0 for s in STATES})
        cell[r["state"]] = cell.get(r["state"], 0) + 1
    readiness = [{"student_id": r["student_id"], "started": r.get("phase0_started_at")}
                 for r in rows if r["state"] in ("phase0", "paid") and not r.get("phase0_completed_at")]
    cycle = (_t("intake_cycle").select("cohorts_formed, students_placed, students_deferred, status")
             .eq("intake_id", chosen["id"]).eq("mode", "live").limit(1).execute()).data or []
    return {
        "intakes": [{"id": i["id"], "label": f"{i['ethiopic_month']} {i['ec_year']}", "formation_date": i["formation_date"],
                     "mode": i.get("mode"), "status": i.get("status")} for i in intakes],
        "intake": {"id": chosen["id"], "label": f"{chosen['ethiopic_month']} {chosen['ec_year']}",
                   "formation_date": chosen["formation_date"], "cutoff_closes": chosen["cutoff_closes"]},
        "totals": {s: sum(1 for r in rows if r["state"] == s) for s in STATES},
        "grid": [{"band": k.split("|")[0], "level": k.split("|")[1], **v} for k, v in sorted(grid.items())],
        "readiness_queue": len(readiness),
        "match": cycle[0] if cycle else None,
    }


# ── benchmarks ──────────────────────────────────────────────────────────────

def _values(now: datetime) -> dict:
    pods = [p for p in (_t("cohorts").select("id, status").execute()).data or [] if p.get("status") == "active"]
    members = (_t("cohort_memberships").select("cohort_id, user_id, status").eq("status", "active").execute()).data or []
    live = {p["id"] for p in pods}
    seated = [m for m in members if m["cohort_id"] in live]
    tutors = [t for t in (_t("tutor").select("id, status, deleted_at").execute()).data or []
              if t.get("status") == "active" and not t.get("deleted_at")]
    pod_tutors = {c["tutor_id"] for c in (_t("cohorts").select("tutor_id, status").execute()).data or []
                  if c.get("status") == "active" and c.get("tutor_id")}
    cycles = (_t("intake_cycle").select("students_placed, students_deferred, mode").eq("mode", "live")
              .execute()).data or []
    placed = sum(int(c.get("students_placed") or 0) for c in cycles)
    pool = placed + sum(int(c.get("students_deferred") or 0) for c in cycles)
    states = (_t("enrolment_state").select("state, state_changed_at, phase0_started_at, phase0_completed_at, student_id")
              .execute()).data or []
    ready_days = [(_ts(s["state_changed_at"]) - _ts(s["phase0_completed_at"])).days for s in states
                  if s["state"] == "in_pod" and s.get("phase0_completed_at")]
    done = [s for s in states if s.get("phase0_started_at") and s.get("phase0_completed_at")]
    within = [1 if (_ts(s["phase0_completed_at"]) - _ts(s["phase0_started_at"])).days <= 21 else 0 for s in done]
    reviews = [(_ts(r["reviewed_at"]) - _ts(r["created_at"])).total_seconds() / 3600
               for r in (_t("fidel_approval").select("created_at, reviewed_at").execute()).data or [] if r.get("reviewed_at")]
    started = {s["student_id"]: _ts(s["phase0_started_at"]) for s in states if s.get("phase0_started_at")}
    firsts: dict[str, datetime] = {}
    for b in (_t("clinic_booking").select("student_id, booked_at").execute()).data or []:
        at = _ts(b["booked_at"])
        if b["student_id"] not in firsts or at < firsts[b["student_id"]]:
            firsts[b["student_id"]] = at
    clinic_days = [(firsts[s] - started[s]).days for s in firsts if s in started]
    return {
        "learners_per_tutor": round(len(seated) / len(pod_tutors), 1) if pod_tutors else None,
        "pod_size": round(len(seated) / len(pods), 1) if pods else None,
        "match_rate": round(placed / pool, 2) if pool else None,
        "ready_to_pod_days": _med(ready_days),
        "review_turnaround_hours": _med(reviews),
        "phase0_within_3_weeks": round(sum(within) / len(within), 2) if within else None,
        "enrol_to_first_clinic_days": _med(clinic_days),
        "bench": len([t for t in tutors if t["id"] not in pod_tutors]),
        "_pods": len(pods),
    }


def _ok(value, target, better):
    if value is None or target is None or better == "info":
        return None
    return value >= target if better == "higher" else value <= target


def benchmarks(actor: str, now: datetime | None = None) -> dict:
    """Now, plus whether each missed its target at the last two formations
    (`benchmark_snapshot` rows written by snapshot() after each formation)."""
    _require(actor)
    now = now or datetime.now(timezone.utc)
    vals = _values(now)
    snaps = sorted((_t("benchmark_snapshot").select("taken_on, values").execute()).data or [],
                   key=lambda r: str(r["taken_on"]))[-2:]
    rows = []
    for key, label, target, better in BENCHMARKS:
        missed = [_ok((s.get("values") or {}).get(key), target, better) is False for s in snaps]
        rows.append({"key": key, "label": label, "value": vals.get(key), "target": target, "better": better,
                     "ok": _ok(vals.get(key), target, better),
                     "trigger": len(missed) == 2 and all(missed)})
    return {"rows": rows, "pods": vals["_pods"],
            "caveat": vals["_pods"] < CAVEAT_PODS,
            "note": ("Fewer than 6 live pods: read these as directions, not triggers." if vals["_pods"] < CAVEAT_PODS
                     else "A metric missing its target at two consecutive formations is a trigger.")}


def snapshot(now: datetime | None = None) -> bool:
    """After a formation closes (daily job, once per formation day), keep the
    benchmark values so the two-consecutive rule can be read."""
    now = now or datetime.now(timezone.utc)
    formed = [i for i in (_t("intake").select("formation_date, status").eq("status", "formed").execute()).data or []]
    if not formed:
        return False
    last = max(str(i["formation_date"])[:10] for i in formed)
    have = (_t("benchmark_snapshot").select("taken_on").eq("taken_on", last).limit(1).execute()).data or []
    if have:
        return False
    vals = _values(now)
    vals.pop("_pods", None)
    _t("benchmark_snapshot").insert({"taken_on": last, "values": vals}).execute()
    return True


# ── the funnel ──────────────────────────────────────────────────────────────

def funnel(actor: str) -> dict:
    _require(actor)
    waitlist = len((_t("waitlist_entry").select("id").execute()).data or [])
    placement = len([s for s in (_t("test_sessions").select("status").execute()).data or []
                     if s.get("status") in ("completed", "complete", "finished")])
    states = (_t("enrolment_state").select("student_id, state, phase0_completed_at").execute()).data or []
    phase0 = len({s["student_id"] for s in states})
    paid = len({s["student_id"] for s in states if s["state"] not in ("waitlisted", "phase0")})
    pod = len({s["student_id"] for s in states if s["state"] in ("in_pod", "paused")}) + \
        len({m["user_id"] for m in (_t("cohort_memberships").select("user_id, status").execute()).data or []
             if m.get("status") in ("active", "graduated")} - {s["student_id"] for s in states})
    months: dict[str, int] = {}
    for g in (_t("lesson_entitlement").select("student_id, source, revoked_at").eq("source", "paid_month")
              .execute()).data or []:
        if not g.get("revoked_at"):
            months[g["student_id"]] = months.get(g["student_id"], 0) + 1
    renewed = sum(1 for n in months.values() if n >= 2)
    steps = [("waitlist", "Joined the waitlist", waitlist), ("placement", "Took the placement test", placement),
             ("phase0", "Started Phase 0", phase0), ("paid", "Paid", paid), ("pod", "In a pod", pod),
             ("renewal", "Renewed for a second month (north star)", renewed)]
    return {"steps": [{"key": k, "label": l, "count": c} for k, l, c in steps], "north_star": renewed}
