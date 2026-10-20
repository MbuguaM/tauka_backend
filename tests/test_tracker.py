"""The milestone tracker (enrolment plan 4.7).

Pinned (developer decisions 2026-10-18):
1. Expected lessons come from the tier's unlocks, spread across each paid month;
   read-ahead and revoked grants don't count; Phase 0's slots are not lessons.
2. On track < 1 behind, slipping 1 to < 3, behind 3+; B2 = lesson 60; the date
   is an estimate except on Intensive; each status has one fix, and the next
   tier's saving is offered when behind or worth 3+ months.
3. No pace (Free) gives no date; lesson 60 reached says so.
4. E-16: the tracker never reads activity or AI-usage tables.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import tracker_service as tr

NOW = datetime(2027, 3, 1, 12, tzinfo=timezone.utc)


def g(days_ago, slot_from, slot_to, source="paid_month", revoked=False):
    return {"available_from": (NOW - timedelta(days=days_ago)).isoformat(), "slot_from": slot_from,
            "slot_to": slot_to, "source": source, "revoked_at": NOW.isoformat() if revoked else None}


# 1
def test_expected_slots_spread_across_the_month():
    grants = [g(45, 0, 4), g(15, 4, 8), g(1, 8, 9, "read_ahead"), g(40, 9, 13, revoked=True),
              g(-5, 13, 17)]                       # a future month counts nothing yet
    assert tr.expected_slots(grants, NOW) == pytest.approx(4 + 4 * 0.5)


# 2
@pytest.mark.parametrize("done,expected,status", [(5, 5.5, "on_track"), (5, 6.0, "slipping"),
                                                   (5, 7.9, "slipping"), (5, 8.0, "behind")])
def test_status_thresholds(done, expected, status):
    assert tr.assess("tutor_tier", done, expected, NOW)["status"] == status


def test_projection_and_fix():
    t = tr.assess("tutor_tier", 20, 20, NOW)
    # 40 lessons at 4 a month = 10 months of 30 days.
    assert t["projected_b2"] == (NOW + timedelta(days=300)).date().isoformat()
    assert t["estimate"] is True and t["fix"].startswith("Keep going: lesson 21 is next.")
    assert t["upgrade_saves_months"] == 3 and "Intensive would bring lesson 60 about 3 months sooner" in t["fix"]
    i = tr.assess("intensive_tier", 20, 20, NOW)
    assert i["estimate"] is False and i["upgrade_saves_months"] is None
    b = tr.assess("learner_tier", 4, 8, NOW)
    assert b["status"] == "behind" and b["fix"].startswith("You're 4 lessons behind.")
    assert "Tutor would bring lesson 60" in b["fix"]
    s = tr.assess("tutor_tier", 50, 51.5, NOW)
    assert s["fix"] == "Finish lesson 51 this week to get back on track."      # 2.5 months saved < 3: no upsell


# 3
def test_no_pace_and_reached():
    assert tr.assess("free_tier", 2, 0, NOW)["status"] == "no_pace"
    assert tr.assess(None, 2, 0, NOW)["projected_b2"] is None
    r = tr.assess("tutor_tier", 60, 70, NOW)
    assert r["status"] == "reached" and r["projected_b2"] is None


# 4
class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f = db, name, []
        db.read.add(name)

    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self.db.rows.get(self.name, []) if all(f(r) for f in self.f)])


class _DB:
    def __init__(self):
        self.read: set[str] = set()
        self.rows = {
            "student": [{"id": "s", "tier": "tutor_tier"}],
            "student_course": [{"student_id": "s", "course_id": "am"}],
            "unit": [{"course_id": "am", "is_preliminary": True}, {"course_id": "am", "is_preliminary": True},
                     {"course_id": "am", "is_preliminary": False}],
            # Two whole months unlocked: slots 0-8 = Phase 0's 2 + content lessons 1-6.
            "lesson_entitlement": [{"student_id": "s", **g(70, 0, 4)}, {"student_id": "s", **g(40, 4, 8)}],
            "cohort_memberships": [{"user_id": "s", "cohort_id": "pod1", "status": "active"}],
            "cohorts": [{"id": "pod1", "start_date": "2027-01-09"}],
            "config": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


def test_for_student_reads_only_grants_and_completions():
    fake = _DB()
    with patch("app.services.tracker_service.db", fake), \
         patch("app.services.intake_service._current_lesson_map", return_value={"s": 4}):  # done = 3
        t = tr.for_student("s", NOW)
    assert t["expected"] == 6.0 and t["lessons_done"] == 3 and t["status"] == "behind"
    assert t["clock_started"] == "2027-01-09"
    assert not fake.read & {"activity_summary", "activity_event", "ai_usage"}
