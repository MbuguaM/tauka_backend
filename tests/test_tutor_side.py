"""The tutor side (enrolment plan 8.3, 8.4, 8.5, 8.7, 8.8; §134).

Pinned:
1. 8.3 commitment: at least 6 h, no more than the weekly availability, no overlap.
   Holidays: 8 weeks ahead -> approved; shorter -> needs ops; on_holiday reads
   approved only.
2. 8.4 line_amount prices a 25-minute 1:1 at half, reviews and gatherings by
   their type rates; nothing changes without type rates.
3. 8.5 E-11: 10% of what referred learners paid, within 12 months, same currency.
4. 8.7 QA: one upcoming pod session per active tutor a month, a rung >= 3
   reviewer who isn't them; rubric 1-4 for every item; done pays the reviewer.
5. 8.8 a profile is public only when published and the flag is on; slug unique.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services import hierarchy_service
from app.services import payout_service as ps
from app.services import tutor_ops_service as svc
from app.services.tutor_ops_service import TutorOpsRefused
from tests.fakedb import FakeDB

TODAY = date(2027, 1, 4)
NOW = datetime(2027, 1, 4, 9, tzinfo=timezone.utc)


def _db(**rows):
    base = {"tutor": [{"id": t, "status": "active"} for t in ("t1", "t2", "t3", "t4")],
            "tutor_availability": [{"tutor_id": "t1", "active": True, "start_time": "09:00", "end_time": "13:00"},
                                   {"tutor_id": "t1", "active": True, "start_time": "14:00", "end_time": "18:00"}],
            "tutor_commitment_block": [], "tutor_holiday": [], "qa_review": [], "tutor_work_item": [],
            "tutor_public_profile": [], "feature_flag": [], "cohort_session": [], "tutor_teaching_profile": []}
    base.update(rows)
    return FakeDB(base, unique={"qa_review": [("tutor_id", "month")], "tutor_public_profile": [("tutor_id",)]})


@pytest.fixture
def db():
    fake = _db()
    with patch("app.services.tutor_ops_service.db", fake), patch("app.core.flags.db", fake), \
         patch("app.services.tutor_ops_service._today", return_value=TODAY), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: {"t3": 3, "t4": 4}.get(t, 1)), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: f"Name {i} Surname" for i in ids}):
        yield fake


# 1
def test_commitment_and_holidays(db):
    with pytest.raises(TutorOpsRefused) as e:
        svc.commit("t1", TODAY, 5)
    assert e.value.reason == "too_few_hours"
    with pytest.raises(TutorOpsRefused) as e:
        svc.commit("t1", TODAY, 9)                                   # 8 h available
    assert e.value.reason == "more_than_available"
    b = svc.commit("t1", TODAY, 8)
    assert b["ends_on"] == (TODAY + timedelta(weeks=5)).isoformat()
    with pytest.raises(TutorOpsRefused):
        svc.commit("t1", TODAY + timedelta(weeks=2), 6)               # overlaps
    far = svc.declare_holiday("t1", TODAY + timedelta(weeks=9), TODAY + timedelta(weeks=10))
    near = svc.declare_holiday("t2", TODAY + timedelta(days=10), TODAY + timedelta(days=12))
    assert (far["status"], near["status"]) == ("approved", "needs_approval")
    assert svc.on_holiday(["t1", "t2"], TODAY + timedelta(days=11)) == set()
    assert svc.on_holiday(["t1", "t2"], TODAY + timedelta(weeks=9, days=1)) == {"t1"}
    svc.decide_holiday("ops", near["id"], True)
    assert svc.on_holiday(["t2"], TODAY + timedelta(days=11)) == {"t2"}
    with pytest.raises(TutorOpsRefused):
        svc.decide_holiday("t1", near["id"], True)


# 2
def test_type_rates():
    counts = {"cohort": 1, "one_to_one": 2, "one_to_one_25": 1, "review_item": 3, "gathering": 1}
    bp = {"one_to_one_25": 5000, "review_item": 1000, "gathering": 15000}
    # 1 pod + 1×50 + 0.5×(25) + 3×10% + 150% = 1 + 1 + 0.5 + 0.3 + 1.5 = 4.3 sessions
    assert ps.line_amount(counts, 1000, bp) == 4300
    assert ps.line_amount(counts, 1000) == 3000                       # no type rates: as before


# 3
def test_commission_is_ten_percent_for_twelve_months():
    fake = FakeDB({
        "referral_signup": [{"tutor_id": "t1", "student_id": "a", "signed_up_at": "2026-02-10T00:00:00+00:00"},
                            {"tutor_id": "t1", "student_id": "b", "signed_up_at": "2025-12-01T00:00:00+00:00"}],
        "student_payment": [
            {"student_id": "a", "amount_minor": 7500, "currency": "USD", "paid_at": "2027-01-05T00:00:00+00:00"},
            {"student_id": "a", "amount_minor": 9700, "currency": "KES", "paid_at": "2027-01-06T00:00:00+00:00"},
            {"student_id": "b", "amount_minor": 7500, "currency": "USD", "paid_at": "2027-01-07T00:00:00+00:00"},
            {"student_id": "a", "amount_minor": 7500, "currency": "USD", "paid_at": "2026-12-05T00:00:00+00:00"},
        ]})
    with patch.object(ps, "db", fake):
        got = ps._referral_commission_minor("t1", date(2027, 1, 1), date(2027, 1, 31), "USD", 1000, 12)
    assert got == 750                                                 # only a's January USD payment; b is past 12 months


# 4
def test_qa_sampling_and_submit(db):
    soon = (NOW + timedelta(days=3)).isoformat()
    db.rows["cohort_session"] = [{"id": f"s-{t}", "lead_tutor_id": t, "status": "scheduled", "scheduled_at": soon}
                                 for t in ("t1", "t2", "t3")]
    out = svc.sample_month(NOW)
    assert out == {"assigned": 3, "no_reviewer": 0, "no_session": 1}  # t4 leads nothing
    reviews = {r["tutor_id"]: r["reviewer_id"] for r in db.rows["qa_review"]}
    assert all(t != r and r in ("t3", "t4") for t, r in reviews.items())
    assert reviews["t3"] == "t4"
    assert svc.sample_month(NOW)["assigned"] == 0                     # once a month
    rid = db.rows["qa_review"][0]["id"]
    reviewer = db.rows["qa_review"][0]["reviewer_id"]
    good = {k: 3 for k in svc.RUBRIC}
    with pytest.raises(TutorOpsRefused):
        svc.submit_qa(reviewer, rid, {**good, "warmth": 5})
    with pytest.raises(TutorOpsRefused):
        svc.submit_qa("t1" if reviewer != "t1" else "t2", rid, good)
    assert svc.submit_qa(reviewer, rid, good)["status"] == "done"
    assert db.rows["tutor_work_item"][0]["kind"] == "qa_observation"
    assert svc.my_qa(db.rows["qa_review"][0]["tutor_id"])["about_me"][0]["scores"] == good


# 5
def test_public_profile(db):
    with pytest.raises(TutorOpsRefused) as e:
        svc.save_profile("t1", "Bad Slug!", None, None, False)
    assert e.value.reason == "bad_slug"
    with pytest.raises(TutorOpsRefused) as e:
        svc.save_profile("t1", "marta-k", "Amharic, warmly", "Hello", True)
    assert e.value.reason == "not_enabled"
    mine = svc.save_profile("t1", "marta-k", "Amharic, warmly", "Hello", False)
    assert mine["first_name"] == "Name" and "Surname" not in str(mine) and mine["published"] is False
    assert svc.public_profile("marta-k") is None
    db.rows["feature_flag"].append({"key": "tutor_public_profiles", "status": "ready"})
    svc.save_profile("t1", "marta-k", "Amharic, warmly", "Hello", True)
    assert svc.public_profile("marta-k")["headline"] == "Amharic, warmly"
    assert [t["slug"] for t in svc.public_index()] == ["marta-k"]
    with pytest.raises(TutorOpsRefused) as e:
        svc.save_profile("t2", "marta-k", None, None, False)
    assert e.value.reason == "slug_taken"
