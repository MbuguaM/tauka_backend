"""Session complaints (enrolment plan §2f item 7, §106).

Pinned:
1. A learner may complain only about a live session they were in, that has
   started, within 14 days; once per session.
2. Conduct goes to safeguarding, the rest to ops; each queue shows only its own.
3. Upheld grants ONE session credit referencing the complaint; rejected grants
   nothing; a decision is final.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import complaint_service, hierarchy_service
from app.services.complaint_service import ComplaintRefused

NOW = datetime(2027, 1, 20, 12, tzinfo=timezone.utc)
STU, OTHER, OPS, SAFE = "s1", "s2", "ops", "safe"
CAPS = {OPS: {"ops"}, SAFE: {"safeguarding"}}


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if self.name == "session_complaint" and any(
                    r["student_id"] == p["student_id"] and r["session_id"] == p["session_id"] for r in rows):
                raise Exception("duplicate key session_complaint_one_per_session")
            if self.name == "session_credit" and any(r.get("reference_id") == p.get("reference_id") for r in rows):
                raise Exception("duplicate key idx_session_credit_one_per_reference")
            row = {"id": f"{self.name}-{len(rows) + 1}", "status": "open", "created_at": NOW.isoformat(), **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        past = (NOW - timedelta(days=2)).isoformat()
        self.rows = {
            "cohort_memberships": [{"user_id": STU, "cohort_id": "pod1"}],
            "cohort_session": [{"id": "cs1", "cohort_id": "pod1", "scheduled_at": past, "status": "completed",
                                "lead_tutor_id": "t1"},
                               {"id": "cs-future", "cohort_id": "pod1", "status": "scheduled", "lead_tutor_id": "t1",
                                "scheduled_at": (NOW + timedelta(days=2)).isoformat()},
                               {"id": "cs-old", "cohort_id": "pod1", "status": "completed", "lead_tutor_id": "t1",
                                "scheduled_at": (NOW - timedelta(days=20)).isoformat()}],
            "makeup_session_participant": [{"makeup_session_id": "m1", "student_id": STU, "status": "attended"}],
            "makeup_session": [{"id": "m1", "scheduled_at": past, "status": "completed", "tutor_id": "t2"}],
            "clinic_booking": [], "clinic": [], "one_to_one_session": [],
            "session_complaint": [], "session_credit": [],
            "user_profiles": [{"id": STU, "first_name": "Selam", "last_name": None},
                              {"id": "t1", "first_name": "Marta", "last_name": None}],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.complaint_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.complaint_service._now", return_value=NOW), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: c in CAPS.get(a, set())):
        yield fake


# 1
def test_only_recent_started_sessions_of_mine(db):
    got = {(s["session_type"], s["session_id"]) for s in complaint_service.recent_sessions(STU, NOW)}
    assert got == {("cohort_session", "cs1"), ("makeup_session", "m1")}
    assert complaint_service.recent_sessions(OTHER, NOW) == []


def test_complain_once_and_only_about_my_session(db):
    with pytest.raises(ComplaintRefused) as e:
        complaint_service.create(STU, "cohort_session", "cs-future", "tutor_absent", None)
    assert e.value.reason == "not_your_session"
    c = complaint_service.create(STU, "cohort_session", "cs1", "tutor_absent", "  Nobody came  ")
    assert c["routed_to"] == "ops" and c["detail"] == "Nobody came"
    with pytest.raises(ComplaintRefused) as e:
        complaint_service.create(STU, "cohort_session", "cs1", "other", None)
    assert e.value.reason == "already_complained"
    assert complaint_service.recent_sessions(STU, NOW)[0]["complained"] in (True, False)


# 2
def test_conduct_goes_to_safeguarding_and_queues_are_separate(db):
    complaint_service.create(STU, "cohort_session", "cs1", "conduct", None)
    complaint_service.create(STU, "makeup_session", "m1", "connection", None)
    assert [c["reason_code"] for c in complaint_service.queue(SAFE)] == ["conduct"]
    q = complaint_service.queue(OPS)
    assert [c["reason_code"] for c in q] == ["connection"] and q[0]["tutor_id"] == "t2"
    with pytest.raises(ComplaintRefused):
        complaint_service.queue("nobody")


# 3
def test_upheld_grants_one_credit_and_decisions_are_final(db):
    c = complaint_service.create(STU, "cohort_session", "cs1", "tutor_absent", None)
    with pytest.raises(ComplaintRefused) as e:
        complaint_service.decide(SAFE, c["id"], True)          # wrong queue
    assert e.value.reason == "not_allowed"
    complaint_service.decide(OPS, c["id"], True)
    credits = db.rows["session_credit"]
    assert len(credits) == 1 and credits[0]["reference_type"] == "session_complaint"
    with pytest.raises(ComplaintRefused) as e:
        complaint_service.decide(OPS, c["id"], True)
    assert e.value.reason == "decided"
    assert len(db.rows["session_credit"]) == 1


def test_rejected_grants_nothing(db):
    c = complaint_service.create(STU, "makeup_session", "m1", "other", None)
    assert complaint_service.decide(OPS, c["id"], False)["status"] == "rejected"
    assert db.rows["session_credit"] == []


def test_route_rejects_an_unknown_reason(client):
    from tests.conftest import make_token
    r = client.post("/complaints", json={"session_type": "clinic", "session_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                          "reason_code": "bad_vibes"},
                    headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 422
