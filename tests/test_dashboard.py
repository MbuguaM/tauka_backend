"""Ops dashboards (enrolment plan Track 9). Ops only; display only (E-16).

Pinned:
1. Ops only.
2. Formation board: the chosen (or next) intake, states by band and level,
   the readiness queue, the match result.
3. Benchmarks: values, targets, the two-consecutive trigger, the < 6 pods caveat;
   a snapshot once per formation.
4. Funnel: each step, and second-month renewals as the north star.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.services import dashboard_service as ds
from app.services import hierarchy_service
from tests.fakedb import FakeDB

NOW = datetime(2027, 1, 20, tzinfo=timezone.utc)


@pytest.fixture
def db():
    fake = FakeDB({
        "intake": [{"id": "tir", "ethiopic_month": "tir", "ec_year": 2019, "formation_date": "2027-01-09",
                    "cutoff_closes": "2027-01-05", "status": "formed", "mode": "live"},
                   {"id": "yek", "ethiopic_month": "yekatit", "ec_year": 2019, "formation_date": "2099-02-08",
                    "cutoff_closes": "2099-02-04", "status": "scheduled", "mode": "live"}],
        "enrolment_state": [
            {"student_id": "a", "state": "in_pod", "band": "ea_gulf", "target_intake_id": "tir",
             "phase0_started_at": "2026-12-01T00:00:00+00:00", "phase0_completed_at": "2026-12-15T00:00:00+00:00",
             "state_changed_at": "2027-01-09T00:00:00+00:00"},
            {"student_id": "b", "state": "phase0", "band": "ea_gulf", "target_intake_id": "tir",
             "phase0_started_at": "2026-12-20T00:00:00+00:00", "phase0_completed_at": None,
             "state_changed_at": "2026-12-20T00:00:00+00:00"}],
        "student": [{"id": "a", "assessed_level": "A1"}, {"id": "b", "assessed_level": "A1"}],
        "intake_cycle": [{"intake_id": "tir", "mode": "live", "cohorts_formed": 1, "students_placed": 3,
                          "students_deferred": 1, "status": "closed"}],
        "cohorts": [{"id": "p1", "status": "active", "tutor_id": "t1"}],
        "cohort_memberships": [{"cohort_id": "p1", "user_id": u, "status": "active"} for u in ("a", "c", "d")],
        "tutor": [{"id": "t1", "status": "active"}, {"id": "t2", "status": "active"}],
        "fidel_approval": [{"created_at": "2027-01-01T00:00:00+00:00", "reviewed_at": "2027-01-02T12:00:00+00:00"}],
        "clinic_booking": [{"student_id": "a", "booked_at": "2026-12-04T00:00:00+00:00"}],
        "benchmark_snapshot": [], "waitlist_entry": [{"id": 1}, {"id": 2}, {"id": 3}],
        "test_sessions": [{"status": "completed"}, {"status": "phase_1"}],
        "lesson_entitlement": [{"student_id": "a", "source": "paid_month"}, {"student_id": "a", "source": "paid_month"},
                               {"student_id": "c", "source": "paid_month"}],
    })
    with patch("app.services.dashboard_service.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"):
        yield fake


def test_ops_only(db):
    with pytest.raises(ds.DashboardRefused):
        ds.funnel("someone")


def test_formation(db):
    out = ds.formation("ops", "tir")
    assert out["intake"]["label"] == "tir 2019"
    assert out["totals"]["in_pod"] == 1 and out["totals"]["phase0"] == 1
    assert out["grid"] == [{"band": "ea_gulf", "level": "1", **{s: 0 for s in ds.STATES}, "in_pod": 1, "phase0": 1}]
    assert out["readiness_queue"] == 1 and out["match"]["students_placed"] == 3
    assert ds.formation("ops", today="2027-01-20")["intake"]["id"] == "yek"                   # the next one by default


def test_benchmarks_and_snapshot(db):
    out = ds.benchmarks("ops", NOW)
    vals = {r["key"]: r for r in out["rows"]}
    assert vals["pod_size"]["value"] == 3.0 and vals["match_rate"]["value"] == 0.75
    assert vals["ready_to_pod_days"]["value"] == 25 and vals["review_turnaround_hours"]["value"] == 36.0
    assert vals["phase0_within_3_weeks"]["value"] == 1.0 and vals["enrol_to_first_clinic_days"]["value"] == 3
    assert vals["bench"]["value"] == 1 and out["caveat"] is True
    assert ds.snapshot(NOW) is True and ds.snapshot(NOW) is False
    db.rows["benchmark_snapshot"].append({"taken_on": "2027-02-08", "values": {"match_rate": 0.5}})
    db.rows["benchmark_snapshot"][0]["values"]["match_rate"] = 0.6
    vals = {r["key"]: r for r in ds.benchmarks("ops", NOW)["rows"]}
    assert vals["match_rate"]["trigger"] is True and vals["pod_size"]["trigger"] is False


def test_funnel(db):
    out = ds.funnel("ops")
    counts = {s["key"]: s["count"] for s in out["steps"]}
    assert counts == {"waitlist": 3, "placement": 1, "phase0": 2, "paid": 1, "pod": 3, "renewal": 1}
    assert out["north_star"] == 1
