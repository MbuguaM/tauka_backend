"""Phase 0 clinics (enrolment plan 1.5, §105).

Pinned:
1. **Scheduling** is ops-only, with an active tutor of rung >= 2, in the future;
   "weekly for N weeks" makes N clinics, each with a room.
2. **Booking**: only while in Phase 0, within the tier's allowance (Learner 1,
   Tutor 2, Intensive 2, Free 0 — attended and missed both count), in the
   learner's own band; a cancelled seat can be re-booked; the database's "full"
   and "not open" become words.
3. **Cancelling a booking** only before the start, and it leaves the room.
4. **Attendance**: the clinic's tutor or ops, after the start, held seats only.
5. **Reminders** once per booking, inside the day before.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import clinic_service, hierarchy_service
from app.services.clinic_service import ClinicRefused

NOW = datetime(2026, 12, 29, 12, 0, tzinfo=timezone.utc)
OPS, TUTOR, RUNG1, STU, OTHER = "ops", "t2", "t1", "s1", "s2"
CAPS = {OPS: {"ops"}}
RUNG = {TUTOR: 2, RUNG1: 1}


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            made = []
            for it in (p if isinstance(p, list) else [p]):
                if self.name == "clinic_booking":
                    self.db.guard_insert(it)
                row = {"id": f"{self.name}-{len(rows) + 1}", "status": "booked" if self.name == "clinic_booking" else "scheduled",
                       "duration_minutes": 30, "capacity": 15, "video_session_id": None, **it}
                rows.append(row)
                made.append(row)
            return SimpleNamespace(data=made)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        start = (NOW + timedelta(days=2)).isoformat()
        self.rows = {
            "clinic": [{"id": "c1", "band": "ea_gulf", "tutor_id": TUTOR, "starts_at": start, "duration_minutes": 30,
                        "capacity": 15, "status": "scheduled", "video_session_id": "v1"},
                       {"id": "c2", "band": "ea_gulf", "tutor_id": TUTOR, "starts_at": (NOW + timedelta(days=5)).isoformat(),
                        "duration_minutes": 30, "capacity": 15, "status": "scheduled", "video_session_id": None},
                       {"id": "c3", "band": "europe", "tutor_id": TUTOR, "starts_at": start, "duration_minutes": 30,
                        "capacity": 15, "status": "scheduled", "video_session_id": None}],
            "clinic_booking": [],
            "student": [{"id": STU, "tier": "learner_tier"}, {"id": OTHER, "tier": "tutor_tier"}],
            "enrolment_state": [{"student_id": STU, "state": "paid", "band": "ea_gulf", "phase0_completed_at": None},
                                {"student_id": OTHER, "state": "paid", "band": "ea_gulf", "phase0_completed_at": None}],
            "tutor": [{"id": TUTOR, "status": "active", "deleted_at": None}, {"id": RUNG1, "status": "active", "deleted_at": None}],
            "user_profiles": [{"id": TUTOR, "first_name": "Marta", "last_name": None, "timezone": "Africa/Nairobi"},
                              {"id": STU, "first_name": "Selam", "last_name": None, "timezone": "Africa/Nairobi"}],
            "video_session": [], "video_session_participant": [],
        }
        self.full = False

    def guard_insert(self, it):
        if self.full:
            raise Exception("clinic_booking: clinic is full")

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.clinic_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.makeup_delivery.db", fake), \
         patch("app.services.clinic_service._now", return_value=NOW), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: c in CAPS.get(a, set())), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: RUNG.get(t)), \
         patch("app.services.clinic_service.calling_service.create_daily_room", new=AsyncMock(return_value=None)):
        yield fake


# 1
async def test_scheduling_rules(db):
    start = (NOW + timedelta(days=3)).isoformat()
    for actor, tutor, at, reason in [(TUTOR, TUTOR, start, "not_allowed"), (OPS, RUNG1, start, "tutor_rung"),
                                     (OPS, TUTOR, (NOW - timedelta(hours=1)).isoformat(), "in_the_past")]:
        with pytest.raises(ClinicRefused) as e:
            await clinic_service.create(actor, "ea_gulf", tutor, at)
        assert e.value.reason == reason
    made = await clinic_service.create(OPS, "ea_gulf", TUTOR, start, repeat_weeks=3)
    assert len(made) == 3
    assert [m["starts_at"][:10] for m in made] == ["2027-01-01", "2027-01-08", "2027-01-15"]
    assert len(db.rows["video_session"]) == 3          # a room each (no Daily URL here)


# 2
def test_learner_sees_band_clinics_and_allowance(db):
    v = clinic_service.for_learner(STU)
    assert v["band"] == "ea_gulf" and v["allowance"] == {"allowed": 1, "used": 0, "left": 1}
    assert [c["id"] for c in v["open"]] == ["c1", "c2"] and v["open"][0]["seats_left"] == 15


def test_booking_allowance_band_and_rebook(db):
    v = clinic_service.book(STU, "c1")
    assert v["booked"][0]["id"] == "c1" and v["allowance"]["left"] == 0
    assert [(p["session_id"], p["student_id"]) for p in db.rows["video_session_participant"]] == [("v1", STU)]
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book(STU, "c2")
    assert e.value.reason == "allowance_used"          # Learner: one
    clinic_service.cancel_booking(STU, "c1")
    assert db.rows["video_session_participant"] == []
    assert clinic_service.book(STU, "c1")["booked"]    # re-book a cancelled seat
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book(OTHER, "c3")
    assert e.value.reason == "other_band"


@pytest.mark.parametrize("change,reason", [
    (lambda db: db.rows["student"][0].update(tier="free_tier"), "not_included"),
    (lambda db: db.rows["enrolment_state"][0].update(state="seat_confirmed", phase0_completed_at="x"), "not_in_phase0"),
    (lambda db: setattr(db, "full", True), "full"),
])
def test_booking_refusals(db, change, reason):
    change(db)
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book(STU, "c1")
    assert e.value.reason == reason


def test_missed_clinic_still_counts(db):
    db.rows["clinic_booking"].append({"clinic_id": "c9", "student_id": STU, "status": "absent"})
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book(STU, "c1")
    assert e.value.reason == "allowance_used"


# 3
def test_cannot_cancel_after_start(db):
    clinic_service.book(STU, "c1")
    db.rows["clinic"][0]["starts_at"] = (NOW - timedelta(minutes=1)).isoformat()
    with pytest.raises(ClinicRefused) as e:
        clinic_service.cancel_booking(STU, "c1")
    assert e.value.reason == "started"


# 4
def test_attendance_rules(db):
    clinic_service.book(STU, "c1")
    with pytest.raises(ClinicRefused) as e:
        clinic_service.mark(TUTOR, "c1", {STU: "attended"})
    assert e.value.reason == "not_started"
    db.rows["clinic"][0]["starts_at"] = (NOW - timedelta(minutes=5)).isoformat()
    for actor, marks, reason in [(OTHER, {STU: "attended"}, "not_allowed"),
                                 (TUTOR, {OTHER: "attended"}, "not_in_clinic"),
                                 (TUTOR, {STU: "excused"}, "invalid_mark")]:
        with pytest.raises(ClinicRefused) as e:
            clinic_service.mark(actor, "c1", marks)
        assert e.value.reason == reason
    out = clinic_service.mark(TUTOR, "c1", {STU: "attended"})
    seat = next(c for c in out if c["id"] == "c1")["learners"][0]
    assert seat == {"student_id": STU, "name": "Selam", "status": "attended"}


# 5
async def test_reminder_once_inside_the_day_before(db):
    db.rows["clinic"][0]["starts_at"] = (NOW + timedelta(hours=20)).isoformat()
    clinic_service.book(STU, "c1")
    with patch("app.services.clinic_service._email_of", return_value="a@b.c"), \
         patch("app.services.clinic_service.email_service.send_template_email", new=AsyncMock()) as send:
        assert (await clinic_service.daily(NOW))["reminders"] == 1
        assert (await clinic_service.daily(NOW))["reminders"] == 0
    assert send.await_count == 1 and send.call_args.args[1] == "clinic"


def test_email_renders():
    from app.services.email_service import _jinja_env
    html = _jinja_env.get_template("clinic.html").render(
        subject="s", when="Tue 29 Dec, 19:30 (Africa/Nairobi)", minutes=30, web_base_url="x")
    assert "Tue 29 Dec" in html and "muted" in html


# routes
def test_route_maps_refusals(client):
    from tests.conftest import make_token
    with patch("app.services.clinic_service.book", side_effect=ClinicRefused("allowance_used")):
        r = client.post("/clinics/c1/book", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "allowance_used"


async def test_weekly_repeat_keeps_the_band_wall_clock_across_a_clock_change(db):
    from datetime import datetime as dt
    with patch("app.services.clinic_service._now", return_value=dt(2026, 10, 18, tzinfo=timezone.utc)):
        # Sat 24 Oct 10:00 London (BST, 09:00 UTC); Europe changes on 25 Oct.
        made = await clinic_service.create(OPS, "europe", TUTOR, "2026-10-24T09:00:00+00:00", repeat_weeks=2)
    assert [m["starts_at"] for m in made] == ["2026-10-24T09:00:00+00:00", "2026-10-31T10:00:00+00:00"]


def test_unmarked_and_empty_clinics(db):
    clinic_service.book(STU, "c1")
    db.rows["clinic"][0]["starts_at"] = (NOW - timedelta(hours=30)).isoformat()   # booked, unmarked
    db.rows["clinic"][1]["starts_at"] = (NOW - timedelta(hours=2)).isoformat()    # nobody booked
    assert [r["id"] for r in clinic_service.unmarked(24, NOW)] == ["c1"]
    assert clinic_service.cancel_empty(NOW) == 1
    c2 = next(c for c in db.rows["clinic"] if c["id"] == "c2")
    assert (c2["status"], c2["cancelled_reason"]) == ("cancelled", "no_bookings")
    assert next(c for c in db.rows["clinic"] if c["id"] == "c1")["status"] == "scheduled"
