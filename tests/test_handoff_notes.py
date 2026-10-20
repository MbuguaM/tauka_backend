"""Handoff notes (enrolment plan 4.3, E-09, §119).

Pinned (developer decisions 2026-10-18):
1. Readers: the learner, their linked tutors (active or handoff_pending — the
   warm handoff), a tutor leading their pod in the next 14 days, ops. Nobody else.
2. Only the home tutor (or ops) writes; two texts <= 280 and a confidence choice.
3. The home tutor is prompted for members of a pod with a guest-led session
   in the next 7 days whose note is missing or older than 30 days.
4. The guest tutor sees their upcoming guest sessions with every member's note.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import handoff_note_service as hn
from app.services import hierarchy_service
from app.services.handoff_note_service import HandoffNoteRefused

NOW = datetime(2027, 2, 1, 12, tzinfo=timezone.utc)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            rows.append(dict(p))
            return SimpleNamespace(data=[p])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        at = lambda days: (NOW + timedelta(days=days)).isoformat()  # noqa: E731
        self.rows = {
            "student_tutor_link": [
                {"student_id": "a", "tutor_id": "home", "role": "home", "state": "active"},
                {"student_id": "a", "tutor_id": "old", "role": "home", "state": "handoff_pending"},
                {"student_id": "a", "tutor_id": "gone", "role": "guest", "state": "ended"},
            ],
            "cohorts": [{"id": "pod1", "name": "Tir A", "tutor_id": "home", "status": "active"}],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": u, "status": "active"} for u in ("a", "b")],
            "cohort_session": [
                {"id": "s-g", "cohort_id": "pod1", "scheduled_at": at(5), "status": "scheduled",
                 "lead_tutor_id": "guest", "lead_role": "guest"},
                {"id": "s-far", "cohort_id": "pod1", "scheduled_at": at(20), "status": "scheduled",
                 "lead_tutor_id": "far", "lead_role": "guest"},
            ],
            "handoff_note": [{"student_id": "b", "recurring_errors": "x", "goals": None, "confidence": "building",
                              "updated_at": (NOW - timedelta(days=40)).isoformat()}],
            "user_profiles": [{"id": "a", "first_name": "Selam", "last_name": None}],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.handoff_note_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.handoff_note_service._now", return_value=NOW), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        yield fake


# 1
@pytest.mark.parametrize("who,ok", [("a", True), ("home", True), ("old", True), ("guest", True), ("ops", True),
                                    ("gone", False), ("far", False), ("b", False), ("stranger", False)])
def test_who_reads(db, who, ok):
    assert hn.can_read(who, "a", NOW) is ok


# 2
def test_only_home_tutor_writes(db):
    for who in ("guest", "a"):
        with pytest.raises(HandoffNoteRefused) as e:
            hn.save(who, "a", "x", "y", "building")
        assert e.value.reason == "not_home_tutor"
    with pytest.raises(HandoffNoteRefused):
        hn.save("home", "a", "x" * 281, None, "building")
    with pytest.raises(HandoffNoteRefused):
        hn.save("home", "a", None, None, "great")
    n = hn.save("home", "a", "  ejectives  ", "order at a café", "hesitant")
    assert (n["recurring_errors"], n["goals"], n["confidence"]) == ("ejectives", "order at a café", "hesitant")
    assert hn.save("old", "a", None, None, "building")["confidence"] == "building"   # pending home tutor
    assert hn.get("a", "a")["written_by"] == "old"                                   # the learner reads it


# 3
def test_prompt_before_a_guest_session(db):
    got = {(r["student_id"], r["reason"]) for r in hn.to_write("home", NOW)}
    assert got == {("a", "missing"), ("b", "stale")}
    hn.save("home", "a", "x", "y", "building")
    db.rows["handoff_note"][0]["updated_at"] = NOW.isoformat()
    assert hn.to_write("home", NOW) == []
    assert hn.to_write("home", NOW - timedelta(days=10)) == []    # the guest session was 15 days out


# 4
def test_guest_sees_the_pod_notes(db):
    s = hn.guest_sessions("guest", NOW)
    assert [x["session_id"] for x in s] == ["s-g"]
    assert {(l["student_id"], (l["note"] or {}).get("confidence")) for l in s[0]["learners"]} == {
        ("a", None), ("b", "building")}
    assert hn.guest_sessions("far", NOW) == []                     # 20 days out
