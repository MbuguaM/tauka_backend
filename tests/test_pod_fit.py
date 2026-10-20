"""First-session norms and the two-week fit survey (enrolment plan 4.1, §118).

Pinned (developer decisions 2026-10-18):
1. Norms are fixed choices, set by a tutor who teaches the pod (or ops); a
   tutor sees which of their pods still need them.
2. The survey is due after two sessions HELD since the learner joined, within
   30 days, once; four fixed answers.
3. A mismatch is flagged for ops only; "moved" takes the learner to the wait
   pool, "none" leaves them; a review happens once.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import hierarchy_service
from app.services import pod_fit_service as pf
from app.services.pod_fit_service import PodFitRefused

NOW = datetime(2027, 1, 25, 12, tzinfo=timezone.utc)
JOINED = (NOW - timedelta(days=14)).isoformat()
OK = {"level_fit": "right", "time_fit": "works", "group_fit": "comfortable", "staying": "stay"}


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            row = dict(p)
            if self.name == "pod_fit_survey":
                row.update({"mismatch": pf.is_mismatch(p), "action": None, "created_at": NOW.isoformat()})
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        def s(days_ago, status="completed"):
            return {"cohort_id": "pod1", "scheduled_at": (NOW - timedelta(days=days_ago)).isoformat(),
                    "status": status, "lead_tutor_id": "home"}
        self.rows = {
            "cohorts": [{"id": "pod1", "name": "Tir A", "tutor_id": "home", "status": "active", "level": 1},
                        {"id": "pod2", "name": "Tir B", "tutor_id": "home", "status": "active", "level": 1}],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": u, "status": "active", "joined_at": JOINED}
                                   for u in ("a", "b")],
            # Two held since joining, one held before it, one upcoming.
            "cohort_session": [s(20), s(10), s(3), s(-4, "scheduled")],
            "pod_norms": [], "pod_fit_survey": [], "user_profiles": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    left = []
    with patch("app.services.pod_fit_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.pod_fit_service._now", return_value=NOW), \
         patch("app.services.checkpoint_service._leave", side_effect=lambda *a: left.append(a)), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        fake.left = left
        yield fake


# 1
def test_norms(db):
    assert [p["cohort_id"] for p in pf.pending_for_tutor("home")] == ["pod1", "pod2"]
    with pytest.raises(PodFitRefused) as e:
        pf.set_norms("stranger", "pod1", "on", "door_5", "amharic_first")
    assert e.value.reason == "not_your_pod"
    with pytest.raises(PodFitRefused):
        pf.set_norms("home", "pod1", "off", "door_5", "amharic_first")
    n = pf.set_norms("home", "pod1", "on", "door_5", "amharic_first")
    assert (n["camera"], n["lateness"], n["language"]) == ("on", "door_5", "amharic_first")
    pf.set_norms("home", "pod1", "optional", "door_5", "amharic_first")        # changing them later
    assert pf.for_member("a")[0]["camera"] == "optional"
    assert [p["cohort_id"] for p in pf.pending_for_tutor("home")] == ["pod2"]


# 2
def test_survey_due_after_two_sessions_since_joining(db):
    assert pf.due("a") == [{"cohort_id": "pod1", "name": "Tir A"}]
    db.rows["cohort_session"][1]["status"] = "cancelled"                         # only one held since joining
    assert pf.due("a") == []
    db.rows["cohort_session"][1]["status"] = "completed"
    db.rows["cohort_memberships"][0]["joined_at"] = (NOW - timedelta(days=31)).isoformat()
    assert pf.due("a") == []                                                     # past the 30 days


def test_submit_once_with_fixed_answers(db):
    with pytest.raises(PodFitRefused) as e:
        pf.submit("a", "pod1", {**OK, "level_fit": "meh"})
    assert e.value.reason == "bad_choice"
    out = pf.submit("a", "pod1", OK)
    assert out["mismatch"] is False
    with pytest.raises(PodFitRefused) as e:
        pf.submit("a", "pod1", OK)
    assert e.value.reason == "not_due"
    with pytest.raises(PodFitRefused):
        pf.submit("stranger", "pod1", OK)


# 3
def test_mismatch_goes_to_ops_who_decide(db):
    pf.submit("a", "pod1", OK)
    pf.submit("b", "pod1", {**OK, "group_fit": "not_comfortable"})
    with pytest.raises(PodFitRefused):
        pf.queue("home")                                                         # never the tutor
    q = pf.queue("ops")
    assert [r["student_id"] for r in q] == ["b"]
    pf.review("ops", "pod1", "b", "moved")
    assert db.left and db.left[0][:3] == ("pod1", ["b"], "transferred")
    assert pf.queue("ops") == []
    with pytest.raises(PodFitRefused) as e:
        pf.review("ops", "pod1", "b", "none")
    assert e.value.reason == "reviewed"


@pytest.mark.parametrize("answers,flag", [
    (OK, False), ({**OK, "level_fit": "too_hard"}, True), ({**OK, "time_fit": "sometimes"}, False),
    ({**OK, "time_fit": "doesnt_work"}, True), ({**OK, "group_fit": "mostly"}, False),
    ({**OK, "staying": "not_sure"}, False), ({**OK, "staying": "move"}, True)])
def test_mismatch_rule(answers, flag):
    assert pf.is_mismatch(answers) is flag
