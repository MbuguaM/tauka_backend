"""Missed payment: grace then lapsed (enrolment plan 2.3a / 2.3b, §126).

Pinned:
1. A failed payment opens one grace (idempotent) and emails day 0.
2. Daily: day 3 and day 6 reminders once each; day 7 lapsed once.
3. Lapsed gates: no room token (402), no clinic booking, not grouped for make-ups.
4. Paid resolves it ('paid'); everything is back.
5. Cancelled resolves it ('cancelled') and the learner leaves the pod
   (membership dropped, enrolment cancelled); a pause keeps its own path.
6. The learner's view: none | grace (until) | lapsed.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import email_service
from app.services import grace_service as gs

NOW = datetime(2027, 2, 1, 12, tzinfo=timezone.utc)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if self.name == "payment_grace" and any(r["student_id"] == p["student_id"] and r.get("resolved_at") is None
                                                    for r in rows):
                raise Exception("duplicate key idx_payment_grace_one_open")
            row = {"id": f"g{len(rows) + 1}", "lapsed_at": None, "resolved_at": None, "reminded_3_at": None,
                   "reminded_6_at": None, **p}
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
            "payment_grace": [],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": "s1", "status": "active"}],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    sent = AsyncMock(return_value={"status": "sent"})
    advanced = []
    with patch("app.services.grace_service.db", fake), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch("app.services.makeup_delivery._zones", side_effect=lambda ids: {}), \
         patch("app.services.phase0_service.advance", side_effect=lambda sid, frm, to: advanced.append((sid, to))), \
         patch.object(email_service, "send_template_email", sent):
        fake.sent, fake.advanced = sent, advanced
        yield fake


def _kinds(db):
    return [c.args[2]["kind"] for c in db.sent.await_args_list]


# 1 + 2 + 6
@pytest.mark.asyncio
async def test_grace_then_lapse(db):
    assert gs.view("s1") == {"state": "none"}
    assert await gs.start("s1", "in_1", NOW) is True
    assert await gs.start("s1", "in_2", NOW + timedelta(days=1)) is False      # Stripe's retries: one grace
    assert gs.view("s1")["state"] == "grace" and gs.view("s1")["until"].startswith("2027-02-08")
    assert (await gs.daily(NOW + timedelta(days=2)))["reminded"] == 0
    assert (await gs.daily(NOW + timedelta(days=3, hours=1)))["reminded"] == 1
    assert (await gs.daily(NOW + timedelta(days=4)))["reminded"] == 0
    assert (await gs.daily(NOW + timedelta(days=6, hours=1)))["reminded"] == 1
    assert gs.is_lapsed("s1") is False
    assert (await gs.daily(NOW + timedelta(days=7, hours=1)))["lapsed"] == 1
    assert (await gs.daily(NOW + timedelta(days=8)))["lapsed"] == 0
    assert gs.is_lapsed("s1") and gs.lapsed_ids(["s1", "s2"]) == {"s1"}
    assert gs.view("s1")["state"] == "lapsed"
    assert _kinds(db) == ["started", "reminder", "last_day", "lapsed"]
    assert all("download" not in str(c.args[2]) for c in db.sent.await_args_list)   # the prompt is in the template


# 4
@pytest.mark.asyncio
async def test_paid_resolves(db):
    await gs.start("s1", "in_1", NOW)
    await gs.daily(NOW + timedelta(days=8))
    assert gs.resolve("s1", "paid") is True
    assert gs.is_lapsed("s1") is False and gs.view("s1") == {"state": "none"}
    assert db.rows["payment_grace"][0]["resolution"] == "paid"
    assert await gs.start("s1", "in_9", NOW + timedelta(days=40)) is True        # a later failure opens a new one


# 5
def test_cancel_leaves_the_pod(db):
    assert gs.leave_pod_on_cancel("s1", NOW) == "pod1"
    m = db.rows["cohort_memberships"][0]
    assert (m["status"], m["left_at"]) == ("dropped", NOW.isoformat())
    assert db.advanced == [("s1", "cancelled")]
    assert gs.leave_pod_on_cancel("s1", NOW) is None                              # nothing left to drop


# 3
@pytest.mark.asyncio
async def test_lapsed_gates(db):
    from fastapi import HTTPException
    from app.models.schemas import CallRequest
    from app.routes import calling
    from app.services import clinic_service
    await gs.start("s1", "in_1", NOW)
    await gs.daily(NOW + timedelta(days=8))
    with pytest.raises(HTTPException) as e:
        await calling.create_call_token(CallRequest(room="r1"), user_id="s1")
    assert e.value.status_code == 402
    with pytest.raises(clinic_service.ClinicRefused) as e:
        clinic_service.book("s1", "c1")
    assert e.value.reason == "payment_lapsed"
    with pytest.raises(clinic_service.ClinicRefused) as e:
        clinic_service.book_drop_in("s1", "c1")
    assert e.value.reason == "payment_lapsed"


def test_grace_email_renders():
    html = email_service._jinja_env.get_template("grace.html").render(
        subject="s", kind="lapsed", until="Mon 8 Feb, 12:00 (UTC)", web_base_url="https://tauka.io")
    assert "/account/subscription" in html and "download" in html and "held" in html
