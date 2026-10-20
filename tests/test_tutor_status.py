"""Tutor status (enrolment plan §2j J4, §122; CONFLICT-057).

Pinned:
1. Ops only; the allowed moves (onboarding -> active | offboarded, active ->
   suspended | offboarded, suspended -> active | offboarded; offboarded final);
   a reason code each time; offboarding sets deleted_at.
2. Every change is a status event; approval starts the trail.
3. Suspending or offboarding reports the sessions still led (to arrange cover).
4. Details: language, and a two-letter country code.
5. The tutor's own view: status and the onboarding steps.
6. J5 (§124): activation needs the five checks; calibration needs >= 85;
   unticking corrects; no free-text reference.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import hierarchy_service
from app.services import tutor_status_service as ts
from app.services.tutor_status_service import TutorStatusRefused

NOW = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
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
        later = (NOW + timedelta(days=3)).isoformat()
        self.rows = {
            "tutor": [{"id": "t-new", "status": "onboarding"}, {"id": "t-on", "status": "active"}],
            "tutor_status_event": [],
            "cohort_session": [{"lead_tutor_id": "t-on", "status": "scheduled", "scheduled_at": later, "id": "s1"},
                               {"lead_tutor_id": "t-on", "status": "completed", "scheduled_at": later, "id": "s2"}],
            "makeup_session": [{"tutor_id": "t-on", "status": "scheduled", "scheduled_at": later, "id": "m1"}],
            "clinic": [],
            "tutor_availability": [{"tutor_id": "t-on", "active": True, "id": "a1"}],
            "tutor_onboarding_check": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.tutor_status_service.db", fake), \
         patch("app.services.tutor_status_service._now", return_value=NOW), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        yield fake


# 1 + 2
def test_moves_and_trail(db):
    with pytest.raises(TutorStatusRefused):
        ts.set_status("stranger", "t-new", "active", "onboarding_complete")
    with pytest.raises(TutorStatusRefused) as e:
        ts.set_status("ops", "t-new", "suspended", "leave")
    assert e.value.reason == "not_allowed_from_onboarding"
    with pytest.raises(TutorStatusRefused):
        ts.set_status("ops", "t-new", "active", "because")
    _all_checks("t-new")
    out = ts.set_status("ops", "t-new", "active", "onboarding_complete")
    assert out == {"tutor_id": "t-new", "from": "onboarding", "status": "active"}
    ts.set_status("ops", "t-new", "offboarded", "left")
    t = db.rows["tutor"][0]
    assert t["status"] == "offboarded" and t["deleted_at"]
    with pytest.raises(TutorStatusRefused):
        ts.set_status("ops", "t-new", "active", "returning")                  # offboarded is final
    assert [(e["from_status"], e["to_status"], e["reason_code"]) for e in db.rows["tutor_status_event"]] == [
        ("onboarding", "active", "onboarding_complete"), ("active", "offboarded", "left")]
    assert [h["to_status"] for h in ts.history("ops", "t-new")] == ["active", "offboarded"]


def _all_checks(tid):
    for step in ts.CHECK_STEPS:
        ts.mark_check("ops", tid, step, True, 90 if step == "calibration" else None)


# 6
def test_activation_needs_the_checklist(db):
    with pytest.raises(TutorStatusRefused) as e:
        ts.set_status("ops", "t-new", "active", "onboarding_complete")
    assert e.value.reason == "checks_incomplete"
    with pytest.raises(TutorStatusRefused):
        ts.mark_check("someone", "t-new", "contract", True)
    with pytest.raises(TutorStatusRefused) as e:
        ts.mark_check("ops", "t-new", "contract", True, ref_code="signed by Hana")
    assert e.value.reason == "bad_ref"
    with pytest.raises(TutorStatusRefused) as e:
        ts.mark_check("ops", "t-new", "calibration", True)
    assert e.value.reason == "score_required"
    out = ts.mark_check("ops", "t-new", "calibration", True, 80)               # recorded as a fail
    assert {c["step"]: c["done"] for c in out["checklist"]}["calibration"] is False
    assert db.rows["tutor_onboarding_check"][-1]["score"] == 80
    _all_checks("t-new")
    ts.mark_check("ops", "t-new", "references", False)                         # a correction
    assert ts.missing_checks("t-new") == ["references"]
    ts.mark_check("ops", "t-new", "references", True, ref_code="R-2")
    assert ts.set_status("ops", "t-new", "active", "onboarding_complete")["status"] == "active"
    ts.set_status("ops", "t-on", "suspended", "leave")
    assert ts.set_status("ops", "t-on", "active", "returning")["status"] == "active"   # a return needs no checklist


# 3
def test_suspending_reports_what_needs_cover(db):
    out = ts.set_status("ops", "t-on", "suspended", "leave")
    assert out["needs_cover"] == {"pod_sessions": 1, "makeups": 1, "clinics": 0}
    assert ts.set_status("ops", "t-on", "active", "returning")["status"] == "active"


# 4
def test_details(db):
    t = ts.set_details("ops", "t-on", "Amharic", "et")
    assert (t["language"], t["country_code"]) == ("Amharic", "ET")
    with pytest.raises(TutorStatusRefused):
        ts.set_details("ops", "t-on", None, "ETH")
    with pytest.raises(TutorStatusRefused):
        ts.set_details("ops", "nobody", "Amharic", None)


# 5
def test_me(db):
    assert ts.me("t-new") == {"is_tutor": True, "status": "onboarding", "steps": [
        {"key": "availability", "done": False}, {"key": "id_check", "done": False},
        {"key": "references", "done": False}, {"key": "contract", "done": False},
        {"key": "training", "done": False}, {"key": "calibration", "done": False}]}
    ts.mark_check("ops", "t-new", "contract", True)
    assert {s["key"]: s["done"] for s in ts.me("t-new")["steps"]}["contract"] is True
    assert ts.me("t-on")["steps"][0]["done"] is True
    assert ts.me("student") == {"is_tutor": False}


# 2 — approval starts the trail
@pytest.mark.asyncio
async def test_approval_records_the_first_event(db):
    from unittest.mock import AsyncMock, MagicMock
    from app.services import email_service
    from app.services import tutor_application_service as apps

    class _AppDB(_DB):
        def __init__(self):
            super().__init__()
            self.rows["tutor_application"] = [{"id": "a1", "email": "n@x.test", "full_name": "Hana B",
                                               "status": "submitted", "admin_note": None}]
            self.rows["tutor"] = []
            self.auth = SimpleNamespace(admin=MagicMock())
            self.auth.admin.invite_user_by_email.return_value = SimpleNamespace(user=SimpleNamespace(id="u1"))

        def rpc(self, name, args):
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))

    fake = _AppDB()
    with patch("app.services.tutor_application_service.db", fake), \
         patch("app.services.tutor_status_service.db", fake), \
         patch.object(email_service, "send_template_email", AsyncMock(return_value={"status": "sent"})):
        await apps.approve("ops", "a1")
    assert fake.rows["tutor_status_event"] == [
        {"tutor_id": "u1", "from_status": None, "to_status": "onboarding", "reason_code": "approved", "actor": "ops"}]
