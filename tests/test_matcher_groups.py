"""Friend groups in the matcher (enrolment plan 3.2, E-12 Stage 2/4, §108).

Pinned:
1. A friend group is seated together or not at all — never split, and never
   added or swapped one member at a time by a repair pass.
2. A group whose lessons aren't adjacent is still found (the singles' sliding
   window couldn't).
3. A member missing from the run holds the rest back (GROUP_WAITING); members
   at different levels are told (GROUP_LEVEL_GAP).
4. Groups are seated before singles; a pool with no groups is unchanged.
"""
from app.services.cohort_matcher import MatchConfig, REASON_GROUP_LEVEL_GAP, REASON_GROUP_WAITING, run
from tests.test_cohort_matcher import CYCLE, a_student, a_tutor


def _together(result, ids):
    for c in result.cohorts:
        if set(ids) & set(c.student_ids):
            return set(ids) <= set(c.student_ids)
    return None   # nobody placed


def test_pair_is_seated_together_with_singles():
    pool = [a_student("f1", lesson=2, group_id="g", group_size=2),
            a_student("f2", lesson=2, group_id="g", group_size=2),
            a_student("s1", lesson=3), a_student("s2", lesson=4)]
    r = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert _together(r, ["f1", "f2"]) is True
    assert r.cohorts[0].groups == 1


def test_group_found_when_its_lessons_are_not_adjacent():
    # Lessons 1 (friend), 2, 3, 4 (singles), 5 (friend): no 4-window holds both.
    pool = [a_student("f1", lesson=1, group_id="g", group_size=2),
            a_student("a", lesson=2), a_student("b", lesson=3), a_student("c", lesson=4),
            a_student("f2", lesson=5, group_id="g", group_size=2)]
    r = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert _together(r, ["f1", "f2"]) is True


def test_a_trio_with_one_seat_free_is_never_split_by_grow():
    # Tutor with one slot: the trio takes 3 seats; a single may join (4); the
    # trio is never broken to make room.
    pool = [a_student(i, group_id="trio", group_size=3) for i in ("t1", "t2", "t3")] + \
           [a_student("x"), a_student("y")]
    r = run(pool, [a_tutor(capacity=1)], MatchConfig(), CYCLE)
    assert _together(r, ["t1", "t2", "t3"]) is True
    assert len(r.cohorts) == 1 and r.cohorts[0].size == 4


def test_missing_friend_holds_the_group():
    pool = [a_student("f1", group_id="g", group_size=3), a_student("f2", group_id="g", group_size=3),
            a_student("s1"), a_student("s2")]
    r = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    reasons = {d.student_id: d.reason for d in r.deferrals}
    assert reasons["f1"] == reasons["f2"] == REASON_GROUP_WAITING
    assert "f1" not in r.placed_ids


def test_level_gap_is_told_not_split():
    pool = [a_student("f1", level=1, group_id="g", group_size=2),
            a_student("f2", level=2, group_id="g", group_size=2),
            a_student("s1"), a_student("s2"), a_student("s3")]
    r = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    reasons = {d.student_id: d.reason for d in r.deferrals}
    assert reasons["f1"] == reasons["f2"] == REASON_GROUP_LEVEL_GAP


def test_pool_without_groups_has_no_group_candidates():
    pool = [a_student(i) for i in ("a", "b", "c", "d")]
    r = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert r.cohorts and all(c.groups == 0 for c in r.cohorts)


# FC-041 gap closed: one friend accepting aligns the group's other offers.
async def test_friend_accepting_withdraws_the_groups_other_offers():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from app.services import offer_service

    rows = {
        "enrolment_group_member": [{"group_id": "g", "student_id": "f1", "left_at": None},
                                   {"group_id": "g", "student_id": "f2", "left_at": None}],
        "match_offer": [{"id": "o-same", "student_id": "f2", "cohort_id": "X", "cycle_id": "c", "status": "offered"},
                        {"id": "o-other", "student_id": "f2", "cohort_id": "Y", "cycle_id": "c", "status": "offered"}],
    }

    class Q:
        def __init__(self, name): self.name, self.f = name, []
        def select(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
        def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
        def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self
        def execute(self): return SimpleNamespace(data=[r for r in rows[self.name] if all(f(r) for f in self.f)])

    fake = SimpleNamespace(schema=lambda _: SimpleNamespace(table=Q))
    with patch.object(offer_service, "db", fake), \
         patch.object(offer_service, "_supersede", new=AsyncMock()) as sup, \
         patch.object(offer_service, "_promote_waiting", new=AsyncMock()):
        n = await offer_service._align_group("f1", "c", "X")
    assert n == 1 and sup.await_args.args[0] == "o-other"
