"""Moving a pod's time (enrolment plan 4.5, E-14, §113).

Pinned (developer decisions 2026-10-18):
1. Ops only proposes. A permanent change starts on a checkpoint (Ethiopic
   day 1); a temporary window is at most 60 days; the new time must be inside
   the home tutor's availability.
2. Every active member must agree; one "no" ends it; a member who joins later
   must agree too; nothing agreed by the start date expires.
3. Permanent: future sessions at the old time cancelled, the slot changed,
   sessions generated, the month's count kept. Temporary: each session in the
   window replaced by one at the new weekday/time in the same week, the
   original cancelled (so it can't come back); sessions outside untouched.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hierarchy_service
from app.services import schedule_service as ss
from app.services.schedule_service import ScheduleRefused

_real_mail = ss._mail   # captured before the fixture mocks it

TZ = "Africa/Nairobi"
TODAY = date(2027, 1, 20)
NOW = datetime(2027, 2, 8, 6, tzinfo=timezone.utc)   # 1 Yekatit 2019, 09:00 Nairobi
YEKATIT_1 = date(2027, 2, 8)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def lt(self, c, v): self.f.append(lambda r: str(r.get(c)) < str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if self.name == "pod_schedule_consent" and any(
                    (r["change_id"], r["student_id"]) == (p["change_id"], p["student_id"]) for r in rows):
                raise Exception("duplicate key pod_schedule_consent_pkey")
            if self.name == "pod_schedule_change" and any(
                    r["cohort_id"] == p["cohort_id"] and r["status"] in ("proposed", "agreed") for r in rows):
                raise Exception("duplicate key idx_pod_schedule_change_one_live")
            row = {"id": f"{self.name}-{len(rows) + 1}", "status": "proposed" if self.name == "pod_schedule_change"
                   else p.get("status"), **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


def _utc(local_iso: str) -> str:
    """'2027-02-12T18:00' in Nairobi (UTC+3) → UTC ISO."""
    d = datetime.fromisoformat(local_iso) - timedelta(hours=3)
    return d.replace(tzinfo=timezone.utc).isoformat()


class _DB:
    def __init__(self):
        self.rows = {
            "cohorts": [{"id": "pod1", "name": "Tir A", "tutor_id": "tut", "status": "active",
                         "slot_day_of_week": 4, "slot_time": "18:00", "slot_timezone": TZ, "sessions_per_month": 3}],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": u, "status": "active"} for u in ("a", "b")],
            # Tutor free Tue and Fri evenings, Nairobi time.
            "tutor_availability": [{"tutor_id": "tut", "day_of_week": d, "start_time": "17:00:00",
                                    "end_time": "22:00:00", "timezone": TZ, "active": True} for d in (1, 4)],
            "pod_schedule_change": [], "pod_schedule_consent": [],
            "cohort_session": [
                {"id": "past", "cohort_id": "pod1", "session_number": 1, "scheduled_at": _utc("2027-02-05T18:00"),
                 "status": "completed", "duration_minutes": 50},
                {"id": "f1", "cohort_id": "pod1", "session_number": 2, "scheduled_at": _utc("2027-02-12T18:00"),
                 "status": "scheduled", "duration_minutes": 50, "lead_tutor_id": "tut", "lead_role": "home"},
                {"id": "f2", "cohort_id": "pod1", "session_number": 3, "scheduled_at": _utc("2027-02-19T18:00"),
                 "status": "scheduled", "duration_minutes": 50, "lead_tutor_id": "g", "lead_role": "guest"},
                {"id": "f3", "cohort_id": "pod1", "session_number": 4, "scheduled_at": _utc("2027-03-12T18:00"),
                 "status": "scheduled", "duration_minutes": 50, "lead_tutor_id": "tut", "lead_role": "home"},
            ],
        }
        self.rpc_calls = []

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)

    def rpc(self, name, args):
        self.rpc_calls.append((name, args))
        # Generate the new slot's (Tuesday 20:00) sessions for the rest of February.
        for i, day in enumerate(("2027-02-16", "2027-02-23")):
            self.rows["cohort_session"].append({"id": f"gen{i}", "cohort_id": "pod1", "status": "scheduled",
                                                "scheduled_at": _utc(f"{day}T20:00")})
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=2))


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.schedule_service.db", fake), \
         patch("app.services.schedule_service._now", return_value=NOW), \
         patch("app.services.schedule_service._mail", new=AsyncMock()), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        yield fake


# 1
@pytest.mark.asyncio
async def test_propose_rules(db):
    async def p(**kw):
        args = dict(actor="ops", cohort_id="pod1", kind="permanent", day=1, at="20:00",
                    starts_on=YEKATIT_1, ends_on=None, reason="tutor", today=TODAY)
        args.update(kw)
        return await ss.propose(**args)

    for kw, reason in [({"actor": "x"}, "not_allowed"), ({"starts_on": date(2027, 2, 9)}, "not_a_checkpoint"),
                       ({"day": 2}, "tutor_not_available"), ({"day": 4, "at": "18:00"}, "same_slot"),
                       ({"kind": "temporary", "ends_on": date(2027, 5, 1)}, "bad_window"),
                       ({"starts_on": TODAY}, "starts_too_soon")]:
        with pytest.raises(ScheduleRefused) as e:
            await p(**kw)
        assert e.value.reason == reason, kw
    row = await p()
    assert row["status"] == "proposed" and row["timezone"] == TZ
    with pytest.raises(ScheduleRefused) as e:
        await p()
    assert e.value.reason == "already_proposed"


def test_tutor_window_must_hold_the_whole_session(db):
    assert ss.tutor_covers("tut", 1, ss._hhmm("21:10"), TZ, TODAY)       # 21:10-22:00
    assert not ss.tutor_covers("tut", 1, ss._hhmm("21:20"), TZ, TODAY)   # runs past 22:00
    assert not ss.tutor_covers("tut", 1, ss._hhmm("16:30"), TZ, TODAY)


# 2
@pytest.mark.asyncio
async def test_consent_all_or_nothing(db):
    ch = await ss.propose("ops", "pod1", "permanent", 1, "20:00", YEKATIT_1, None, "tutor", TODAY)
    assert ss.answer("a", ch["id"], True)["status"] == "proposed"
    view = ss.mine("b")[0]
    assert (view["answered"], view["members"], view["your_answer"]) == (1, 2, None)
    assert view["first_new_at"] == _utc("2027-02-09T20:00")     # first Tuesday on/after 8 Feb
    with pytest.raises(ScheduleRefused) as e:
        ss.answer("a", ch["id"], True)
    assert e.value.reason == "already_answered"
    with pytest.raises(ScheduleRefused) as e:
        ss.answer("stranger", ch["id"], True)
    assert e.value.reason == "not_your_pod"
    assert ss.answer("b", ch["id"], True)["status"] == "agreed"


@pytest.mark.asyncio
async def test_one_no_ends_it_and_silence_expires(db):
    ch = await ss.propose("ops", "pod1", "permanent", 1, "20:00", YEKATIT_1, None, "tutor", TODAY)
    assert ss.answer("a", ch["id"], False)["status"] == "declined"
    db.rows["pod_schedule_change"][0]["status"] = "declined"
    ch2 = await ss.propose("ops", "pod1", "permanent", 1, "20:00", YEKATIT_1, None, "tutor", TODAY)
    out = await ss.daily(NOW)
    assert out["expired"] == 1
    assert db.rows["pod_schedule_change"][1]["status"] == "expired" and ch2["id"]


@pytest.mark.asyncio
async def test_late_joiner_must_agree(db):
    ch = await ss.propose("ops", "pod1", "permanent", 1, "20:00", YEKATIT_1, None, "tutor", TODAY)
    ss.answer("a", ch["id"], True)
    ss.answer("b", ch["id"], True)
    db.rows["cohort_memberships"].append({"cohort_id": "pod1", "user_id": "c", "status": "active"})
    assert (await ss.daily(NOW))["waiting"] == 1
    ss.answer("c", ch["id"], True)
    assert (await ss.daily(NOW))["applied"] == 1


# 3
@pytest.mark.asyncio
async def test_permanent_regenerates_and_keeps_the_count(db):
    ch = await ss.propose("ops", "pod1", "permanent", 1, "20:00", YEKATIT_1, None, "tutor", TODAY)
    ss.answer("a", ch["id"], True)
    ss.answer("b", ch["id"], True)
    assert (await ss.daily(NOW))["applied"] == 1
    status = {s["id"]: s["status"] for s in db.rows["cohort_session"]}
    assert status["past"] == "completed"
    assert status["f1"] == status["f2"] == status["f3"] == "cancelled"
    pod = db.rows["cohorts"][0]
    assert (pod["slot_day_of_week"], pod["slot_time"]) == (1, "20:00")
    assert db.rows and db.rpc_calls == [("generate_cohort_sessions", {"p_cohort": "pod1", "p_months": 2})]
    # February: 1 held (5th) + 2 generated = 3 = the cadence, so nothing trimmed.
    assert status["gen0"] == status["gen1"] == "scheduled"
    assert db.rows["pod_schedule_change"][0]["status"] == "applied"


@pytest.mark.asyncio
async def test_temporary_window_replaces_only_sessions_inside(db):
    ch = await ss.propose("ops", "pod1", "temporary", 1, "21:00", date(2027, 2, 9), date(2027, 3, 1),
                          "ramadan", TODAY)
    ss.answer("a", ch["id"], True)
    ss.answer("b", ch["id"], True)
    out = await ss.daily(datetime(2027, 2, 9, 6, tzinfo=timezone.utc))
    assert out["applied"] == 1
    status = {s["id"]: s["status"] for s in db.rows["cohort_session"]}
    assert status["f1"] == status["f2"] == "cancelled"     # Fri 12th, 19th: inside the window
    assert status["f3"] == "scheduled"                     # 12 March: after it
    new = [s for s in db.rows["cohort_session"] if s["id"].startswith("cohort_session-")]
    # The same weeks' Tuesdays at 21:00 Nairobi, keeping each session's lead.
    assert sorted((s["scheduled_at"], s["lead_role"]) for s in new) == [
        (_utc("2027-02-09T21:00"), "home"), (_utc("2027-02-16T21:00"), "guest")]
    assert db.rows["cohorts"][0]["slot_day_of_week"] == 4    # the usual slot is unchanged
    # A session generated later inside the window is moved on the next run;
    # the ones already moved are left alone.
    db.rows["cohort_session"].append({"id": "late", "cohort_id": "pod1", "session_number": 9, "status": "scheduled",
                                      "scheduled_at": _utc("2027-02-26T18:00"), "duration_minutes": 50})
    out = await ss.daily(datetime(2027, 2, 10, 6, tzinfo=timezone.utc))
    assert out["rewindowed"] == 1
    assert {s["id"]: s["status"] for s in db.rows["cohort_session"]}["late"] == "cancelled"
    assert sum(1 for s in db.rows["cohort_session"] if s["id"].startswith("cohort_session-")) == 3


def test_ops_withdraw(db):
    db.rows["pod_schedule_change"].append({"id": "c1", "cohort_id": "pod1", "status": "agreed"})
    assert ss.withdraw("ops", "c1")["status"] == "withdrawn"
    with pytest.raises(ScheduleRefused):
        ss.withdraw("ops", "c1")


def test_template_renders():
    from app.services import email_service
    for kind in ("proposed", "applied"):
        html = email_service._jinja_env.get_template("schedule_change.html").render(
            subject="s", kind=kind, change_kind="temporary", pod_name="Tir A", audience="learner",
            first_new="Tue 9 Feb, 21:00 (Africa/Nairobi)", starts_on="2027-02-09", ends_on="2027-03-01")
        assert "Tue 9 Feb" in html and "2027-03-01" in html


@pytest.mark.asyncio
async def test_applied_email_carries_an_updated_calendar(db):
    """Plan 4.5: the new sessions, and the old ones cancelled (same UID) so calendars drop them."""
    from app.services import email_service
    ch = await ss.propose("ops", "pod1", "permanent", 1, "20:00", YEKATIT_1, None, "tutor", TODAY)
    ss.answer("a", ch["id"], True)
    ss.answer("b", ch["id"], True)
    await ss.daily(NOW)
    sent = AsyncMock(return_value={"status": "sent"})
    with patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"),          patch("app.services.makeup_delivery._zones", side_effect=lambda ids: {}),          patch.object(email_service, "send_template_email", sent):
        await _real_mail("pod1", db.rows["pod_schedule_change"][0], "applied")
    call = sent.await_args
    body = call.kwargs["attachments"][0]["content"].decode()
    assert call.args[2]["calendar"] is True
    assert "UID:gen0@tauka.io" in body and "UID:f1@tauka.io" in body
    assert body.count("STATUS:CANCELLED") == 3                    # f1, f2, f3
