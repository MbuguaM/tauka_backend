"""The admin make-up screen's read (enrolment plan §2f item 1).

Pinned: each proposed make-up comes with learner names, the proposed tutor's
name, and the tutors an override may pick — only those in EVERY learner's own
pool, the same rule `schedule()` enforces. Withdrawn seats don't count.
"""
from types import SimpleNamespace
from unittest.mock import patch

from app.services import makeup_service


class _Q:
    def __init__(self, tables, name):
        self.tables, self.name, self.f = tables, name, []

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self.tables.get(self.name, []) if all(f(r) for f in self.f)])


class _DB:
    def __init__(self, tables): self.tables = tables
    def schema(self, _): return self
    def table(self, n): return _Q(self.tables, n)


TABLES = {
    "makeup_session": [{"id": "m1", "course_id": "c", "due_by": "2027-02-01T00:00:00+00:00", "duration_minutes": 20,
                        "proposed_tutor_id": "t1", "proposed_at": "2027-01-20T18:00:00+00:00",
                        "proposal_note": None, "created_at": "x", "status": "proposed"}],
    "makeup_session_participant": [
        {"makeup_session_id": "m1", "student_id": "s1", "funded_by": "month", "status": "booked"},
        {"makeup_session_id": "m1", "student_id": "s2", "funded_by": "month", "status": "booked"},
        {"makeup_session_id": "m1", "student_id": "s3", "funded_by": "month", "status": "withdrawn"},
    ],
    "student_tutor_link": [
        {"student_id": "s1", "tutor_id": "t1", "role": "home", "state": "active"},
        {"student_id": "s1", "tutor_id": "t2", "role": "guest", "state": "active"},
        {"student_id": "s2", "tutor_id": "t1", "role": "guest", "state": "active"},
        {"student_id": "s2", "tutor_id": "t3", "role": "home", "state": "active"},
        {"student_id": "s3", "tutor_id": "t9", "role": "home", "state": "active"},
    ],
    "user_profiles": [
        {"id": "s1", "first_name": "Selam", "last_name": None},
        {"id": "s2", "first_name": "Dawit", "last_name": "A"},
        {"id": "t1", "first_name": "Marta", "last_name": None},
    ],
}


def test_proposed_sessions_carry_names_and_the_common_pool():
    with patch("app.services.makeup_service.db", _DB(TABLES)):
        rows = makeup_service.proposed_sessions()
    r = rows[0]
    assert [x["name"] for x in r["learners"]] == ["Selam", "Dawit A"]      # withdrawn s3 left out
    assert r["proposed_tutor_name"] == "Marta"
    assert r["pool_tutors"] == [{"tutor_id": "t1", "name": "Marta"}]      # t2/t3 aren't shared; t9 is s3's


def test_no_proposed_sessions_is_an_empty_list():
    with patch("app.services.makeup_service.db", _DB({"makeup_session": []})):
        assert makeup_service.proposed_sessions() == []
