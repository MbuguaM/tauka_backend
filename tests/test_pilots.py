"""Track 10 pilots and the last remainders (§140).

Pinned:
1. Holiday dates (Enkutatash, Meskel, Genna, Timket, Fasika) and the window.
2. Gatherings: ops only; RSVP within capacity; done pays the host.
3. A mid-month intake is band-only, Ethiopic day 15; phase0 targets it only for that band.
4. Co-tutor: rung 2+, not the lead; both paid for the clinic.
5. Clinic-mates: 2+ attended clinics together; first names only.
6. Second review: 1 in 5 of rung-1 reviews, deterministic; agree/disagree; calibration.
"""
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from app.services import hierarchy_service
from app.services import phase0_service
from app.services import pilots_service as ps
from app.services.pilots_service import PilotRefused
from tests.fakedb import FakeDB


# 1
def test_holiday_dates():
    d = ps.holiday_dates(2027)
    assert d["enkutatash"] == date(2027, 9, 12) and d["meskel"] == date(2027, 9, 28)   # after leap 2019 E.C.
    assert d["genna"] == date(2027, 1, 7) and d["timket"] == date(2027, 1, 19)
    assert ps.orthodox_easter(2027) == date(2027, 5, 2) and ps.orthodox_easter(2026) == date(2026, 4, 12)
    assert ps.current_holiday(date(2027, 9, 20))[0] == "meskel"
    assert ps.current_holiday(date(2027, 7, 1)) is None


def test_holiday_lesson_served():
    db = FakeDB({"holiday_lesson": [{"id": "h", "holiday": "timket", "title": "Timket words", "body": "x" * 20,
                                     "active": True, "created_at": "2027-01-01"}]})
    with patch("app.services.pilots_service.db", db):
        assert ps.holiday_lesson(date(2027, 1, 10))["title"] == "Timket words"
        assert ps.holiday_lesson(date(2027, 3, 1)) is None


@pytest.fixture
def db():
    fake = FakeDB({"gathering": [], "gathering_rsvp": [], "tutor_work_item": [], "broadcast": [], "intake": [],
                   "clinic": [{"id": "c1", "tutor_id": "t1", "status": "scheduled"}],
                   "clinic_booking": [], "fidel_approval": []},
                  unique={"gathering_rsvp": [("gathering_id", "student_id")]})
    with patch("app.services.pilots_service.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: {"r1": 1, "t2": 2, "t1": 2}.get(t, 0)), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: f"First{i} Last" for i in ids}):
        yield fake


# 2
def test_gatherings(db):
    at = datetime(2027, 3, 1, 15, tzinfo=timezone.utc)
    with pytest.raises(PilotRefused):
        ps.create_gathering("x", "meetup", "Coffee", "Nairobi", at, 2)
    g = ps.create_gathering("ops", "meetup", "Coffee", "Nairobi", at, 2, host_tutor_id="t1", season_id="bega")
    assert db.rows["broadcast"][0]["target_type"] == "season"
    ps.rsvp("a", g["id"], True)
    ps.rsvp("b", g["id"], True)
    with pytest.raises(PilotRefused) as e:
        ps.rsvp("c", g["id"], True)
    assert e.value.reason == "full"
    ps.rsvp("b", g["id"], False)
    ps.rsvp("c", g["id"], True)
    listed = ps.gatherings("c", datetime(2027, 2, 1, tzinfo=timezone.utc))[0]
    assert (listed["going"], listed["mine"]) == (2, True)
    ps.finish_gathering("ops", g["id"])
    assert db.rows["tutor_work_item"][0]["kind"] == "gathering"


# 3
def test_mid_month_band_intake(db):
    row = ps.open_mid_month("ops", "ea_gulf", 2019, 6, today=date(2026, 10, 19))
    assert row["band"] == "ea_gulf" and row["formation_date"] == "2027-02-22"     # Yekatit 15, 2019
    intakes = [{"id": "all", "formation_date": "2027-03-10", "cutoff_closes": "2027-03-01", "mode": "live",
                "status": "scheduled"}, {**row, "id": "mid"}]

    def pick(band):
        return phase0_service.choose_target(intakes, started=date(2027, 1, 1), today=date(2027, 1, 1),
                                            completed=None, band=band)
    assert pick("ea_gulf")["id"] == "mid" and pick("europe")["id"] == "all"


# 4
def test_co_tutor(db):
    with pytest.raises(PilotRefused):
        ps.set_co_tutor("ops", "c1", "t1")                 # the lead
    with pytest.raises(PilotRefused):
        ps.set_co_tutor("ops", "c1", "r1")                 # rung 1
    assert ps.set_co_tutor("ops", "c1", "t2")["co_tutor_id"] == "t2"


def test_both_tutors_paid():
    from app.services import payout_service
    fake = FakeDB({"clinic": [{"tutor_id": "t1", "co_tutor_id": "t2", "delivered_minutes": 30, "status": "completed",
                               "starts_at": "2027-01-10T10:00:00+00:00"}],
                   "cohort_session": [], "one_to_one_session": [], "makeup_session": [], "fidel_approval": [],
                   "tutor_work_item": []})
    with patch.object(payout_service, "db", fake):
        out = payout_service.delivered_sessions(date(2027, 1, 1), date(2027, 1, 31))
    assert out["t1"]["clinic"] == 1 and out["t2"]["clinic"] == 1


# 5
def test_clinic_mates(db):
    db.rows["clinic_booking"] = [{"clinic_id": c, "student_id": s, "status": "attended"}
                                 for c in ("k1", "k2") for s in ("me", "x")] + \
                                [{"clinic_id": "k1", "student_id": "y", "status": "attended"}]
    assert ps.clinic_mates("me") == [{"first_name": "Firstx", "clinics_together": 2}]


# 6
def test_second_review(db):
    ids = [f"a{i}" for i in range(40)]
    sampled = [i for i in ids if ps.sampled(i)]
    assert 0 < len(sampled) < 20 and ps.sampled(sampled[0]) is True
    a = sampled[0]
    db.rows["fidel_approval"] = [{"id": a, "status": "approved", "reviewed_by": "r1", "second_status": None}]
    assert ps.mark_for_second_review(a, "r1") is True
    assert ps.mark_for_second_review(a, "t2") is False                  # only rung-1 reviews are sampled
    with pytest.raises(PilotRefused):
        ps.second_queue("r1")
    assert [r["id"] for r in ps.second_queue("t2")] == [a]
    assert ps.second_review("t2", a, False)["second_status"] == "disagree"
    cal = ps.calibration("ops")
    assert cal == [{"tutor_id": "r1", "name": "Firstr1 Last", "checked": 1, "agreement": 0.0}]
