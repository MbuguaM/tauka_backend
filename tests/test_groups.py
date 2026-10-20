"""Friend groups and private pods — the service (enrolment plan 3.2, §108).

Pinned: create gives an 8-character code from the unambiguous alphabet; one
group per learner; join by code; a full or closed group refuses; leaving the
last member dissolves it; a learner sees first names and readiness, never ids or
levels; ops sees the level gap; the matcher gets friend groups and leaves
private-pod members out.
"""
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import group_service, hierarchy_service
from app.services.group_service import GroupRefused

A, B, C, D = "a", "b", "c", "d"
COURSE = "c1"


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
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
            if self.name == "enrolment_group_member":
                g = next(g for g in self.db.rows["enrolment_group"] if g["id"] == p["group_id"])
                if g["status"] != "open":
                    raise Exception("enrolment_group: not open")
                n = sum(1 for m in rows if m["group_id"] == p["group_id"] and m.get("left_at") is None)
                if n >= g["max_size"]:
                    raise Exception("enrolment_group: full")
            row = {"id": f"g{len(rows) + 1}", "status": "open", "left_at": None, **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.rows = {
            "student": [{"id": A, "assessed_level": "A1"}, {"id": B, "assessed_level": "A1"},
                        {"id": C, "assessed_level": "A2"}, {"id": D, "assessed_level": "A1"}],
            "enrolment_state": [{"student_id": A, "state": "seat_confirmed"}, {"student_id": B, "state": "paid"}],
            "user_profiles": [{"id": A, "first_name": "Selam", "last_name": "T"},
                              {"id": B, "first_name": "Dawit", "last_name": None}],
            "enrolment_group": [], "enrolment_group_member": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.group_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"):
        yield fake


def test_create_join_and_what_a_learner_sees(db):
    g = group_service.create(A, "friends", COURSE)["group"]
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", g["code"]) and g["max_size"] == 3
    with pytest.raises(GroupRefused) as e:
        group_service.create(A, "friends", COURSE)
    assert e.value.reason == "already_in_group"
    out = group_service.join(B, g["code"].lower())["group"]       # codes are case-insensitive
    assert [m["first_name"] for m in out["members"]] == ["Selam", "Dawit"]
    assert out["members"][1] == {"first_name": "Dawit", "ready": False, "is_you": True}
    assert "level" not in out["members"][0] and "student_id" not in out["members"][0]
    assert out["all_ready"] is False


def test_full_unknown_and_level_gap(db):
    g = group_service.create(A, "friends", COURSE)["group"]
    group_service.join(B, g["code"])
    group_service.join(C, g["code"])
    with pytest.raises(GroupRefused) as e:
        group_service.join(D, g["code"])
    assert e.value.reason == "full"
    with pytest.raises(GroupRefused) as e:
        group_service.join(D, "ZZZZZZZZ")
    assert e.value.reason == "no_such_group"
    ops = group_service.admin_list("ops")
    assert ops[0]["level_gap"] is True          # A1, A1, A2
    with pytest.raises(GroupRefused):
        group_service.admin_list(A)


def test_leaving_last_dissolves(db):
    group_service.create(A, "friends", COURSE)
    group_service.leave(A)
    assert db.rows["enrolment_group"][0]["status"] == "dissolved"
    with pytest.raises(GroupRefused):
        group_service.leave(A)


def test_pool_groups_for_the_matcher(db):
    f = group_service.create(A, "friends", COURSE)["group"]
    group_service.join(B, f["code"])
    p = group_service.create(C, "private_pod", COURSE)["group"]
    group_service.join(D, p["code"])
    friends, private = group_service.pool_groups([A, B, C, D])
    assert friends[A][1] == 2 and friends[A][0] == friends[B][0]
    assert private == {C, D}


def test_a_group_of_one_is_matched_as_a_single(db):
    group_service.create(A, "friends", COURSE)
    friends, _ = group_service.pool_groups([A])
    assert friends == {}
