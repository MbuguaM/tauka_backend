"""Concern reports (§117) and tutors' rating averages (§116) — enrolment plan 4.6.

Pinned (developer decisions 2026-10-18):
1. Tutors see averages only once 3+ different learners rated in 30 days; a
   dimension rated by fewer than 3 stays hidden; stars are averaged as before.
2. A concern can name only someone the reporter knows through Tauka; a session
   only one they were in; "someone else"/"myself" name nobody.
3. Only safeguarding reads, acknowledges and closes; a closed report needs an
   outcome and stays closed.
4. The alert goes at once with the category only (never the note or a name),
   and once more when unacknowledged for 24 hours.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import concern_service as cs
from app.services import email_service, hierarchy_service
from app.services import rating_service as rs
from app.services.concern_service import ConcernRefused

NOW = datetime(2027, 2, 1, 12, tzinfo=timezone.utc)


# 1
def test_averages_need_three_learners():
    two = [{"student_id": "a", "stars": 5, "clarity": 4}, {"student_id": "b", "stars": 3, "clarity": 2}]
    assert rs.averages(two) == {"enough": False, "learners": 2, "min_learners": 3}
    rows = two + [{"student_id": "c", "stars": 4, "clarity": 3, "pace": 5},
                  {"student_id": "a", "stars": 4, "pace": 4}]
    out = rs.averages(rows)
    assert out["enough"] and out["learners"] == 3 and out["stars"] == 4.0
    assert out["clarity"] == 3.0
    assert out["pace"] is None            # only 2 learners scored pace
    assert out["atmosphere"] is None


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
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
            row = {"id": f"cr{len(rows) + 1}", "status": "open", "created_at": NOW.isoformat(), **p}
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
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": u} for u in ("me", "mate")],
            "cohorts": [{"id": "pod1", "tutor_id": "home"}],
            "cohort_session": [{"cohort_id": "pod1", "lead_tutor_id": "guest"}],
            "makeup_session_participant": [], "makeup_session": [],
            "student_tutor_link": [],
            "user_profiles": [{"id": "home", "first_name": "Marta", "last_name": "B"},
                              {"id": "mate", "first_name": "Selam", "last_name": None}],
            "admin_users": [{"id": "safe"}, {"id": "ops"}],
            "concern_report": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    sent = []

    async def send(to, subject, body, **k):
        sent.append((to, subject, body))
        return {"status": "sent"}

    with patch("app.services.concern_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.concern_service._now", return_value=NOW), \
         patch.object(hierarchy_service, "linked_tutor_ids", return_value=set()), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: (a, c) in {("safe", "safeguarding"), ("ops", "ops")}), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch("app.services.complaint_service.recent_sessions",
               return_value=[{"session_type": "cohort_session", "session_id": "11111111-1111-1111-1111-111111111111"}]), \
         patch.object(email_service, "send_email", side_effect=send):
        fake.sent = sent
        yield fake


# 2
def test_people_and_who_a_report_can_name(db):
    got = {(p["id"], p["kind"], p["name"]) for p in cs.people("me")}
    assert got == {("home", "tutor", "Marta"), ("guest", "tutor", "A tutor"), ("mate", "learner", "Selam")}
    with pytest.raises(ConcernRefused) as e:
        cs.create("me", "learner", "home", "harassment", None)      # home is a tutor, not a learner
    assert e.value.reason == "unknown_person"
    with pytest.raises(ConcernRefused) as e:
        cs.create("me", "tutor", "home", "other", None, "clinic", "22222222-2222-2222-2222-222222222222")
    assert e.value.reason == "not_your_session"
    r = cs.create("me", "learner", "mate", "contact_details", "  asked for my number  ",
                  "cohort_session", "11111111-1111-1111-1111-111111111111")
    assert r["about_user_id"] == "mate" and r["detail"] == "asked for my number"
    r2 = cs.create("me", "myself", "mate", "wellbeing", None)
    assert r2["about_user_id"] is None                               # nobody named for "myself"


# 3
def test_only_safeguarding_handles(db):
    r = cs.create("me", "tutor", "home", "harassment", "note")
    for who in ("ops", "me"):
        with pytest.raises(ConcernRefused):
            cs.queue(who)
    q = cs.queue("safe")
    assert q[0]["about_name"] == "Marta B" and q[0]["overdue"] is False
    cs.acknowledge("safe", r["id"])
    with pytest.raises(ConcernRefused):
        cs.acknowledge("safe", r["id"])
    out = cs.close("safe", r["id"], "warning_issued")
    assert out["status"] == "closed" and out["outcome"] == "warning_issued"
    with pytest.raises(ConcernRefused):
        cs.close("safe", r["id"], "no_action")
    assert [c["status"] for c in cs.mine("me")] == ["closed"]


# 4
@pytest.mark.asyncio
async def test_alerts_carry_no_note_and_repeat_once_when_overdue(db):
    r = cs.create("me", "learner", "mate", "harassment", "SECRET DETAIL")
    await cs.alert_new(r["id"])
    to, subject, body = db.sent[0]
    assert to == ["safe@x.test"] and "Harassment" in subject
    assert "SECRET" not in body and "Selam" not in body
    assert (await cs.daily(NOW + timedelta(hours=2))) == {"alerts": 0, "overdue": 0}
    assert (await cs.daily(NOW + timedelta(hours=25)))["overdue"] == 1
    assert "24 hours" in db.sent[-1][1]
    assert (await cs.daily(NOW + timedelta(hours=49)))["overdue"] == 0
