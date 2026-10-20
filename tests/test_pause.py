"""Pausing (enrolment plan 4.4, §111).

Pinned (developer decisions 2026-10-18):
1. A pause starts at the end of the paid month, lasts at most 3 months, and
   stops billing the way a cancellation does. Free, gift, offer-holding and
   already-paused learners are refused.
2. When it starts: out of the pod (CONFLICT-048 columns), offers declined,
   pending make-up seats withdrawn, enrolment `paused`.
3. Paying again resumes into the wait pool (or `paid` before Phase 0 is done);
   past 3 months it lapses into a cancellation and the credits are forfeited.
   A week before, one reminder.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import pause_service as ps
from app.services.pause_service import PauseRefused

NOW = datetime(2027, 3, 1, 9, tzinfo=timezone.utc)
PERIOD_END = NOW + timedelta(days=10)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            row = {"id": f"p{len(rows) + 1}", "status": "scheduled", **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        if kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.rows = {
            "student": [
                {"id": "s", "tier": "tutor_tier", "current_period_end": PERIOD_END.isoformat(),
                 "stripe_subscription_id": "sub_1"},
                {"id": "free", "tier": "free_tier"},
                {"id": "gifted", "tier": "tutor_tier", "active_gift_id": "g1"},
                {"id": "offered", "tier": "tutor_tier"},
            ],
            "enrolment_state": [{"student_id": "s", "state": "in_pod"},
                                {"student_id": "offered", "state": "matched"}],
            "enrolment_pause": [],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": "s", "status": "active"}],
            "match_offer": [],
            "makeup_session_participant": [{"makeup_session_id": "m1", "student_id": "s", "status": "pending"},
                                           {"makeup_session_id": "m-old", "student_id": "s", "status": "attended"}],
            "makeup_session": [{"id": "m1", "status": "proposed"}, {"id": "m-old", "status": "completed"}],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    moves = []
    with patch("app.services.pause_service.db", fake), \
         patch("app.services.phase0_service.advance",
               side_effect=lambda s, f, t: moves.append((s, tuple(f), t)) or 1), \
         patch("app.services.account_service.change_subscription", new=AsyncMock()) as stop, \
         patch("app.services.account_service.reactivate_subscription", new=AsyncMock()) as restart, \
         patch("app.services.makeup_service.forfeit_on_cancellation", return_value=2) as forfeit:
        fake.moves, fake.stop, fake.restart, fake.forfeit = moves, stop, restart, forfeit
        yield fake


# 1
@pytest.mark.asyncio
async def test_request_from_period_end_for_three_months(db):
    p = await ps.request("s", NOW)
    assert p["status"] == "scheduled" and p["effective_at"] == PERIOD_END.isoformat()
    assert p["ends_by"] == datetime(2027, 6, 11, 9, tzinfo=timezone.utc).isoformat()
    db.stop.assert_awaited_once_with("s", "free_tier", None, None)
    assert db.rows["cohort_memberships"][0]["status"] == "active"     # nothing changes yet
    with pytest.raises(PauseRefused) as e:
        await ps.request("s", NOW)
    assert e.value.reason == "already_paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("who,reason", [("free", "not_paying"), ("gifted", "gift"),
                                        ("offered", "answer_offers_first")])
async def test_refusals(db, who, reason):
    with pytest.raises(PauseRefused) as e:
        await ps.request(who, NOW)
    assert e.value.reason == reason
    assert ps.view(who)["reason"] == reason


@pytest.mark.asyncio
async def test_withdraw_before_it_starts(db):
    await ps.request("s", NOW)
    out = await ps.withdraw("s")
    assert out["status"] == "withdrawn"
    db.restart.assert_awaited_once_with("s")
    assert ps.view("s")["can_pause"] is True


# 2
@pytest.mark.asyncio
async def test_start_at_period_end(db):
    await ps.request("s", NOW)
    assert (await ps.daily(NOW))["started"] == 0
    assert (await ps.daily(PERIOD_END))["started"] == 1
    m = db.rows["cohort_memberships"][0]
    assert m["status"] == "paused" and m["removal_reason"].startswith("paused by the learner")
    seats = {x["makeup_session_id"]: x["status"] for x in db.rows["makeup_session_participant"]}
    assert seats == {"m1": "withdrawn", "m-old": "attended"}
    assert ("s", ps.PAUSABLE, "paused") in db.moves
    p = db.rows["enrolment_pause"][0]
    assert p["status"] == "active" and p["left_cohort_id"] == "pod1"
    with pytest.raises(PauseRefused):
        await ps.withdraw("s")


# 3
@pytest.mark.asyncio
async def test_resume_goes_to_wait_pool(db):
    await ps.request("s", NOW)
    await ps.daily(PERIOD_END)
    db.moves.clear()
    assert ps.resume_after_payment("s", PERIOD_END + timedelta(days=20)) is True
    assert db.moves == [("s", ("paused",), "seat_confirmed"), ("s", ("seat_confirmed",), "wait_pool"),
                        ("s", ("paused",), "paid")]
    assert db.rows["enrolment_pause"][0]["status"] == "resumed"
    assert ps.resume_after_payment("s") is False


@pytest.mark.asyncio
async def test_reminder_then_lapse(db):
    await ps.request("s", NOW)
    await ps.daily(PERIOD_END)
    ends = datetime.fromisoformat(db.rows["enrolment_pause"][0]["ends_by"])
    with patch("app.services.pause_service._remind", new=AsyncMock()) as remind:
        assert (await ps.daily(ends - timedelta(days=6)))["reminded"] == 1
        assert (await ps.daily(ends - timedelta(days=5)))["reminded"] == 0
        remind.assert_awaited_once()
    assert (await ps.daily(ends))["lapsed"] == 1
    assert ("s", ("paused",), "cancelled") in db.moves
    db.forfeit.assert_called_once_with("s")
    assert db.rows["enrolment_pause"][0]["status"] == "lapsed"


def test_reminder_template_renders():
    from app.services import email_service
    html = email_service._jinja_env.get_template("pause_ending.html").render(
        subject="s", ends="Fri 11 Jun, 12:00 (Africa/Nairobi)", web_base_url="https://tauka.io")
    assert "Fri 11 Jun" in html and "/account/subscription" in html and "forfeited" in html
