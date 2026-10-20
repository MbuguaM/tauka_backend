"""Unit assessments (§141).

Pinned:
1. Blocks: block k = lessons 10k-9..10k, reached at lesson 10k, at most six.
2. Bands and outcome: quiz >= 80 % / 60 %, speaking mean >= 3 / 2; the weaker wins;
   no outcome until both parts are in.
3. Opening: reaching lesson 10 opens block 1 once, with its ten lessons (content
   units only); reaching 20 opens block 2.
4. The quiz: only the learner, once, a sane score; weak lessons kept inside the block.
5. Speaking: a linked tutor, the pod's tutor or ops; not the learner, not another
   tutor; once; four scores 1-4. The tutor's queue lists their learners only.
6. Tracker: a not_yet adds two weeks and makes the review the fix; secure changes nothing.
7. Checkpoint: the latest assessment rides on the member, with a flag on not_yet.
"""
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from app.services import hierarchy_service
from app.services import tracker_service
from app.services import unit_assessment_service as ua
from app.services.unit_assessment_service import UnitAssessmentRefused
from tests.fakedb import FakeDB

COURSE = "course-1"
SCORES = {"pronunciation": 3, "accuracy": 3, "fluency": 3, "comprehension": 3}


# 1
def test_blocks():
    assert ua.block_span(1) == (1, 10) and ua.block_span(6) == (51, 60)
    assert ua.blocks_reached(9) == [] and ua.blocks_reached(10) == [1]
    assert ua.blocks_reached(25) == [1, 2] and ua.blocks_reached(80) == [1, 2, 3, 4, 5, 6]


# 2
@pytest.mark.parametrize("correct,total,band", [(16, 20, "secure"), (15, 20, "developing"),
                                                (12, 20, "developing"), (11, 20, "not_yet")])
def test_quiz_band(correct, total, band):
    assert ua.quiz_band(correct, total) == band


def test_outcome_is_the_weaker_part():
    row = {"quiz_total": 20, "quiz_correct": 18, "speaking": None}
    assert ua.outcome(row) is None
    assert ua.outcome({**row, "speaking": SCORES}) == "secure"
    low = {"pronunciation": 2, "accuracy": 2, "fluency": 1, "comprehension": 2}     # mean 1.75
    assert ua.outcome({**row, "speaking": low}) == "not_yet"
    assert ua.outcome({**row, "quiz_correct": 13, "speaking": SCORES}) == "developing"


@pytest.fixture
def db():
    units = [{"id": f"u{n}", "title": f"Lesson {n}", "unit_order": n, "level": 1, "course_id": COURSE}
             for n in range(1, 31)]
    units += [{"id": "p1", "title": "Intro", "unit_order": 1, "level": None, "course_id": COURSE}]
    fake = FakeDB({"unit": units, "student_course": [{"student_id": "s", "course_id": COURSE}],
                   "unit_progress": [], "unit_assessment": [],
                   "student_tutor_link": [{"student_id": "s", "tutor_id": "home", "state": "active"}],
                   "cohort_memberships": [{"user_id": "s", "cohort_id": "pod", "status": "active"}],
                   "cohorts": [{"id": "pod", "tutor_id": "podtutor", "status": "active"}]},
                  unique={"unit_assessment": [("student_id", "course_id", "block")]})
    with patch.object(ua, "db", fake), \
         patch("app.services.intake_service.db", fake), \
         patch.object(hierarchy_service, "_app", lambda: fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: f"Name {i}" for i in ids}):
        yield fake


def _complete(db, upto):
    db.rows["unit_progress"] = [{"user_id": "s", "unit_id": f"u{n}", "status": "completed"}
                                for n in range(1, upto + 1)]


# 3
def test_opening(db):
    _complete(db, 9)
    assert ua.mine("s")["assessments"] == []
    _complete(db, 10)
    a = ua.mine("s")["assessments"]
    assert [x["block"] for x in a] == [1]
    assert [l["lesson"] for l in a[0]["lessons"]] == list(range(1, 11))      # content units only
    ua.mine("s")
    assert len(db.rows["unit_assessment"]) == 1                              # once
    _complete(db, 20)
    assert [x["block"] for x in ua.mine("s")["assessments"]] == [1, 2]


