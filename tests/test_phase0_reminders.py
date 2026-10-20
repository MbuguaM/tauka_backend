"""Phase 0 reminders by email (enrolment plan 1.3, §127).

Pinned:
1. pick(): deadline_3 (0-3 days), deadline_14 (4-14), else weekly on every 7th
   day since the start; nothing otherwise, nothing after the deadline.
2. Daily: only learners in phase0/paid, unfinished, reminders on, with a target;
   claimed once (a second run sends nothing); a failed send is un-claimed.
3. The off link: a wrong token does nothing; the right one turns them off.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import email_service
from app.services import phase0_reminder_service as pr

TODAY = date(2026, 12, 25)
DEADLINE = "2027-01-05"                      # 11 days on


# 1
@pytest.mark.parametrize("started,deadline,today,kind", [
    (date(2026, 12, 1), date(2027, 1, 5), date(2027, 1, 3), "deadline_3"),
    (date(2026, 12, 1), date(2027, 1, 5), date(2027, 1, 5), "deadline_3"),
    (date(2026, 12, 1), date(2027, 1, 5), date(2026, 12, 25), "deadline_14"),
    (date(2026, 11, 1), date(2027, 1, 5), date(2026, 11, 15), "weekly_2"),
    (date(2026, 11, 1), date(2027, 1, 5), date(2026, 11, 16), None),
    (date(2026, 11, 1), date(2027, 1, 5), date(2026, 11, 4), None),
    (date(2026, 11, 1), date(2027, 1, 5), date(2027, 1, 10), "weekly_10"),   # after the deadline: weekly only
    (date(2026, 11, 1), None, date(2026, 11, 8), "weekly_1"),
])
def test_pick(started, deadline, today, kind):
    assert pr.pick(started, deadline, today) == kind


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
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            key = (p["student_id"], p["intake_id"], p["kind"])
            if any((r["student_id"], r["intake_id"], r["kind"]) == key for r in rows):
                raise Exception("duplicate key phase0_reminder_pkey")
            rows.append(dict(p))
            return SimpleNamespace(data=[p])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        if kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        return SimpleNamespace(data=[dict(r) for r in hit])


def _row(sid, **kw):
    return {"id": f"e-{sid}", "student_id": sid, "course_id": "c1", "state": "phase0", "created_at": "2026-12-01",
            "phase0_started_at": "2026-12-01", "phase0_completed_at": None, "reminders_off_at": None,
            "target_intake_id": "tir", **kw}


class _DB:
    def __init__(self):
        self.rows = {
            "enrolment_state": [
                _row("a"),
                _row("done", phase0_completed_at="2026-12-20"),
                _row("off", reminders_off_at="2026-12-10"),
                _row("podded", state="in_pod"),
                _row("none", target_intake_id=None),
            ],
            "phase0_reminder": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    sent = AsyncMock(return_value={"status": "sent"})
    target = {"ethiopic_month": 5, "ec_year": 2019, "cutoff_closes": DEADLINE}
    with patch("app.services.phase0_reminder_service.db", fake), \
         patch("app.services.phase0_service._sync", side_effect=lambda r: r), \
         patch("app.services.phase0_service._present", side_effect=lambda r: {"target_intake": target}), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch.object(email_service, "send_template_email", sent):
        fake.sent = sent
        yield fake


# 2
@pytest.mark.asyncio
async def test_daily_sends_once_to_the_right_people(db):
    assert (await pr.daily(TODAY))["sent"] == 1
    call = db.sent.await_args
    assert call.args[0] == "a@x.test" and call.args[2]["kind"] == "deadline_14"
    assert call.args[2]["intake"] == "Tir 2019" and call.args[2]["deadline"] == "Tue 5 Jan"
    assert pr.off_token("a") in call.args[2]["off_url"]
    assert db.rows["phase0_reminder"] == [{"student_id": "a", "intake_id": "tir", "kind": "deadline_14"}]
    assert (await pr.daily(TODAY))["sent"] == 0                                    # claimed


@pytest.mark.asyncio
async def test_failed_send_is_unclaimed(db):
    db.sent.return_value = {"status": "failed"}
    assert (await pr.daily(TODAY))["sent"] == 0
    assert db.rows["phase0_reminder"] == []


# 3
def test_off_link(db):
    assert pr.turn_off("a", "nope") is False
    assert db.rows["enrolment_state"][0]["reminders_off_at"] is None
    assert pr.turn_off("a", pr.off_token("a")) is True
    assert db.rows["enrolment_state"][0]["reminders_off_at"]


def test_email_renders():
    html = email_service._jinja_env.get_template("phase0_reminder.html").render(
        subject="s", kind="deadline_3", intake="Tir 2019", deadline="Tue 5 Jan", days_left=2,
        off_url="https://api.tauka.io/enrolment/reminders/off?s=a&t=x", web_base_url="https://tauka.io")
    assert "2 days left" in html and "Stop these reminders" in html and "test-out" in html
