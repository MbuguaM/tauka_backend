"""Make-ups after booking (enrolment plan §2f items 2-5, §103).

Pinned:
1. **The room** is made once, for the tutor, with the learners as participants;
   no Daily key → an empty link the tutor fills in, never a failed booking.
2. **The tutor sees names; a learner sees only the group size.**
3. **Attendance**: only the tutor (or ops), only once started, only seated
   learners, only the three marks.
4. **Reminders** go once, inside the day before, and not on top of a booked
   notice sent in the last 12 hours.
5. Times in email are the recipient's own zone.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hierarchy_service, makeup_delivery
from app.services.makeup_delivery import MakeupRefused

NOW = datetime(2027, 1, 20, 12, 0, tzinfo=timezone.utc)
TUTOR, S1, S2, OPS, STRANGER = "t1", "s1", "s2", "ops", "x"


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            items = p if isinstance(p, list) else [p]
            made = []
            for it in items:
                row = {"id": f"{self.name}-{len(rows) + 1}", **it}
                rows.append(row)
                made.append(row)
            return SimpleNamespace(data=made)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self, rows): self.rows = rows
    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


def _rows(start=NOW + timedelta(hours=6)):
    return {
        "makeup_session": [{"id": "m1", "course_id": "c", "tutor_id": TUTOR, "scheduled_at": start.isoformat(),
                            "due_by": (NOW + timedelta(days=5)).isoformat(), "duration_minutes": 20,
                            "status": "scheduled", "video_session_id": None,
                            "booked_notice_sent_at": None, "reminder_sent_at": None}],
        "makeup_session_participant": [
            {"makeup_session_id": "m1", "student_id": S1, "funded_by": "month", "status": "pending"},
            {"makeup_session_id": "m1", "student_id": S2, "funded_by": "credit", "status": "pending"},
        ],
        "user_profiles": [{"id": S1, "first_name": "Selam", "last_name": None, "timezone": "Africa/Nairobi"},
                          {"id": S2, "first_name": "Dawit", "last_name": None, "timezone": "America/New_York"},
                          {"id": TUTOR, "first_name": "Marta", "last_name": None, "timezone": "Europe/London"}],
        "tutor": [{"id": TUTOR}],
        "video_session": [], "video_session_participant": [], "session_credit": [],
    }


@pytest.fixture
def db():
    fake = _DB(_rows())
    with patch("app.services.makeup_delivery.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.makeup_delivery._now", return_value=NOW), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == OPS):
        yield fake


# 1. the room
async def test_room_is_made_once_with_the_learners(db):
    with patch("app.services.makeup_delivery.calling_service.create_daily_room",
               new=AsyncMock(return_value="https://x.daily.co/mu")) as room:
        vs = await makeup_delivery.provision_room("m1")
        again = await makeup_delivery.provision_room("m1")
    assert vs == again and room.await_count == 1
    v = db.rows["video_session"][0]
    assert v["tutor_id"] == TUTOR and v["duration_minutes"] == 20 and v["video_call_url"] == "https://x.daily.co/mu"
    assert {p["student_id"] for p in db.rows["video_session_participant"]} == {S1, S2}
    assert db.rows["makeup_session"][0]["video_session_id"] == vs


async def test_no_daily_key_still_makes_a_room_without_a_link(db):
    with patch("app.services.makeup_delivery.calling_service.create_daily_room", new=AsyncMock(return_value=None)):
        vs = await makeup_delivery.provision_room("m1")
    assert vs and db.rows["video_session"][0]["video_call_url"] is None


# 2. who sees what
def test_tutor_sees_names_learner_sees_group_size(db):
    t = makeup_delivery.for_tutor(TUTOR, NOW)[0]
    assert [l["name"] for l in t["learners"]] == ["Selam", "Dawit"] and not t["can_mark"]
    me = makeup_delivery.for_learner(S1, NOW)
    assert me["next"]["group_size"] == 2 and me["next"]["tutor_name"] == "Marta"
    assert "learners" not in me["next"] and "Dawit" not in str(me)


def test_proposed_make_up_shows_as_being_arranged(db):
    db.rows["makeup_session"][0].update({"status": "proposed", "scheduled_at": None})
    me = makeup_delivery.for_learner(S1, NOW)
    assert me["next"] is None and len(me["arranging"]) == 1


# 3. attendance
def test_attendance_rules(db):
    with pytest.raises(MakeupRefused) as e:
        makeup_delivery.mark_attendance(TUTOR, "m1", {S1: "attended"})
    assert e.value.reason == "not_started"
    db.rows["makeup_session"][0]["scheduled_at"] = (NOW - timedelta(minutes=30)).isoformat()
    for actor, marks, reason in [
        (STRANGER, {S1: "attended"}, "not_allowed"),
        (TUTOR, {"nobody": "attended"}, "not_in_session"),
        (TUTOR, {S1: "late"}, "invalid_mark"),
    ]:
        with pytest.raises(MakeupRefused) as e:
            makeup_delivery.mark_attendance(actor, "m1", marks)
        assert e.value.reason == reason
    out = makeup_delivery.mark_attendance(TUTOR, "m1", {S1: "attended", S2: "absent"})
    assert {l["status"] for l in out[0]["learners"]} == {"attended", "absent"}
    assert makeup_delivery.mark_attendance(OPS, "m1", {S2: "excused"})   # ops may correct


# 4. reminders
async def test_reminder_once_inside_the_day_before(db):
    with patch("app.services.makeup_delivery._email_of", return_value="a@b.c"), \
         patch("app.services.makeup_delivery.email_service.send_template_email",
               new=AsyncMock(return_value={"status": "sent"})) as send, \
         patch("app.services.makeup_delivery.provision_room", new=AsyncMock(return_value=None)):
        assert (await makeup_delivery.daily(NOW))["reminders"] == 1
        assert send.await_count == 3                         # two learners + the tutor
        assert (await makeup_delivery.daily(NOW))["reminders"] == 0


async def test_no_reminder_two_days_out_or_right_after_the_notice(db):
    with patch("app.services.makeup_delivery._email_of", return_value="a@b.c"), \
         patch("app.services.makeup_delivery.email_service.send_template_email", new=AsyncMock()) as send, \
         patch("app.services.makeup_delivery.provision_room", new=AsyncMock(return_value=None)):
        db.rows["makeup_session"][0]["scheduled_at"] = (NOW + timedelta(hours=48)).isoformat()
        assert (await makeup_delivery.daily(NOW))["reminders"] == 0
        db.rows["makeup_session"][0].update({"scheduled_at": (NOW + timedelta(hours=6)).isoformat(),
                                             "booked_notice_sent_at": (NOW - timedelta(hours=2)).isoformat()})
        assert (await makeup_delivery.daily(NOW))["reminders"] == 0
        send.assert_not_awaited()


# 5. local time
def test_local_time_uses_the_recipients_zone():
    at = datetime(2027, 1, 19, 15, 0, tzinfo=timezone.utc)
    assert makeup_delivery.local_time(at, "Africa/Nairobi") == "Tue 19 Jan, 18:00 (Africa/Nairobi)"
    assert makeup_delivery.local_time(at, "Not/AZone").endswith("15:00 (UTC)")


@pytest.mark.parametrize("kind,audience", [("booked", "learner"), ("reminder", "tutor")])
def test_email_renders(kind, audience):
    from app.services.email_service import _jinja_env
    html = _jinja_env.get_template("makeup.html").render(
        subject="s", kind=kind, audience=audience, when="Tue 19 Jan, 18:00 (Africa/Nairobi)",
        minutes=20, group_size=2, web_base_url="https://tauka.io")
    assert "Tue 19 Jan" in html and "2 learners" in html and "&lt;strong" not in html


# routes
def test_attendance_route_maps_refusals(client):
    from tests.conftest import make_token
    with patch("app.services.makeup_delivery.mark_attendance", side_effect=MakeupRefused("not_allowed")):
        r = client.post("/makeups/m1/attendance", json={"marks": {"s1": "attended"}},
                        headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "not_allowed"
    r = client.post("/makeups/m1/attendance", json={"marks": {"s1": "late"}},
                    headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 422


def test_mine_route_answers_for_the_caller(client):
    from tests.conftest import make_token, TEST_USER_ID
    with patch("app.services.makeup_delivery.mine", return_value={"as_tutor": None, "as_learner": {"next": None}}) as m:
        r = client.get("/makeups/mine", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 200 and m.call_args.args[0] == TEST_USER_ID


# §2f item 9: unmarked make-ups are reported
def test_unmarked_lists_ended_make_ups_with_a_pending_seat(db):
    db.rows["makeup_session"][0]["scheduled_at"] = (NOW - timedelta(hours=30)).isoformat()
    assert [r["id"] for r in makeup_delivery.unmarked(24, NOW)] == ["m1"]
    for s in db.rows["makeup_session_participant"]:
        s["status"] = "attended"
    assert makeup_delivery.unmarked(24, NOW) == []
    db.rows["makeup_session_participant"][0]["status"] = "pending"
    db.rows["makeup_session"][0]["scheduled_at"] = (NOW - timedelta(hours=2)).isoformat()
    assert makeup_delivery.unmarked(24, NOW) == []          # not yet a day late
