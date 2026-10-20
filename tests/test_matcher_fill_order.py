"""Matcher fill order and low volume (enrolment plan 3.3).

Pinned:
1. ANCHORED first: a candidate seating the slot tutor's referred learners is
   ordered ahead of a higher-scoring one without them, and ahead of a group.
2. Below `low_volume_pool` ready learners, the soft criteria (goal, style, L1,
   experience) score nothing; at or above it they count. Recorded in stats.
"""
from dataclasses import replace
from datetime import datetime, timezone

from app.services import cohort_matcher as cm

T = datetime(2027, 1, 9, 10, tzinfo=timezone.utc)


def _slot(tutor="t1"):
    return cm.TutorSlot(tutor_id=tutor, start_utc=T, local_day_of_week=5, local_start_minute=600, tz="UTC")


def _cand(score, anchored=0, groups=0, ids=("a", "b", "c")):
    return cm.Candidate(student_ids=ids, slot=_slot(), cadence_class="B", level=1, score=score, breakdown={},
                        lesson_low=1, lesson_high=1, bands_relaxed=False, claim=(), groups=groups, anchored=anchored)


# 1
def test_anchored_candidates_come_first():
    plain, grouped, anchored = _cand(95), _cand(90, groups=1), _cand(60, anchored=2)
    assert sorted([plain, grouped, anchored], key=cm._candidate_order) == [anchored, grouped, plain]


def test_anchored_counts_the_slot_tutors_referrals():
    s = lambda i, ref: cm.StudentIn(id=i, tier="tutor_tier", level=1, current_lesson=1, tz="UTC",  # noqa: E731
                                     referral_tutor_id=ref)
    assert cm._anchored([s("a", "t1"), s("b", "t1"), s("c", "t2"), s("d", None)], _slot("t1")) == 2


# 2
def test_soft_criteria_collapse_at_low_volume():
    tutor = cm.TutorIn(id="t1", min_level=1, max_level=6, max_concurrent_cohorts=3, specialisations=("heritage",))
    members = [cm.StudentIn(id=i, tier="tutor_tier", level=1, current_lesson=1, tz="UTC", goal="heritage")
               for i in "abc"]
    cfg = cm.MatchConfig()
    full, b_full = cm.score_candidate(members, _slot(), tutor, cfg, 5, 2.0)
    low, b_low = cm.score_candidate(members, _slot(), tutor, replace(cfg, soft_collapsed=True), 5, 2.0)
    assert b_full["goal"] > 0 and b_low["goal"] == 0 and low < full


def test_run_records_the_collapse():
    cfg = cm.MatchConfig()
    result = cm.run([], [], cfg, T.date())
    assert result.stats["soft_collapsed"] is True
    result = cm.run([], [], replace(cfg, low_volume_pool=0), T.date())
    assert result.stats["soft_collapsed"] is False
