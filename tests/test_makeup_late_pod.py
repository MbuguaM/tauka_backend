"""§2f item 8 in the sweep: a learner podded late keeps the proposed make-up
that covers the shortfall, and loses it only once pod sessions in that paid
month (plus their other make-ups for it) reach the tier's count."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services import makeup_service as ms

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
PAID = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f = db, name, []

    def select(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def lt(self, c, v): self.f.append(lambda r: str(r.get(c)) < str(v)); return self

    def execute(self):
        return SimpleNamespace(data=[r for r in self.db.rows.get(self.name, []) if all(f(r) for f in self.f)])


class _DB:
    def __init__(self, pod_days):
        self.rows = {
            "student": [{"id": "s", "tier": "tutor_tier"}],
            "cohorts": [{"id": "pod", "status": "active"}],
            "cohort_memberships": [{"user_id": "s", "cohort_id": "pod", "status": "active"}],
            "cohort_session": [{"cohort_id": "pod", "status": "scheduled",
                                "scheduled_at": (PAID + timedelta(days=d)).isoformat()} for d in pod_days],
            "makeup_session_participant": [
                {"makeup_session_id": "done", "student_id": "s", "funded_by": "month",
                 "period_start": PAID.isoformat(), "status": "attended"},
                {"makeup_session_id": "open", "student_id": "s", "funded_by": "month",
                 "period_start": PAID.isoformat(), "status": "pending"},
            ],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


SEATS = [{"makeup_session_id": "open", "student_id": "s", "funded_by": "month", "status": "pending"}]


def test_one_pod_session_left_keeps_the_make_up():
    with patch.object(ms, "db", _DB([25])):
        assert ms._month_covered(SEATS, {"s"}, NOW) == set()      # 1 made-up + 1 pod < 3


def test_pod_plus_other_make_ups_covering_the_month_withdraws_it():
    with patch.object(ms, "db", _DB([20, 27])):
        assert ms._month_covered(SEATS, {"s"}, NOW) == {("open", "s")}   # 1 + 2 = 3


def test_pod_sessions_after_the_paid_month_do_not_count():
    with patch.object(ms, "db", _DB([35, 38])):                       # 8 Dec, 11 Dec: next month
        assert ms._month_covered(SEATS, {"s"}, NOW) == set()
