"""Cover requests (enrolment plan §2h, §123; CONFLICT-058).

Pinned:
1. Only the session's lead may ask, before it starts; one open request per
   session; inside 24 hours it is late (and a late_drop mark).
2. Offered to tutors in EVERY learner's pool, active, free (availability, no
   clash), never the requester; the pod's guests first.
3. First to accept wins: the lead and the room host change, other offers
   expire, cover_given / cover_taken recorded; later accepts are refused.
4. Withdraw only while open.
5. Ops: list, assign anyone in the pool (not outside it).
6. Daily: alert inside 30 hours once; a session that started uncovered is
   cancelled and every pod member credited once.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import cover_service as cs
from app.services import email_service, hierarchy_service
from app.services.cover_service import CoverRefused

NOW = datetime(2027, 2, 1, 12, tzinfo=timezone.utc)
START = NOW + timedelta(days=3)


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
            if self.name == "session_cover_request" and any(
                    r["session_id"] == p["session_id"] and r["status"] == "open" for r in rows):
                raise Exception("duplicate key idx_session_cover_request_one_open")
            if self.name == "session_credit" and any(
                    (r["student_id"], r["reference_id"]) == (p["student_id"], p["reference_id"]) for r in rows):
                raise Exception("duplicate key idx_session_credit_one_per_reference")
            row = {"id": f"{self.name}-{len(rows) + 1}", **({"status": "open"} if self.name == "session_cover_request" else {}),
                   **({"response": "pending"} if self.name == "session_cover_offer" else {}), **p}
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
            "cohort_session": [{"id": "cs1", "cohort_id": "pod1", "scheduled_at": START.isoformat(),
                                "duration_minutes": 50, "status": "scheduled", "lead_tutor_id": "home",
                                "video_session_id": "v1"}],
            "cohorts": [{"id": "pod1", "tutor_id": "home"}],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": u, "status": "active"} for u in ("a", "b")],
            "student_tutor_link": [
                {"student_id": "a", "tutor_id": "home", "role": "home", "state": "active"},
                {"student_id": "b", "tutor_id": "home", "role": "home", "state": "active"},
                {"student_id": "a", "tutor_id": "g1", "role": "guest", "state": "active"},
                {"student_id": "b", "tutor_id": "g1", "role": "guest", "state": "active"},
                {"student_id": "a", "tutor_id": "z9", "role": "home", "state": "active"},
                {"student_id": "b", "tutor_id": "z9", "role": "home", "state": "active"},
                {"student_id": "a", "tutor_id": "busy", "role": "guest", "state": "active"},
                {"student_id": "b", "tutor_id": "busy", "role": "guest", "state": "active"},
                {"student_id": "a", "tutor_id": "half", "role": "guest", "state": "active"},   # only a's
                {"student_id": "a", "tutor_id": "off", "role": "guest", "state": "active"},
                {"student_id": "b", "tutor_id": "off", "role": "guest", "state": "active"},
            ],
            "tutor": [{"id": t, "status": "active"} for t in ("home", "g1", "z9", "busy", "half")]
                     + [{"id": "off", "status": "suspended"}],
            "video_session": [{"id": "v1", "tutor_id": "home"}],
            "session_cover_request": [], "session_cover_offer": [], "tutor_reliability_event": [],
            "session_credit": [], "makeup_session": [], "makeup_session_participant": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    sent = AsyncMock(return_value={"status": "sent"})
    alerts = AsyncMock(return_value={"status": "sent"})
    with patch("app.services.cover_service.db", fake), \
         patch("app.services.cover_service._now", return_value=NOW), \
         patch("app.services.schedule_service.tutor_covers", side_effect=lambda t, *a: True), \
         patch("app.services.makeup_service.load_busy",
               side_effect=lambda ids, now: {"busy": [(START - timedelta(minutes=10), START + timedelta(minutes=40))]}
               if "busy" in ids else {}), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: i.title() for i in ids if i}), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch("app.services.makeup_delivery._zones", side_effect=lambda ids: {}), \
         patch.object(email_service, "send_template_email", sent), \
         patch.object(email_service, "send_email", alerts), \
         patch("app.services.cover_service.settings.admin_email", "ops@tauka.io"), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        fake.sent, fake.alerts = sent, alerts
        yield fake


# 1 + 2
@pytest.mark.asyncio
async def test_request_and_who_is_offered(db):
    with pytest.raises(CoverRefused) as e:
        await cs.request("g1", "cohort_session", "cs1", "ill")
    assert e.value.reason == "not_your_session"
    r = await cs.request("home", "cohort_session", "cs1", "ill")
    assert r["late"] is False and r["offered"] == 2
    offers = [(o["tutor_id"], o["rank"]) for o in db.rows["session_cover_offer"]]
    assert offers == [("g1", 1), ("z9", 2)]            # guests first; not busy, half-pool, suspended or the requester
    assert db.alerts.await_count == 1 and db.rows["session_cover_request"][0]["alerted_at"]
    assert [c.args[0] for c in db.sent.await_args_list] == ["g1@x.test", "z9@x.test"]
    with pytest.raises(CoverRefused) as e:
        await cs.request("home", "cohort_session", "cs1", "ill")
    assert e.value.reason == "already_requested"


@pytest.mark.asyncio
async def test_late_request_marks_late_drop(db):
    db.rows["cohort_session"][0]["scheduled_at"] = (NOW + timedelta(hours=5)).isoformat()
    r = await cs.request("home", "cohort_session", "cs1", "emergency")
    assert r["late"] is True
    assert [e["kind"] for e in db.rows["tutor_reliability_event"]] == ["late_drop"]
    assert "LATE" in db.alerts.await_args.args[1]


# 3
@pytest.mark.asyncio
async def test_first_accept_wins(db):
    await cs.request("home", "cohort_session", "cs1", "ill")
    rid = db.rows["session_cover_request"][0]["id"]
    assert [o["request_id"] for o in cs.offers_for("z9")] == [rid]
    await cs.accept("z9", rid)
    s = db.rows["cohort_session"][0]
    assert (s["lead_tutor_id"], s["lead_role"]) == ("z9", "guest")
    assert db.rows["video_session"][0]["tutor_id"] == "z9"
    assert {o["tutor_id"]: o["response"] for o in db.rows["session_cover_offer"]} == {"g1": "expired", "z9": "accepted"}
    assert [(e["tutor_id"], e["kind"]) for e in db.rows["tutor_reliability_event"]] == [
        ("home", "cover_given"), ("z9", "cover_taken")]
    with pytest.raises(CoverRefused):
        await cs.accept("g1", rid)
    learner_mail = [c for c in db.sent.await_args_list if c.args[2]["kind"] == "covered_learner"]
    assert {c.args[0] for c in learner_mail} == {"a@x.test", "b@x.test"}
    assert all("ill" not in str(c.args[2]) for c in learner_mail)          # never why


# 4
@pytest.mark.asyncio
async def test_withdraw(db):
    await cs.request("home", "cohort_session", "cs1", "clash")
    rid = db.rows["session_cover_request"][0]["id"]
    with pytest.raises(CoverRefused):
        cs.withdraw("g1", rid)
    cs.withdraw("home", rid)
    assert db.rows["session_cover_request"][0]["status"] == "withdrawn"
    assert {o["response"] for o in db.rows["session_cover_offer"]} == {"expired"}


# 5
@pytest.mark.asyncio
async def test_ops_assign_pool_rule(db):
    await cs.request("home", "cohort_session", "cs1", "ill")
    rid = db.rows["session_cover_request"][0]["id"]
    q = cs.open_requests("ops")
    assert {c["tutor_id"] for c in q[0]["can_assign"]} == {"g1", "z9", "busy"}   # availability is ops' call
    with pytest.raises(CoverRefused) as e:
        await cs.assign("ops", rid, "half")
    assert e.value.reason == "not_in_pool"
    await cs.assign("ops", rid, "busy")
    assert db.rows["session_cover_request"][0]["assigned_by"] == "ops"
    with pytest.raises(CoverRefused):
        cs.open_requests("home")


# 6
@pytest.mark.asyncio
async def test_daily_alert_then_fail_with_credits(db):
    await cs.request("home", "cohort_session", "cs1", "ill")
    db.alerts.reset_mock()
    assert (await cs.daily(START - timedelta(hours=40)))["alerted"] == 0      # alerted at request, > 30 h out
    assert (await cs.daily(START - timedelta(hours=20)))["alerted"] == 1
    assert (await cs.daily(START - timedelta(hours=10)))["alerted"] == 0      # once
    out = await cs.daily(START + timedelta(hours=1))
    assert out["failed"] == 1
    assert db.rows["cohort_session"][0]["status"] == "cancelled"
    assert sorted(c["student_id"] for c in db.rows["session_credit"]) == ["a", "b"]
    assert db.rows["session_cover_request"][0]["status"] == "failed"
    assert (await cs.daily(START + timedelta(hours=30)))["failed"] == 0


def test_cover_email_renders():
    html = email_service._jinja_env.get_template("cover.html").render(
        subject="s", kind="covered_learner", when="Thu 4 Feb, 15:00 (UTC)", tutor="<b>Z</b>")
    assert "&lt;b&gt;Z" in html and "Thu 4 Feb" in html


@pytest.mark.asyncio
async def test_mine_lists_led_sessions_with_their_request(db):
    m = cs.mine("home")
    assert [(x["session_id"], x["request"], x["late_if_now"]) for x in m] == [("cs1", None, False)]
    await cs.request("home", "cohort_session", "cs1", "ill")
    assert cs.mine("home")[0]["request"]["status"] == "open"
    assert cs.mine("g1") == []