# 4
def test_quiz(db):
    _complete(db, 10)
    aid = ua.mine("s")["assessments"][0]["id"]
    with pytest.raises(UnitAssessmentRefused) as e:
        ua.submit_quiz("other", aid, 15, 20, [])
    assert e.value.reason == "not_your_assessment"
    with pytest.raises(UnitAssessmentRefused):
        ua.submit_quiz("s", aid, 21, 20, [])
    out = ua.submit_quiz("s", aid, 15, 20, [3, 3, 7, 44])
    assert out["quiz_band"] == "developing" and out["weak_lessons"] == [3, 7] and out["outcome"] is None
    with pytest.raises(UnitAssessmentRefused) as e:
        ua.submit_quiz("s", aid, 20, 20, [])
    assert e.value.reason == "already_taken"
    assert "lessons" not in ua.mine("s")["assessments"][0]                   # taken: no quiz to build


# 5
def test_speaking(db):
    _complete(db, 10)
    aid = ua.mine("s")["assessments"][0]["id"]
    for who in ("s", "stranger"):
        with pytest.raises(UnitAssessmentRefused) as e:
            ua.rate_speaking(who, aid, SCORES)
        assert e.value.reason == "not_allowed"
    assert ua.may_rate("home", "s") and ua.may_rate("podtutor", "s") and ua.may_rate("ops", "s")
    with pytest.raises(UnitAssessmentRefused):
        ua.rate_speaking("home", aid, {**SCORES, "fluency": 5})
    assert [r["name"] for r in ua.to_rate("podtutor")] == ["Name s"] and ua.to_rate("stranger") == []
    out = ua.rate_speaking("podtutor", aid, SCORES)
    assert out["speaking_band"] == "secure" and out["outcome"] is None       # no quiz yet
    assert ua.to_rate("home") == []
    out = ua.submit_quiz("s", aid, 10, 20, [2])
    assert out["outcome"] == "not_yet"
    with pytest.raises(UnitAssessmentRefused) as e:
        ua.rate_speaking("home", aid, SCORES)
    assert e.value.reason == "already_rated"
    assert ua.latest("s") == {"block": 1, "outcome": "not_yet", "weak_lessons": [2], "lesson_to": 10}


# 6
def test_tracker_folds_the_latest():
    now = datetime(2027, 3, 1, tzinfo=timezone.utc)
    base = tracker_service.assess("tutor_tier", 10, 10, now)
    assert tracker_service.apply_assessment(base, None) == {**base, "assessment": None}
    secure = {"block": 1, "outcome": "secure", "weak_lessons": [], "lesson_to": 10}
    assert tracker_service.apply_assessment(base, secure)["projected_b2"] == base["projected_b2"]
    out = tracker_service.apply_assessment(base, {**secure, "outcome": "not_yet", "weak_lessons": [3, 7]})
    assert (date.fromisoformat(out["projected_b2"]) - date.fromisoformat(base["projected_b2"])).days == 14
    assert out["fix"].startswith("Review lessons 3, 7 before moving on")
    out = tracker_service.apply_assessment(base, {**secure, "outcome": "not_yet"})
    assert "lessons 1–10" in out["fix"]


# 7
def test_checkpoint_carries_it():
    from app.services import checkpoint_service
    fake = FakeDB({"cohort_memberships": [{"cohort_id": "pod", "user_id": "s", "status": "active"}],
                   "cohort_session": [], "enrolment_state": []})
    latest = {"block": 1, "outcome": "not_yet", "weak_lessons": [], "lesson_to": 10}
    with patch.object(checkpoint_service, "db", fake), \
         patch("app.services.intake_service._current_lesson_map", return_value={"s": 11}), \
         patch("app.services.makeup_service._names", return_value={}), \
         patch("app.services.tracker_service.for_student", return_value={"status": "on_track"}), \
         patch.object(ua, "latest", return_value=latest):
        snap = checkpoint_service.snapshot({"id": "pod", "tutor_id": "t"}, date(2027, 3, 1), date(2027, 4, 1))
    m = snap["members"][0]
    assert m["assessment"] == latest and "assessment_not_yet" in m["flags"]
