"""Wait-pool drop-ins (enrolment plan 3.7, §115).

Pinned (developer decisions 2026-10-18):
1. Ops schedules a drop-in as a clinic of kind `drop_in`: 50 minutes, up to 10.
2. Bookable by a paying learner with no pod (not paused, not Free), in their
   band, ONE A WEEK (Monday-Sunday in the band's anchor zone).
3. Drop-ins and Phase 0 clinics don't eat into each other: the Phase 0
   allowance counts clinics only, and Phase 0 lists never show drop-ins.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import clinic_service, hierarchy_service
from app.services.clinic_service import ClinicRefused
from tests.test_clinics import _DB, CAPS, NOW, OPS, RUNG, TUTOR

# NOW is Tue 29 Dec 2026, 12:00 UTC. ea_gulf's anchor week runs Mon 28 Dec - Sun 3 Jan.
THU = (NOW + timedelta(days=2)).isoformat()
SAT = (NOW + timedelta(days=4)).isoformat()
NEXT_TUE = (NOW + timedelta(days=7)).isoformat()


def _drop(cid, at, band="ea_gulf", capacity=10):
    return {"id": cid, "kind": "drop_in", "band": band, "tutor_id": TUTOR, "starts_at": at,
            "duration_minutes": 50, "capacity": capacity, "status": "scheduled", "video_session_id": None}


@pytest.fixture
def db():
    fake = _DB()
    fake.rows["clinic"] += [_drop("d1", THU), _drop("d2", SAT), _drop("d3", NEXT_TUE), _drop("d-eu", THU, "europe")]
    fake.rows["student"] += [{"id": "wait", "tier": "tutor_tier"}, {"id": "podded", "tier": "tutor_tier"},
                             {"id": "free", "tier": "free_tier"}, {"id": "resting", "tier": "tutor_tier"}]
    fake.rows["enrolment_state"] += [{"student_id": s, "state": st, "band": "ea_gulf"} for s, st in
                                     (("wait", "wait_pool"), ("podded", "in_pod"), ("free", "phase0"),
                                      ("resting", "paused"))]
    fake.rows["cohort_memberships"] = [{"cohort_id": "pod1", "user_id": "podded", "status": "active"}]
    fake.rows["enrolment_pause"] = [{"id": "p1", "student_id": "resting", "status": "active"}]
    with patch("app.services.clinic_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch("app.services.makeup_delivery.db", fake), \
         patch("app.services.clinic_service._now", return_value=NOW), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: c in CAPS.get(a, set())), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: RUNG.get(t)), \
         patch("app.services.clinic_service.calling_service.create_daily_room", new=AsyncMock(return_value=None)):
        yield fake


# 1
@pytest.mark.asyncio
async def test_schedule_a_drop_in(db):
    made = await clinic_service.create(OPS, "ea_gulf", TUTOR, (NOW + timedelta(days=3)).isoformat(), 2,
                                       kind="drop_in")
    assert [(c["kind"], c["duration_minutes"], c["capacity"]) for c in made] == [("drop_in", 50, 10)] * 2
    assert db.rows["video_session"][-1]["title"] == "Drop-in session"
    with pytest.raises(ClinicRefused) as e:
        await clinic_service.create(OPS, "ea_gulf", TUTOR, (NOW + timedelta(days=3)).isoformat(), 1, 12, "drop_in")
    assert e.value.reason == "invalid_capacity"


# 2
@pytest.mark.parametrize("who,reason", [("podded", "in_a_pod"), ("free", "not_paying"), ("resting", "paused")])
def test_who_may_book(db, who, reason):
    view = clinic_service.drop_ins_for_learner(who)
    assert view["eligible"] is False and view["reason"] == reason and view["open"] == []
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book(who, "d1")
    assert e.value.reason == reason


def test_one_a_week(db):
    view = clinic_service.drop_ins_for_learner("wait")
    assert [c["id"] for c in view["open"]] == ["d1", "d2", "d3"]           # own band only
    view = clinic_service.book("wait", "d1")
    assert [b["id"] for b in view["booked"]] == ["d1"]
    assert {c["id"]: c["week_used"] for c in view["open"]} == {"d2": True, "d3": False}
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book("wait", "d2")                                  # same Mon-Sun week
    assert e.value.reason == "week_used"
    clinic_service.book("wait", "d3")                                      # next week is fine
    with pytest.raises(ClinicRefused) as e:
        clinic_service.book("wait", "d-eu")
    assert e.value.reason == "other_band"


def test_cancelling_frees_the_week(db):
    clinic_service.book("wait", "d1")
    view = clinic_service.cancel_booking("wait", "d1")
    assert "eligible" in view and view["booked"] == []                     # the drop-in view comes back
    clinic_service.book("wait", "d2")


# 3
def test_kinds_dont_mix(db):
    from tests.test_clinics import STU
    # A Phase 0 learner who also has a paid tier and no pod may take a drop-in…
    clinic_service.book(STU, "d1")
    # …and it does not use their Phase 0 clinic allowance (Learner: 1).
    view = clinic_service.for_learner(STU)
    assert view["allowance"]["used"] == 0
    assert all(c["id"].startswith("c") for c in view["open"])              # no drop-ins in the Phase 0 list
    assert view["booked"] == []


def test_reminder_wording():
    from app.services import email_service
    html = email_service._jinja_env.get_template("clinic.html").render(
        subject="s", kind="drop_in", when="Thu 31 Dec, 15:00 (Africa/Nairobi)", minutes=50)
    assert "Drop-in session" in html and "doesn" in html and "Fidel" not in html
