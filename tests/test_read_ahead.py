"""Read-ahead and the upgrade offer (enrolment plan 2.2, §2c; §91's read_ahead source).

Pinned:
1. Only a live paid plan (or gift), not in a payment grace, with a paid month.
2. The read-ahead lesson is the unit at the cursor (preliminary first); a free
   next lesson isn't offered.
3. One per paid month: a second request changes nothing.
4. The read-ahead slot is EARLY, not extra: next month's grant starts at the
   same cursor.
5. Completed (unit_progress) -> an upgrade offer: Learner -> Tutor, Tutor ->
   Intensive; Intensive gets none.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import lesson_service as ls
from app.services.lesson_service import ReadAheadRefused


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f = db, name, []

    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        return SimpleNamespace(data=[dict(r) for r in rows if all(f(r) for f in self.f)])


class _RPC:
    def __init__(self, db, args): self.db, self.args = db, args

    def execute(self):
        """app.grant_lesson_slots: read_ahead = 1 slot; the cursor ignores read_ahead; idempotent on ref."""
        a, rows = self.args, self.db.rows["lesson_entitlement"]
        same = [r for r in rows if (r["student_id"], r["source"], r["grant_ref"]) ==
                (a["p_student_id"], a["p_source"], a["p_grant_ref"])]
        if same:
            return SimpleNamespace(data=same[0])
        cur = max([r["slot_to"] for r in rows if r["student_id"] == a["p_student_id"] and r["source"] != "read_ahead"],
                  default=0)
        n = 1 if a["p_source"] == "read_ahead" else a["p_count"]
        row = {"student_id": a["p_student_id"], "slot_from": cur, "slot_to": cur + n, "source": a["p_source"],
               "grant_ref": a["p_grant_ref"], "available_from": "2026-01-01T00:00:00+00:00",
               "created_at": f"2026-01-{len(rows) + 1:02d}", "revoked_at": None, "id": f"e{len(rows)}"}
        rows.append(row)
        return SimpleNamespace(data=row)


class _DB:
    def __init__(self, tier="tutor_tier", status="active"):
        self.rows = {
            "student": [{"id": "s1", "tier": tier, "subscription_status": status, "active_gift_id": None}],
            "lesson_entitlement": [],
            "unit": [
                {"id": "intro", "unit_order": 1, "is_preliminary": True, "title": "Introduction"},
                {"id": "alpha", "unit_order": 2, "is_preliminary": True, "title": "Alphabet"},
                *[{"id": f"u{i}", "unit_order": i, "is_preliminary": False, "title": f"Unit {i}"} for i in range(1, 8)],
            ],
            "unit_progress": [],
            "payment_grace": [],
        }
        for u in self.rows["unit"]:
            u.update({"course_id": "c1", "created_at": "2026-01-01", "deleted_at": None})

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)
    def rpc(self, name, args): return _RPC(self, args)


def _fixture(tier="tutor_tier", status="active"):
    fake = _DB(tier, status)
    p1 = patch("app.services.lesson_service.db", fake)
    p2 = patch("app.services.grace_service.db", fake)
    return fake, p1, p2


@pytest.fixture
def db():
    fake, p1, p2 = _fixture()
    with p1, p2:
        ls.grant_paid_month("s1", "tutor_tier", "in_1")          # slots 0-4: intro, alpha, u1, u2
        yield fake


# 2 + 3 + 4
def test_read_ahead_is_next_months_first_lesson(db):
    s = ls.read_ahead("s1", "c1")
    assert (s["available"], s["lesson"]["unit_id"], s["unlocked"]) == (True, "u3", False)
    s = ls.request_read_ahead("s1", "c1")
    assert s["unlocked"] is True
    ra = [r for r in db.rows["lesson_entitlement"] if r["source"] == "read_ahead"]
    assert [(r["slot_from"], r["slot_to"], r["grant_ref"]) for r in ra] == [(4, 5, "in_1")]
    ls.request_read_ahead("s1", "c1")                              # one per month
    assert len([r for r in db.rows["lesson_entitlement"] if r["source"] == "read_ahead"]) == 1
    ls.grant_paid_month("s1", "tutor_tier", "in_2")
    nxt = db.rows["lesson_entitlement"][-1]
    assert (nxt["slot_from"], nxt["slot_to"]) == (4, 8)           # covers the read-ahead slot again: early, not extra
    assert ls.read_ahead("s1", "c1")["lesson"]["unit_id"] == "u7"  # and next month's read-ahead moves on


# 5
def test_completion_brings_the_offer(db):
    ls.request_read_ahead("s1", "c1")
    assert ls.read_ahead("s1", "c1")["offer"] is None
    db.rows["unit_progress"].append({"user_id": "s1", "unit_id": "u3", "status": "completed"})
    assert ls.read_ahead("s1", "c1")["offer"] == {"to_tier": "intensive_tier", "per_month": 6}


def test_no_offer_without_reading_ahead(db):
    db.rows["unit_progress"].append({"user_id": "s1", "unit_id": "u3", "status": "completed"})
    assert ls.read_ahead("s1", "c1")["offer"] is None


def test_intensive_gets_no_offer():
    fake, p1, p2 = _fixture("intensive_tier")
    with p1, p2:
        ls.grant_paid_month("s1", "intensive_tier", "in_1")
        ls.request_read_ahead("s1", "c1")
        unit = ls.read_ahead("s1", "c1")["lesson"]["unit_id"]
        fake.rows["unit_progress"].append({"user_id": "s1", "unit_id": unit, "status": "completed"})
        assert ls.read_ahead("s1", "c1")["offer"] is None


# 1 + 2
@pytest.mark.parametrize("tier,status,setup,reason", [
    ("free_tier", "active", None, "not_paying"),
    ("tutor_tier", "canceled", None, "not_subscribed"),
    ("tutor_tier", "active", "grace", "payment_pending"),
    ("tutor_tier", "active", "nogrant", "no_paid_month"),
    ("learner_tier", "active", None, "next_is_free"),            # Learner month 1 ends at slot 2 = Unit 1, free
])
def test_refusals(tier, status, setup, reason):
    fake, p1, p2 = _fixture(tier, status)
    with p1, p2:
        if setup != "nogrant":
            ls.grant_paid_month("s1", tier, "in_1") if ls.lessons_per_month(tier) else None
        if setup == "grace":
            fake.rows["payment_grace"].append({"student_id": "s1", "resolved_at": None, "lapsed_at": None})
        assert ls.read_ahead("s1", "c1")["reason"] == reason
        with pytest.raises(ReadAheadRefused):
            ls.request_read_ahead("s1", "c1")
