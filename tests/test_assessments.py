"""Exit assessments, certificates, the guarantee (Track 10, §138).

Pinned:
1. Off without the flag. Request only at lesson 60; one open at a time; no C1 yet.
2. Raters: rung 4, active, two different, never the learner's own tutors.
3. Rating: four skills + overall, CEFR; both needed; pass = both at the level.
4. A pass issues a certificate; /verify shows name, level, date only.
5. A not-yet for Intensive at >= 85% attendance (tutor-marked + peer) grants
   2 months; under 85% or another tier grants none.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.services import assessment_service as svc
from app.services import hierarchy_service
from app.services.assessment_service import AssessmentRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 11, 1, tzinfo=timezone.utc)
GOOD = {"listening": "B2", "speaking": "B2", "reading": "C1", "writing": "B2", "overall": "B2"}
LOW = {**GOOD, "overall": "B1"}


@pytest.fixture
def db():
    fake = FakeDB({
        "feature_flag": [{"key": "exit_assessment", "status": "ready"}],
        "tutor": [{"id": t, "status": "active"} for t in ("own", "r1", "r2", "r3low")],
        "student": [{"id": "s1", "tier": "intensive_tier"}, {"id": "s2", "tier": "tutor_tier"}],
        "exit_assessment": [], "certificate": [], "guarantee_claim": [],
        "cohort_session_attendance": [], "cohort_session": [], "peer_session_participant": [], "peer_session": [],
    }, unique={"certificate": [("verify_code",), ("assessment_id",)]})
    done = {"s1": 60, "s2": 60, "s3": 40}
    with patch("app.services.assessment_service.db", fake), patch("app.core.flags.db", fake), \
         patch("app.services.peer_service.db", fake), \
         patch("app.services.assessment_service._lessons_done", side_effect=lambda s: done.get(s, 0)), \
         patch("app.services.tracker_service._b2_lesson", return_value=60), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch.object(hierarchy_service, "linked_tutor_ids", side_effect=lambda s: {"own"}), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: {"own": 4, "r1": 4, "r2": 4, "r3low": 3}[t]), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: "Hanna Bekele" for i in ids}):
        yield fake


def _scheduled(db, sid="s1"):
    a = svc.request(sid)
    svc.schedule("ops", a["id"], NOW, "r1", "r2")
    return a["id"]


# 1
def test_request_rules(db):
    with pytest.raises(AssessmentRefused) as e:
        svc.request("s3")
    assert e.value.reason == "not_yet_eligible"
    with pytest.raises(AssessmentRefused) as e:
        svc.request("s1", "C1")
    assert e.value.reason == "c1_not_open"
    svc.request("s1")
    db.unique["exit_assessment"] = [("student_id", "level")]
    with pytest.raises(AssessmentRefused):
        svc.request("s1")
    db.rows["feature_flag"][0]["status"] = "coming"
    assert svc.mine("s1") == {"enabled": False}


# 2
def test_raters(db):
    assert svc.eligible_raters("s1") == ["r1", "r2"]
    a = svc.request("s1")
    for r1, r2 in (("r1", "r1"), ("r1", "own"), ("r1", "r3low")):
        with pytest.raises(AssessmentRefused) as e:
            svc.schedule("ops", a["id"], NOW, r1, r2)
        assert e.value.reason == "bad_raters"
    with pytest.raises(AssessmentRefused):
        svc.schedule("r1", a["id"], NOW, "r1", "r2")


# 3 + 4
def test_pass_issues_a_verifiable_certificate(db):
    aid = _scheduled(db)
    with pytest.raises(AssessmentRefused):
        svc.rate("r1", aid, {**GOOD, "overall": "B3"})
    with pytest.raises(AssessmentRefused):
        svc.rate("own", aid, GOOD)
    assert svc.rate("r1", aid, GOOD)["status"] == "scheduled"
    with pytest.raises(AssessmentRefused):
        svc.rate("r1", aid, GOOD)
    out = svc.rate("r2", aid, GOOD, NOW)
    assert (out["status"], out["outcome"]) == ("done", "pass")
    cert = db.rows["certificate"][0]
    assert len(cert["verify_code"]) == 10
    v = svc.verify(cert["verify_code"].lower())
    assert v == {"holder_name": "Hanna Bekele", "level": "B2", "issued_at": cert["issued_at"][:10], "issuer": "Tauka"}
    assert svc.verify("NOPE123456") is None


# 5
def test_guarantee(db):
    db.rows["cohort_session"] = [{"id": f"c{i}", "scheduled_at": "2027-06-01T10:00:00+00:00"} for i in range(10)]
    db.rows["cohort_session_attendance"] = [{"cohort_session_id": f"c{i}", "user_id": "s1",
                                             "status": "attended" if i < 9 else "absent"} for i in range(10)]
    aid = _scheduled(db)
    svc.rate("r1", aid, LOW)
    svc.rate("r2", aid, GOOD, NOW)
    claim = db.rows["guarantee_claim"][0]
    assert (claim["attendance_bp"], claim["months_granted"], claim["status"]) == (9000, 2, "granted")
    assert db.rows["certificate"] == []
    # Tutor tier: no guarantee.
    aid2 = _scheduled(db, "s2")
    svc.rate("r1", aid2, LOW)
    svc.rate("r2", aid2, LOW, NOW)
    assert len(db.rows["guarantee_claim"]) == 1


def test_low_attendance_declines(db):
    db.rows["cohort_session"] = [{"id": f"c{i}", "scheduled_at": "2027-06-01T10:00:00+00:00"} for i in range(10)]
    db.rows["cohort_session_attendance"] = [{"cohort_session_id": f"c{i}", "user_id": "s1",
                                             "status": "attended" if i < 8 else "absent"} for i in range(10)]
    aid = _scheduled(db)
    svc.rate("r1", aid, LOW)
    svc.rate("r2", aid, LOW, NOW)
    claim = db.rows["guarantee_claim"][0]
    assert (claim["attendance_bp"], claim["months_granted"], claim["status"]) == (8000, 0, "declined")
