"""Forming a private pod in software (enrolment plan 3.2, §128).

Pinned:
1. Ops only; a private-pod group, open, 2+ members, all ready, one level, one
   cadence; an active tutor and an active catalogue slot.
2. Forming writes a `private` cycle (cohorts_formed 1), one `forming` pod sized
   to the group at the slot (anchor zone), an offer per member; enrolment ->
   matched; the group -> matched.
3. Activation: a pod sized under cohort_min goes live when it is full.
4. Close: a private pod still forming when every offer is resolved is
   dissolved and its seated members go back to the wait pool.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import group_service as gs
from app.services import hierarchy_service, refill_service
from app.services.group_service import GroupRefused

NOW = datetime(2027, 1, 20, 9, tzinfo=timezone.utc)


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
            row = {"id": f"{self.name}-{len(rows) + 1}", **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        ids = ("a", "b")
        self.rows = {
            "enrolment_group": [{"id": "g1", "kind": "private_pod", "status": "open", "code": "ABCDEFGH", "max_size": 4}],
            "enrolment_group_member": [{"group_id": "g1", "student_id": i, "left_at": None} for i in ids],
            "student": [{"id": i, "assessed_level": "A1", "tier": "tutor_tier"} for i in ids],
            "enrolment_state": [{"student_id": i, "state": "seat_confirmed"} for i in ids],
            "tutor": [{"id": "t1", "status": "active", "deleted_at": None}, {"id": "t0", "status": "onboarding"}],
            "slot_catalogue": [{"id": "s1", "band": "europe", "anchor_timezone": "Europe/London", "day_of_week": 5,
                                "start_time": "10:00:00", "active": True}],
            "intake_cycle": [], "cohorts": [], "match_offer": [], "cohort_memberships": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    moved = []
    cfg = SimpleNamespace(offer_ttl_hours=24, cohort_min=3, as_dict=lambda: {})
    with patch("app.services.group_service.db", fake), \
         patch("app.services.refill_service.db", fake), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: i.title() for i in ids}), \
         patch("app.services.intake_service.load_config", return_value=cfg), \
         patch("app.services.intake_service._course_for", return_value="c1"), \
         patch("app.services.phase0_service.advance", side_effect=lambda sid, frm, to: moved.append((sid, to))), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"):
        fake.moved = moved
        yield fake


# 1
@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    (lambda d: None, None),
    (lambda d: d.rows["enrolment_group"][0].update(kind="friends"), "not_formable"),
    (lambda d: d.rows["enrolment_group_member"].pop(), "too_few"),
    (lambda d: d.rows["enrolment_state"][1].update(state="phase0"), "not_ready"),
    (lambda d: d.rows["student"][1].update(assessed_level="B1"), "level_gap"),
    (lambda d: d.rows["student"][1].update(tier="learner_tier"), "mixed_plans"),
])
async def test_who_can_be_formed(db, change, reason):
    change(db)
    if reason is None:
        assert (await gs.form_private_pod("ops", "g1", "t1", "s1", NOW))["offered"] == 2
        return
    with pytest.raises(GroupRefused) as e:
        await gs.form_private_pod("ops", "g1", "t1", "s1", NOW)
    assert e.value.reason == reason


@pytest.mark.asyncio
async def test_ops_tutor_and_slot_checks(db):
    with pytest.raises(GroupRefused):
        await gs.form_private_pod("someone", "g1", "t1", "s1", NOW)
    with pytest.raises(GroupRefused) as e:
        await gs.form_private_pod("ops", "g1", "t0", "s1", NOW)
    assert e.value.reason == "tutor_inactive"
    with pytest.raises(GroupRefused) as e:
        await gs.form_private_pod("ops", "g1", "t1", "nope", NOW)
    assert e.value.reason == "no_such_slot"


# 2
@pytest.mark.asyncio
async def test_forming_writes_pod_and_offers(db):
    out = await gs.form_private_pod("ops", "g1", "t1", "s1", NOW)
    cycle = db.rows["intake_cycle"][0]
    assert (cycle["mode"], cycle["cohorts_formed"], cycle["status"]) == ("private", 1, "offered")
    pod = db.rows["cohorts"][0]
    assert (pod["status"], pod["max_students"], pod["slot_day_of_week"], pod["slot_time"], pod["slot_timezone"],
            pod["cadence_class"], pod["level"]) == ("forming", 2, 5, "10:00:00", "Europe/London", "B", 1)
    assert {(o["student_id"], o["cohort_id"], o["status"]) for o in db.rows["match_offer"]} == {
        ("a", out["cohort_id"], "offered"), ("b", out["cohort_id"], "offered")}
    assert sorted(db.moved) == [("a", "matched"), ("b", "matched")]
    assert db.rows["enrolment_group"][0]["status"] == "matched"
    with pytest.raises(GroupRefused):
        await gs.form_private_pod("ops", "g1", "t1", "s1", NOW)                   # once


# 3
@pytest.mark.asyncio
async def test_small_pod_goes_live_when_full():
    from app.services import offer_service
    fake = SimpleNamespace(rows={"cohorts": [{"id": "p1", "status": "forming", "max_students": 2}],
                                 "cohort_memberships": [{"cohort_id": "p1", "status": "active"}] * 2})
    fake.schema = lambda _: fake
    fake.table = lambda n: _Q(fake, n)
    calls = []
    fake.rpc = lambda name, args: SimpleNamespace(execute=lambda: calls.append(args))
    with patch("app.services.offer_service.db", fake):
        assert await offer_service._activate_if_viable("p1", SimpleNamespace(cohort_min=3)) is True
    assert fake.rows["cohorts"][0]["status"] == "active" and calls == [{"p_cohort": "p1", "p_months": 2}]


# 4
@pytest.mark.asyncio
async def test_close_dissolves_an_unfilled_private_pod(db):
    out = await gs.form_private_pod("ops", "g1", "t1", "s1", NOW)
    db.rows["match_offer"][0]["status"] = "accepted"
    db.rows["match_offer"][1]["status"] = "declined"
    db.rows["cohort_memberships"].append({"cohort_id": out["cohort_id"], "user_id": "a", "status": "active"})
    db.moved.clear()
    assert refill_service.close_resolved() == 1
    assert db.rows["cohorts"][0]["status"] == "dissolved"
    assert db.rows["cohort_memberships"][0]["status"] == "dropped"
    assert sorted(db.moved) == [("a", "wait_pool"), ("b", "wait_pool")]
    assert db.rows["intake_cycle"][0]["status"] == "closed"
