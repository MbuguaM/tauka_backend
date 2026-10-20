"""Monthly pod checkpoints (enrolment plan 4.2, §109).

Pinned:
1. One checkpoint per live pod per Ethiopic month, opened on or after day 1;
   none in Pagume; a pod that starts on day 1 waits for next month.
2. The snapshot reads tutor-marked attendance, the sessions and their leads,
   seats, lesson and pledge — and flags low attendance and a lesson outside
   the band. It never reads activity or AI usage tables.
3. Ops reviews once. A pace move takes the member out of the pod and back to
   the wait pool; "continue" cannot move anyone; dissolve drops everyone.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import checkpoint_service as cs
from app.services import hierarchy_service
from app.services.checkpoint_service import CheckpointRefused

NOW = datetime(2027, 1, 10, 9, tzinfo=timezone.utc)
TIR_1 = date(2027, 1, 9)        # 1 Tir 2019
TAHSAS_1 = date(2026, 12, 10)   # 1 Tahsas 2019
OPS, NOBODY = "ops", "nobody"


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)
        db.read.add(name)

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def lt(self, c, v): self.f.append(lambda r: str(r.get(c)) < str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if self.name == "pod_checkpoint" and any(
                    (r["cohort_id"], r["ec_year"], r["ec_month"]) == (p["cohort_id"], p["ec_year"], p["ec_month"])
                    for r in rows):
                raise Exception("duplicate key pod_checkpoint_one_per_month")
            row = {"id": f"cp{len(rows) + 1}", "status": "open", **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.read: set[str] = set()
        self.rows = {
            "cohorts": [
                {"id": "pod1", "name": "Tir A", "tutor_id": "home", "max_students": 4, "status": "active",
                 "start_date": "2026-12-10", "band_lesson_low": 3, "band_lesson_high": 6, "level": 1},
                # Starts on 1 Tir itself: its first checkpoint is 1 Yekatit.
                {"id": "pod-new", "name": "Tir B", "tutor_id": "home", "max_students": 4, "status": "forming",
                 "start_date": "2027-01-09", "level": 1},
                {"id": "pod-gone", "name": "Old", "status": "dissolved", "start_date": "2026-10-01"},
            ],
            "cohort_memberships": [
                {"cohort_id": "pod1", "user_id": "a", "status": "active"},
                {"cohort_id": "pod1", "user_id": "b", "status": "active"},
                {"cohort_id": "pod1", "user_id": "c", "status": "active"},
            ],
            "cohort_session": [
                {"id": f"s{i}", "cohort_id": "pod1", "scheduled_at": f"2026-12-{12 + 7 * i}T15:00:00+00:00",
                 "status": "completed", "lead_tutor_id": "guest" if i == 1 else "home",
                 "lead_role": "guest" if i == 1 else "home"} for i in range(3)
            ] + [{"id": "s-next", "cohort_id": "pod1", "scheduled_at": "2027-01-09T15:00:00+00:00",
                  "status": "scheduled", "lead_tutor_id": "home", "lead_role": "home"}],
            "cohort_session_attendance": (
                [{"cohort_session_id": f"s{i}", "user_id": "a", "status": "attended"} for i in range(3)]
                + [{"cohort_session_id": f"s{i}", "user_id": "b", "status": "absent"} for i in range(2)]
                + [{"cohort_session_id": "s2", "user_id": "b", "status": "attended"}]
                + [{"cohort_session_id": f"s{i}", "user_id": "c", "status": "attended"} for i in range(3)]),
            "enrolment_state": [{"student_id": "a", "pace_pledge_hours": 5},
                                {"student_id": "c", "pace_pledge_hours": 20}],
            "pod_checkpoint": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    advanced = []
    with patch("app.services.checkpoint_service.db", fake), \
         patch("app.services.checkpoint_service._now", return_value=NOW), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: i.upper() for i in ids if i}), \
         patch("app.services.intake_service._current_lesson_map", side_effect=lambda ids: {"a": 4, "b": 4, "c": 8}), \
         patch("app.services.phase0_service.advance", side_effect=lambda s, f, t: advanced.append((s, f, t))), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == OPS and c == "ops"):
        fake.advanced = advanced
        yield fake


# 1
def test_month_of_and_pagume():
    assert cs.month_of(date(2027, 1, 20)) == (2019, 5, TIR_1, TAHSAS_1)
    assert cs.month_of(date(2026, 9, 7)) is None            # Pagume 2, 2018
    y, m, due, start = cs.month_of(date(2026, 9, 11))        # 1 Meskerem 2019
    assert (y, m, due, start) == (2019, 1, date(2026, 9, 11), date(2026, 8, 7))  # from 1 Nehase


def test_opens_one_per_live_pod_per_month(db):
    assert cs.open_due(date(2027, 1, 10)) == 1
    assert [r["cohort_id"] for r in db.rows["pod_checkpoint"]] == ["pod1"]
    assert cs.open_due(date(2027, 1, 11)) == 0               # already open
    assert cs.open_due(date(2027, 1, 8)) == 0                # pod1 started on 1 Tahsas: none that month
    assert cs.open_due(date(2027, 2, 8)) == 2                # 1 Yekatit: pod1 and pod-new


# 2
def test_snapshot_reads_marked_attendance_only(db):
    cs.open_due(TIR_1)
    snap = db.rows["pod_checkpoint"][0]["snapshot"]
    assert snap["sessions"] == {"scheduled": 3, "held": 3, "cancelled": 0}
    assert {r["tutor_id"]: r["sessions"] for r in snap["rota"]} == {"home": 2, "guest": 1}
    by = {m["student_id"]: m for m in snap["members"]}
    assert by["b"]["flags"] == ["low_attendance"] and by["b"]["attended"] == 1
    assert by["c"]["flags"] == ["ahead_of_band"] and by["c"]["pace_pledge_hours"] == 20
    assert by["a"]["flags"] == []
    assert snap["open_seats"] == 1 and snap["lesson_spread"] == 4
    assert not db.read & {"activity_summary", "activity_event", "ai_usage"}


# 3
def test_review_is_ops_only_and_final(db):
    cs.open_due(TIR_1)
    cp = db.rows["pod_checkpoint"][0]["id"]
    with pytest.raises(CheckpointRefused) as e:
        cs.review(NOBODY, cp, "continue", True)
    assert e.value.reason == "not_allowed"
    with pytest.raises(CheckpointRefused) as e:
        cs.review(OPS, cp, "continue", True, ["c"])
    assert e.value.reason == "move_needs_adjust"
    with pytest.raises(CheckpointRefused) as e:
        cs.review(OPS, cp, "adjust", True, ["stranger"])
    assert e.value.reason == "not_a_member"
    cs.review(OPS, cp, "continue", True)
    with pytest.raises(CheckpointRefused) as e:
        cs.review(OPS, cp, "pause", True)
    assert e.value.reason == "reviewed"


def test_pace_move_returns_member_to_wait_pool(db):
    cs.open_due(TIR_1)
    cp = db.rows["pod_checkpoint"][0]["id"]
    out = cs.review(OPS, cp, "adjust", True, ["c"])
    assert out["moved_student_ids"] == ["c"] and out["status"] == "reviewed"
    status = {m["user_id"]: m["status"] for m in db.rows["cohort_memberships"]}
    assert status == {"a": "active", "b": "active", "c": "transferred"}
    assert db.advanced == [("c", ("in_pod",), "wait_pool")]
    assert db.rows["cohorts"][0]["status"] == "active"


def test_dissolve_drops_everyone(db):
    cs.open_due(TIR_1)
    cs.review(OPS, db.rows["pod_checkpoint"][0]["id"], "dissolve", False)
    assert {m["status"] for m in db.rows["cohort_memberships"]} == {"dropped"}
    assert db.rows["cohorts"][0]["status"] == "dissolved"
    assert len(db.advanced) == 3
