"""Gender as a matching preference or requirement (plan B8, §132; L-010).

Pinned:
1. Flag off: reads say disabled, writes are refused, the matcher gets nothing.
2. A requirement needs a stated gender; values are a closed set.
3. Matcher: a same-gender TUTOR requirement is a hard edge filter
   (NO_SAME_GENDER_MATCH); a same-gender POD requirement excludes mixed windows;
   a preference only scores, and scores nothing once soft criteria collapse.
"""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.services import cohort_matcher as cm
from app.services import gender_service as gs
from tests.fakedb import FakeDB

T = datetime(2027, 1, 9, 10, tzinfo=timezone.utc)


def _db(flag="ready"):
    return FakeDB({"feature_flag": [{"key": "gender_matching", "status": flag}], "gender_preference": []},
                  unique={"gender_preference": [("user_id",)]})


# 1
def test_flag_off():
    db = _db("coming")
    with patch("app.services.gender_service.db", db), patch("app.core.flags.db", db):
        assert gs.mine("u") == {"enabled": False}
        with pytest.raises(gs.GenderRefused) as e:
            gs.set_mine("u", "woman", "requirement", "none")
        assert e.value.reason == "not_enabled"
        assert gs.for_matching(["u"]) == {}


# 2
def test_set_and_read():
    db = _db()
    with patch("app.services.gender_service.db", db), patch("app.core.flags.db", db):
        with pytest.raises(gs.GenderRefused) as e:
            gs.set_mine("u", "prefer_not", "requirement", "none")
        assert e.value.reason == "gender_needed"
        with pytest.raises(gs.GenderRefused):
            gs.set_mine("u", "woman", "always", "none")
        out = gs.set_mine("u", "woman", "requirement", "preference")
        assert out == {"enabled": True, "gender": "woman", "same_gender_tutor": "requirement",
                       "same_gender_pod": "preference"}
        gs.set_mine("u", "woman", "none", "none")                     # upsert
        assert len(db.rows["gender_preference"]) == 1
        assert gs.for_matching(["u"])["u"]["same_gender_tutor"] == "none"


# 3
def _s(i, gender, tutor="none", pod="none"):
    return cm.StudentIn(id=i, tier="tutor_tier", level=1, current_lesson=1, tz="UTC", gender=gender,
                        same_gender_tutor=tutor, same_gender_pod=pod)


def _slot():
    return cm.TutorSlot(tutor_id="t1", start_utc=T, local_day_of_week=5, local_start_minute=600, tz="UTC")


def test_tutor_requirement_is_a_filter():
    cfg = cm.MatchConfig()
    tutor_m = cm.TutorIn(id="t1", min_level=1, max_level=6, max_concurrent_cohorts=3, gender="man")
    s = replace(_s("a", "woman", tutor="requirement"),
                windows=(cm.TimeWindow(day_of_week=T.weekday(), start_minute=0, end_minute=1440),))
    assert cm._student_fits_slot(s, _slot(), tutor_m, cfg) == cm.REASON_GENDER
    tutor_w = replace(tutor_m, gender="woman")
    assert cm._student_fits_slot(s, _slot(), tutor_w, cfg) is None
    pref = replace(s, same_gender_tutor="preference")
    assert cm._student_fits_slot(pref, _slot(), tutor_m, cfg) is None   # a preference never filters


def test_pod_requirement_and_preference():
    assert cm._genders_ok([_s("a", "woman", pod="requirement"), _s("b", "woman"), _s("c", "woman")])
    assert not cm._genders_ok([_s("a", "woman", pod="requirement"), _s("b", "man"), _s("c", "woman")])
    assert cm._genders_ok([_s("a", "woman", pod="preference"), _s("b", "man"), _s("c", "woman")])
    tutor = cm.TutorIn(id="t1", min_level=1, max_level=6, max_concurrent_cohorts=3, gender="woman")
    members = [_s("a", "woman", tutor="preference"), _s("b", "man"), _s("c", "man")]
    cfg = cm.MatchConfig()
    _, full = cm.score_candidate(members, _slot(), tutor, cfg, 5, 2.0)
    _, low = cm.score_candidate(members, _slot(), tutor, replace(cfg, soft_collapsed=True), 5, 2.0)
    assert full["gender"] == 10.0 and low["gender"] == 0.0
