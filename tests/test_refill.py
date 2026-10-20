"""Seat refills (enrolment plan 3.6, §112).

Pinned:
1. Refills happen in the first 14 days after a formation day, never in Pagume.
2. A learner fits a pod only at its level, with a chosen slot at the pod's
   time, and without stretching its lesson spread past `lesson_band`.
3. Wait pool first, then the longest waiting, then a favourite slot; at most
   open seats + overbook slack per pod (minus offers already out), one each.
4. The run writes refill offers under one refill cycle a day and moves the
   learner to `matched`; a refill cycle closes once its offers resolve, and
   whoever didn't take the seat goes back to the wait pool.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import refill_service as rs
from app.services.cohort_matcher import MatchConfig

NOW = datetime(2027, 1, 12, 9, tzinfo=timezone.utc)   # 4 Tir 2019
CFG = MatchConfig()
SLOT = {"id": "sl-fri", "anchor_timezone": "Africa/Nairobi", "day_of_week": 4, "start_time": "18:00"}
OTHER = {"id": "sl-sat", "anchor_timezone": "Africa/Nairobi", "day_of_week": 5, "start_time": "18:00"}
SLOTS = {s["id"]: s for s in (SLOT, OTHER)}


def pod(pid="pod1", level=1, seats=1, lessons=(4, 5, 6)):
    return {"id": pid, "tutor_id": "t1", "level": level, "slot_timezone": "Africa/Nairobi",
            "slot_day_of_week": 4, "slot_time": "18:00", "open_seats": seats, "lessons": list(lessons)}


def person(sid, state="wait_pool", level=1, lesson=5, slot="sl-fri", fav=False, waited=0):
    return {"student_id": sid, "state": state, "level": level, "lesson": lesson, "waited": waited,
            "choices": [{"slot_id": slot, "favourite": fav}]}


# 1
def test_window():
    assert rs.in_window(date(2027, 1, 9))        # 1 Tir
    assert rs.in_window(date(2027, 1, 22))       # 14 Tir
    assert not rs.in_window(date(2027, 1, 23))   # 15 Tir
    assert not rs.in_window(date(2026, 9, 7))    # Pagume


# 2
def test_fit_rules():
    people = [person("lvl", level=2), person("slot", slot="sl-sat"), person("far", lesson=11),
              person("ok", lesson=8)]
    picks = rs.plan([pod(seats=2)], people, SLOTS, CFG, NOW, {})
    assert [sid for sid, _ in picks] == ["ok"]   # lesson 8 keeps the spread at 4 <= 5; 11 makes it 7


# 3
def test_order_and_room():
    people = [person("new", state="seat_confirmed", waited=0), person("w1", waited=1),
              person("w2", waited=2), person("w0fav", fav=True), person("w0")]
    # 1 seat + overbook slack 2 = 3 offers.
    picks = [sid for sid, _ in rs.plan([pod(seats=1)], people, SLOTS, CFG, NOW, {})]
    assert picks == ["w2", "w1", "w0fav"]
    # Two offers already out on this pod: one more.
    picks = [sid for sid, _ in rs.plan([pod(seats=1)], people, SLOTS, CFG, NOW, {"pod1": 2})]
    assert picks == ["w2"]


def test_one_offer_per_learner_across_pods():
    picks = rs.plan([pod("a"), pod("b")], [person("x")], SLOTS, CFG, NOW, {})
    assert len(picks) == 1


# 4
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
        self.rows = {"intake_cycle": [], "match_offer": []}

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    moves = []
    with patch("app.services.refill_service.db", fake), \
         patch("app.services.phase0_service.advance", side_effect=lambda s, f, t: moves.append((s, tuple(f), t))), \
         patch("app.services.intake_service.load_config", return_value=CFG), \
         patch("app.services.slot_service.catalogue", return_value=[SLOT, OTHER]), \
         patch("app.services.refill_service.open_pods", return_value=[pod()]), \
         patch("app.services.refill_service._open_offer_counts", return_value=({}, set())), \
         patch("app.services.refill_service.candidates", return_value=[person("w")]):
        fake.moves = moves
        yield fake


def test_run_writes_refill_offers(db):
    out = rs.run(NOW)
    assert out["offers"] == 1
    cyc = db.rows["intake_cycle"][0]
    assert cyc["mode"] == "refill" and cyc["status"] == "offered" and cyc["cycle_date"] == "2027-01-12"
    o = db.rows["match_offer"][0]
    assert (o["student_id"], o["cohort_id"], o["rank"], o["priority"]) == ("w", "pod1", 1, True)
    assert o["score_breakdown"]["kind"] == "refill"
    assert db.moves == [("w", ("seat_confirmed", "wait_pool"), "matched")]
    # A second run the same day reuses the cycle.
    rs.run(NOW)
    assert len(db.rows["intake_cycle"]) == 1


def test_outside_window_offers_nothing(db):
    out = rs.run(datetime(2027, 1, 30, tzinfo=timezone.utc))
    assert out == {"closed": 0, "offers": 0, "in_window": False}


def test_close_when_resolved(db):
    db.rows["intake_cycle"] = [{"id": "c1", "mode": "refill", "status": "offered"}]
    db.rows["match_offer"] = [{"cycle_id": "c1", "student_id": "a", "status": "offered"},
                              {"cycle_id": "c1", "student_id": "b", "status": "declined"}]
    assert rs.close_resolved() == 0
    db.rows["match_offer"][0]["status"] = "accepted"
    assert rs.close_resolved() == 1
    assert db.rows["intake_cycle"][0]["status"] == "closed"
    assert db.moves == [("b", ("matched",), "wait_pool")]
