"""Standby roster and tutor no-shows (enrolment plan 8.6, §125).

Pinned:
1. Standby windows: validated, at most 14, the tutor's own zone, removable;
   a window holds an instant in its own zone.
2. A learner in the session reports a no-show from the start to +15 minutes;
   strangers can't; a second report returns the open one.
3. Offered to standby tutors first (even outside their availability), then
   guests; taken until +20 minutes; the absent lead gets `no_show`, the taker
   `cover_taken`.
4. The lead turning up withdraws it: no mark. A request whose session was
   delivered is withdrawn by the daily run, not failed.
5. Uncovered: the daily run fails it, credits each learner, marks `no_show`.
6. Reliability: 100 - 10 late_drop - 25 no_show + 5 cover_taken, 90 days, 0..100.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import cover_service as cs
from app.services import email_service, hierarchy_service
from app.services import standby_service as sb
from app.services.cover_service import CoverRefused
from app.services.standby_service import StandbyRefused
from tests.test_cover import NOW, _DB, _Q

START = NOW + timedelta(days=3)          # Thu 4 Feb 2027 12:00 UTC


class _Q2(_Q):
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self


class _DB2(_DB):
    def __init__(self):
        super().__init__()
        self.rows["tutor_standby_window"] = []
        self.rows["cohort_session_attendance"] = []
        self.rows["user_profiles"] = [{"id": "z9", "timezone": "Africa/Nairobi"}]

    def table(self, n): return _Q2(self, n)


@pytest.fixture
def db():
    fake = _DB2()
    sent = AsyncMock(return_value={"status": "sent"})
    alerts = AsyncMock(return_value={"status": "sent"})
    # Nobody's weekly availability holds the session: only standby can make a tutor free.
    with patch("app.services.cover_service.db", fake), \
         patch("app.services.standby_service.db", fake), \
         patch("app.services.cover_service._now", return_value=START + timedelta(minutes=5)), \
         patch("app.services.schedule_service.tutor_covers", side_effect=lambda t, *a: t == "g1"), \
         patch("app.services.makeup_service.load_busy", side_effect=lambda ids, now: {}), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: i.title() for i in ids if i}), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch("app.services.makeup_delivery._zones", side_effect=lambda ids: {}), \
         patch.object(email_service, "send_template_email", sent), \
         patch.object(email_service, "send_email", alerts), \
         patch("app.services.cover_service.settings.admin_email", "ops@tauka.io"), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        fake.sent, fake.alerts = sent, alerts
        yield fake


def _later(db, minutes):
    return patch("app.services.cover_service._now", return_value=START + timedelta(minutes=minutes))


# 1
def test_windows(db):
    w = sb.add_window("z9", 3, "14:00", "17:00")             # Thursday, Nairobi
    assert w["timezone"] == "Africa/Nairobi"
    for bad in ((7, "10:00", "11:00"), (1, "22:00", "20:00"), (1, "9:00", "10:00")):
        with pytest.raises(StandbyRefused):
            sb.add_window("z9", *bad)
    with pytest.raises(StandbyRefused):
        sb.add_window("stranger", 1, "10:00", "11:00")
    assert sb.on_standby(["z9"], START) == {"z9"}              # 12:00 UTC = 15:00 Nairobi
    assert sb.on_standby(["z9"], START + timedelta(hours=3)) == set()
    sb.remove_window("z9", w["id"])
    assert sb.windows("z9") == [] and sb.on_standby(["z9"], START) == set()
    for i in range(sb.MAX_WINDOWS):
        sb.add_window("z9", i % 7, f"{i:02d}:00", f"{i:02d}:30")
    with pytest.raises(StandbyRefused):
        sb.add_window("z9", 0, "23:00", "00:00")


# 2 + 3
@pytest.mark.asyncio
async def test_report_and_standby_takes_it(db):
    sb.add_window("z9", 3, "14:00", "17:00")
    with pytest.raises(CoverRefused) as e:
        await cs.report_no_show("stranger", "cohort_session", "cs1")
    assert e.value.reason == "not_your_session"
    with _later(db, -5), pytest.raises(CoverRefused) as e:
        await cs.report_no_show("a", "cohort_session", "cs1")
    assert e.value.reason == "not_now"
    r = await cs.report_no_show("a", "cohort_session", "cs1")
    assert (r["reason_code"], r["reported_by"], r["requested_by"], r["late"]) == ("no_show", "a", "home", True)
    # standby z9 first (not available by its weekly windows), then guest g1; busy/half/off excluded
    assert [(o["tutor_id"], o["rank"]) for o in db.rows["session_cover_offer"]] == [("z9", 1), ("g1", 2)]
    assert {c.args[2]["kind"] for c in db.sent.await_args_list} == {"urgent", "absent_lead"}
    assert "NO-SHOW" in db.alerts.await_args.args[1]
    assert (await cs.report_no_show("b", "cohort_session", "cs1"))["already"] is True
    rid = r["id"]
    assert cs.offers_for("z9")[0]["no_show"] is True
    with _later(db, 15):
        await cs.accept("z9", rid)
    assert db.rows["cohort_session"][0]["lead_tutor_id"] == "z9"
    assert [(e["tutor_id"], e["kind"]) for e in db.rows["tutor_reliability_event"]] == [
        ("home", "no_show"), ("z9", "cover_taken")]


@pytest.mark.asyncio
async def test_too_late_to_take(db):
    r = await cs.report_no_show("a", "cohort_session", "cs1")
    with _later(db, 25), pytest.raises(CoverRefused) as e:
        await cs.accept("g1", r["id"])
    assert e.value.reason == "started"


# 4
@pytest.mark.asyncio
async def test_lead_turns_up(db):
    r = await cs.report_no_show("a", "cohort_session", "cs1")
    assert cs.mine("home")[0]["request"]["no_show"] is True       # "I'm here" shows on their panel
    cs.withdraw("home", r["id"])
    assert db.rows["session_cover_request"][0]["status"] == "withdrawn"
    assert db.rows["tutor_reliability_event"] == []


@pytest.mark.asyncio
async def test_delivered_is_withdrawn_not_failed(db):
    await cs.report_no_show("a", "cohort_session", "cs1")
    db.rows["cohort_session_attendance"].append({"cohort_session_id": "cs1", "user_id": "a", "status": "attended"})
    out = await cs.daily(START + timedelta(hours=2))
    assert out["failed"] == 1                                       # processed
    assert db.rows["session_cover_request"][0]["status"] == "withdrawn"
    assert db.rows["session_credit"] == [] and db.rows["tutor_reliability_event"] == []
    assert db.rows["cohort_session"][0]["status"] == "scheduled"


# 5
@pytest.mark.asyncio
async def test_uncovered_fails_with_credits_and_mark(db):
    await cs.report_no_show("a", "cohort_session", "cs1")
    assert (await cs.daily(START + timedelta(minutes=10)))["failed"] == 0    # still takeable
    await cs.daily(START + timedelta(hours=2))
    assert db.rows["session_cover_request"][0]["status"] == "failed"
    assert db.rows["cohort_session"][0]["status"] == "cancelled"
    assert sorted(c["student_id"] for c in db.rows["session_credit"]) == ["a", "b"]
    assert [(e["tutor_id"], e["kind"]) for e in db.rows["tutor_reliability_event"]] == [("home", "no_show")]


# 6
def test_reliability(db):
    recent = (START - timedelta(days=10)).isoformat()
    old = (START - timedelta(days=120)).isoformat()
    db.rows["tutor_reliability_event"] = [
        {"tutor_id": "home", "kind": "late_drop", "created_at": recent},
        {"tutor_id": "home", "kind": "no_show", "created_at": recent},
        {"tutor_id": "home", "kind": "no_show", "created_at": old},
        {"tutor_id": "z9", "kind": "cover_taken", "created_at": recent},
        *[{"tutor_id": "g1", "kind": "no_show", "created_at": recent}] * 5,
    ]
    r = sb.reliability(["home", "z9", "g1"], START)
    assert (r["home"]["score"], r["home"]["no_show"], r["home"]["late_drop"]) == (65, 1, 1)
    assert r["z9"]["score"] == 100 and r["g1"]["score"] == 0
