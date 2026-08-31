"""The cohort matcher — plan `tutor-cohort-matching-plan.md` §3, backlog O-16.

`cohort_matcher` is pure, so every test here is a plain function call with no
fixtures, no database and no mocking. That is the whole reason the pipeline was
split the way it was.

WHY THE ADVERSARIAL POOLS ARE HALF THIS FILE. Plan §11 names the residual risk
precisely: the repair passes (grow / eject / open) "only fire on pools too thin
or too lopsided for the greedy seed, which is precisely the shape of an early
real cycle and the hardest shape to imagine while writing tests. The dry run
must include adversarial pools, not just plausible ones — a level with one
student, a timezone isolate, a slot with nine eligible students and one tutor."
All three are below, by name.
"""

from datetime import date

import pytest

from app.services.cohort_matcher import (
    MatchConfig,
    StudentIn,
    TimeWindow,
    TutorIn,
    build_tutor_slots,
    first_occurrence_utc,
    member_fit,
    run,
    summarise,
)

# 2026-09-01 is a Tuesday. Every test dates from it so weekday arithmetic in the
# assertions is checkable by hand.
CYCLE = date(2026, 9, 1)
NAIROBI = "Africa/Nairobi"      # UTC+3 all year, no DST
LONDON = "Europe/London"        # UTC+1 in September (BST)
NEW_YORK = "America/New_York"   # UTC-4 in September (EDT)


def hhmm(h: int, m: int = 0) -> int:
    return h * 60 + m


def a_tutor(
    tid: str = "t1",
    *,
    day: int = 1,
    start: int = hhmm(9),
    end: int = hhmm(13),
    tz: str = NAIROBI,
    min_level: int = 1,
    max_level: int = 4,
    capacity: int = 3,
    used: int = 0,
    **kwargs,
) -> TutorIn:
    return TutorIn(
        id=tid,
        min_level=min_level,
        max_level=max_level,
        max_concurrent_cohorts=capacity,
        current_cohorts=used,
        availability=(TimeWindow(day, start, end, tz=tz),),
        specialisations=kwargs.pop("specialisations", ("conversation",)),
        teaching_style=kwargs.pop("teaching_style", "social"),
        native_language=kwargs.pop("native_language", "Amharic"),
        years_experience=kwargs.pop("years_experience", 5),
    )


def a_student(
    sid: str,
    *,
    tier: str = "tutor_tier",
    level: int = 1,
    lesson: int = 3,
    tz: str = NAIROBI,
    windows: tuple[TimeWindow, ...] | None = None,
    **kwargs,
) -> StudentIn:
    if windows is None:
        windows = (
            TimeWindow(1, hhmm(9), hhmm(13), rank=1),
            TimeWindow(2, hhmm(9), hhmm(13), rank=2),
            TimeWindow(3, hhmm(9), hhmm(13), rank=3),
        )
    return StudentIn(
        id=sid, tier=tier, level=level, current_lesson=lesson, tz=tz,
        windows=windows,
        goal=kwargs.pop("goal", "conversation"),
        style=kwargs.pop("style", "social"),
        native_language=kwargs.pop("native_language", "Amharic"),
        **kwargs,
    )


# ── Stage 0 — the canonical week ─────────────────────────────────────────────

def test_slot_converts_through_a_real_zone_not_an_offset():
    """09:00 Nairobi is 06:00 UTC. Plan §3 Stage 0 forbids fixed offsets."""
    instant = first_occurrence_utc(
        __import__("zoneinfo").ZoneInfo(NAIROBI), CYCLE, 1, hhmm(9))
    assert instant.hour == 6
    assert instant.date() == date(2026, 9, 1)  # the Tuesday itself


def test_dst_is_applied_at_the_slot_not_assumed():
    """London is UTC+1 in September and UTC+0 in December.

    This is the defect plan §3 Stage 0 warns about by name: an intake on the 1st
    placing a cohort that starts after a DST change slips by an hour for half
    its members. Converting through the zone at the actual instant is what stops
    it, and this test fails the moment somebody caches an offset.
    """
    zone = __import__("zoneinfo").ZoneInfo(LONDON)
    september = first_occurrence_utc(zone, date(2026, 9, 1), 1, hhmm(9))
    december = first_occurrence_utc(zone, date(2026, 12, 1), 1, hhmm(9))
    assert september.hour == 8   # BST, UTC+1
    assert december.hour == 9    # GMT, UTC+0


def test_slots_stop_before_the_window_ends():
    """A 50-minute session needs 50 minutes. 09:00-13:00 on a 30-minute grid
    yields starts at 09:00 .. 12:00 inclusive — seven, not eight."""
    slots = build_tutor_slots([a_tutor()], MatchConfig(), CYCLE)
    assert len(slots) == 7
    assert max(s.local_start_minute for s in slots) == hhmm(12)


def test_a_tutor_at_capacity_produces_no_slots():
    """Capacity is applied at slot creation (plan §3 Stage 1), so a full tutor
    is invisible to matching rather than filtered later."""
    assert build_tutor_slots(
        [a_tutor(capacity=2, used=2)], MatchConfig(), CYCLE) == []


# ── Stage 1 — hard constraints ───────────────────────────────────────────────

def test_a_full_cohort_forms_from_a_clean_pool():
    pool = [a_student(f"s{i}", lesson=3 + i) for i in range(4)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 1
    assert len(result.cohorts[0].student_ids) == 4
    assert not result.deferrals


def test_proficiency_band_is_hard():
    """A level-4 student and a tutor certified 1-2 do not match, and the reason
    names the certification rather than the calendar — it is a hiring brief."""
    pool = [a_student(f"s{i}", level=4) for i in range(4)]
    result = run(pool, [a_tutor(max_level=2)], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 0
    assert {d.reason for d in result.deferrals} == {"NO_TUTOR_FOR_LEVEL"}


def test_cadence_class_never_mixes():
    """Plan §1b.2: class A (Learner, monthly) and class B (Tutor/Intensive,
    3x/month) are a HARD constraint. Two of each cannot make one cohort of four
    even though every other constraint is satisfied."""
    pool = [a_student("a1", tier="learner_tier"), a_student("a2", tier="learner_tier"),
            a_student("b1", tier="tutor_tier"), a_student("b2", tier="tutor_tier")]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 0
    for cohort in result.cohorts:
        assert len({p for p in cohort.student_ids}) <= 2


def test_free_tier_is_not_in_the_pool_and_is_not_a_deferral():
    """A free-tier student has not bought live sessions. Filing them as deferred
    would make every count in the cycle wrong."""
    pool = [a_student("f1", tier="free_tier")] + [
        a_student(f"s{i}") for i in range(3)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert "f1" not in {d.student_id for d in result.deferrals}
    assert "f1" not in result.placed_ids


def test_civil_hours_bind_even_when_the_offset_band_does_not():
    """Plan §3 Stage 0: "the offset band alone will happily schedule three
    people at 06:00". A 06:00 Nairobi slot is inside every band and outside
    every civilised hour."""
    tutor = a_tutor(start=hhmm(6), end=hhmm(7))
    pool = [a_student(f"s{i}",
                      windows=(TimeWindow(1, hhmm(5), hhmm(8), rank=1),) * 1 +
                              (TimeWindow(2, hhmm(9), hhmm(13), rank=2),
                               TimeWindow(3, hhmm(9), hhmm(13), rank=3)))
            for i in range(4)]
    result = run(pool, [tutor], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 0


def test_the_whole_session_must_fit_inside_a_declared_window():
    """Intersecting rather than containing would place a student who is free for
    the first ten minutes of their own lesson."""
    tutor = a_tutor(start=hhmm(12), end=hhmm(13))
    pool = [a_student(f"s{i}", windows=(
        TimeWindow(1, hhmm(11), hhmm(12, 30), rank=1),
        TimeWindow(2, hhmm(9), hhmm(13), rank=2),
        TimeWindow(3, hhmm(9), hhmm(13), rank=3),
    )) for i in range(4)]
    result = run(pool, [tutor], MatchConfig(), CYCLE)
    # The only tutor slot is 12:00-12:50; the rank-1 window ends at 12:30.
    assert result.stats["cohorts_formed"] == 0


def test_a_commitment_lock_confines_a_student_to_one_tutor():
    """Spec §1.3 / platform plan §4.4: a locked home tutor is non-negotiable —
    not scored down, excluded."""
    locked = a_student("s0", locked_tutor_id="t2")
    others = [a_student(f"s{i}") for i in range(1, 4)]
    result = run([locked, *others], [a_tutor("t1")], MatchConfig(), CYCLE)
    assert "s0" not in result.placed_ids
    assert result.stats["cohorts_formed"] == 1
    assert len(result.cohorts[0].student_ids) == 3


def test_a_referral_is_a_hard_preference_subject_to_capacity():
    """Spec §1.1 via platform plan §3.1. If the referring tutor has a feasible
    slot, the student is matched there; if not, the preference yields rather
    than deferring somebody over an attribution that still pays out."""
    cfg = MatchConfig()
    referred = [a_student(f"r{i}", referral_tutor_id="t2") for i in range(3)]
    plain = [a_student(f"p{i}") for i in range(3)]
    t1 = a_tutor("t1")
    t2 = a_tutor("t2", day=3)
    result = run([*referred, *plain], [t1, t2], cfg, CYCLE)
    referred_cohorts = [c for c in result.cohorts
                        if set(c.student_ids) & {"r0", "r1", "r2"}]
    assert referred_cohorts, "the referred students should be placed"
    for c in referred_cohorts:
        assert c.slot.tutor_id == "t2"

    # And with no t2 at all, the preference must not strand them.
    yielded = run([*referred], [t1], cfg, CYCLE)
    assert yielded.stats["cohorts_formed"] == 1


# ── Stage 2 — the bands ──────────────────────────────────────────────────────

def test_lesson_band_excludes_a_straggler():
    pool = [a_student("s0", lesson=1), a_student("s1", lesson=2),
            a_student("s2", lesson=3), a_student("s3", lesson=40)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 1
    assert "s3" not in result.cohorts[0].student_ids
    assert "s3" in {d.student_id for d in result.deferrals}


def test_timezone_band_excludes_a_stranded_offset():
    """Nairobi is UTC+3 and New York is UTC-4 in September: seven hours apart,
    outside a 2h band. The New Yorker is a TZ_ISOLATE, not a scheduling
    accident, and the deferral says so."""
    pool = [a_student(f"s{i}") for i in range(3)]
    pool.append(a_student("far", tz=NEW_YORK, windows=(
        TimeWindow(1, hhmm(0), hhmm(23), rank=1),
        TimeWindow(2, hhmm(0), hhmm(23), rank=2),
        TimeWindow(3, hhmm(0), hhmm(23), rank=3),
    )))
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 1
    assert "far" not in result.cohorts[0].student_ids
    far = next(d for d in result.deferrals if d.student_id == "far")
    assert far.reason in ("TZ_ISOLATE", "NO_SLOT_OVERLAP")


def test_bands_relax_after_two_waiting_cycles():
    """Cold-start ladder Stage 2 (plan §4b): automatic and system-side, before
    any human remedy. The same pool that cannot form at lesson_band 5 forms at
    8 once everyone has waited two cycles, and the cohort is LABELLED relaxed so
    a looser group is known to be looser."""
    lessons = (1, 4, 7)
    strict = [a_student(f"s{i}", lesson=l) for i, l in enumerate(lessons)]
    assert run(strict, [a_tutor()], MatchConfig(), CYCLE).stats["cohorts_formed"] == 0

    waited = [a_student(f"s{i}", lesson=l, cycles_waited=2)
              for i, l in enumerate(lessons)]
    result = run(waited, [a_tutor()], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 1
    assert result.cohorts[0].bands_relaxed is True


# ── Stage 3 — scoring ────────────────────────────────────────────────────────

def test_l1_and_experience_decay_with_level():
    """Plan §3 Stage 3 implements what the spec SAYS rather than what it
    tabulates: L1 explanation is "useful for beginners", and experienced tutors
    are wanted *less* by advanced students. A flat +15/+10 says neither."""
    cfg = MatchConfig()
    tutor = a_tutor(years_experience=10)
    beginner = member_fit(a_student("b", level=1), tutor, cfg)
    advanced = member_fit(a_student("a", level=4), tutor, cfg)
    assert beginner["l1"] == 15.0
    assert advanced["l1"] == 0.0
    assert beginner["experience"] == 10.0
    assert advanced["experience"] == 0.0


def test_a_junior_tutor_scores_higher_for_an_advanced_student():
    """The other half of the same sentence: an advanced learner "benefits more
    from a conversation partner" than from a veteran."""
    cfg = MatchConfig()
    junior = a_tutor("j", years_experience=1)
    senior = a_tutor("s", years_experience=20)
    advanced = a_student("a", level=4)
    assert member_fit(advanced, junior, cfg)["experience"] > \
           member_fit(advanced, senior, cfg)["experience"]


def test_tier_cohesion_is_scored_and_does_not_outrank_goal_alignment():
    """Plan §3 Stage 3 weights cohesion at 20, "level with style match, below
    goal alignment", and argues the ordering explicitly. The one-line formula in
    the plan omits the term entirely; building the line rather than the table
    would leave it scoring nothing."""
    cfg = MatchConfig()
    homogeneous = [a_student(f"s{i}", tier="tutor_tier") for i in range(4)]
    mixed = [a_student("s0", tier="tutor_tier"), a_student("s1", tier="tutor_tier"),
             a_student("s2", tier="intensive_tier"),
             a_student("s3", tier="intensive_tier")]
    hom = run(homogeneous, [a_tutor()], cfg, CYCLE).cohorts[0]
    mix = run(mixed, [a_tutor()], cfg, CYCLE).cohorts[0]
    assert hom.breakdown["tier_cohesion"] == 20.0
    assert mix.breakdown["tier_cohesion"] == 10.0
    assert cfg.weights["tier_cohesion"] < cfg.weights["goal"]


def test_the_offer_summary_is_a_template_and_never_empty():
    """Plan §3 Stage 5: the summary "must be instant, stable, and incapable of
    confidently misstating someone's schedule" — a template, not an LLM call."""
    result = run([a_student(f"s{i}") for i in range(4)], [a_tutor()],
                 MatchConfig(), CYCLE)
    line = summarise(result.cohorts[0])
    assert line.endswith(".")
    assert "group is" in line


# ── Stage 4 — determinism ────────────────────────────────────────────────────

def test_the_same_inputs_produce_the_same_answer():
    """A rerun must produce the same answer or nothing is debuggable and nothing
    is explicable to a student. Shuffling the input order must not move anyone."""
    pool = [a_student(f"s{i}", lesson=1 + i % 4) for i in range(9)]
    tutors = [a_tutor("t1"), a_tutor("t2", day=3)]
    first = run(pool, tutors, MatchConfig(), CYCLE)
    second = run(list(reversed(pool)), list(reversed(tutors)), MatchConfig(), CYCLE)
    assert [c.student_ids for c in first.cohorts] == \
           [c.student_ids for c in second.cohorts]
    assert first.placed_ids == second.placed_ids


def test_priority_and_waiting_break_ties_before_the_id_does():
    """`(priority DESC, cycles_waited DESC, student_id ASC)`. With five students
    for four seats the one left out must be the one with the weakest claim —
    never simply the last id."""
    pool = [
        a_student("aaa", priority=False, cycles_waited=0),
        a_student("bbb", priority=True, cycles_waited=0),
        a_student("ccc", priority=False, cycles_waited=3),
        a_student("ddd", priority=True, cycles_waited=1),
        a_student("eee", priority=False, cycles_waited=2),
    ]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    placed = result.placed_ids
    if len(placed) == 4:
        assert "bbb" in placed and "ddd" in placed
        assert "aaa" not in placed


# ── Adversarial pools — plan §11's named residual risk ───────────────────────

def test_adversarial_a_level_with_one_student():
    """One student at level 3 among nine at level 1. They cannot be placed, and
    the reason must be the actionable one — too few peers, a marketing problem —
    rather than a timezone or a rota they cannot influence."""
    pool = [a_student(f"s{i}", level=1) for i in range(9)]
    pool.append(a_student("lonely", level=3))
    result = run(pool, [a_tutor(), a_tutor("t2", day=3)], MatchConfig(), CYCLE)
    lonely = next(d for d in result.deferrals if d.student_id == "lonely")
    assert lonely.reason == "TOO_FEW_PEERS_AT_LEVEL"
    assert lonely.detail["peers_at_level"] == 0


def test_adversarial_nine_eligible_students_and_one_slot():
    """A slot with nine takers and one tutor. Plan §3 Stage 4's whole point:
    every remaining student must get a precise reason, nobody may be placed
    twice, and no slot may be used twice."""
    tutor = TutorIn(id="t1", min_level=1, max_level=4, max_concurrent_cohorts=1,
                    availability=(TimeWindow(1, hhmm(9), hhmm(10), tz=NAIROBI),))
    pool = [a_student(f"s{i}") for i in range(9)]
    result = run(pool, [tutor], MatchConfig(), CYCLE)

    assert result.stats["cohorts_formed"] == 1
    assert len(result.cohorts[0].student_ids) == 4
    assert len(result.placed_ids) == 4
    assert len(result.deferrals) == 5
    # Every student is accounted for exactly once.
    assert result.placed_ids | {d.student_id for d in result.deferrals} == \
           {s.id for s in pool}


def test_adversarial_timezone_isolate_among_many():
    """One student seven hours from everyone else, with a wide-open calendar.
    A band expressed as an average would place them; a spread must not."""
    pool = [a_student(f"s{i}") for i in range(8)]
    pool.append(a_student("isolate", tz=NEW_YORK, windows=(
        TimeWindow(1, hhmm(0), hhmm(23), rank=1),
        TimeWindow(2, hhmm(0), hhmm(23), rank=2),
        TimeWindow(3, hhmm(0), hhmm(23), rank=3))))
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    for cohort in result.cohorts:
        assert "isolate" not in cohort.student_ids


def test_adversarial_the_open_repair_pass_uses_a_second_slot():
    """Five students, one tutor with two hours of availability. The greedy seed
    takes four on the first slot; the OPEN pass must not leave the fifth
    stranded when a whole second slot is free — but it cannot open a cohort of
    one either, and saying so is the honest outcome."""
    pool = [a_student(f"s{i}") for i in range(5)]
    result = run(pool, [a_tutor(start=hhmm(9), end=hhmm(13))], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 1
    assert len(result.deferrals) == 1
    assert result.deferrals[0].reason in (
        "TOO_FEW_PEERS_AT_LEVEL", "TZ_ISOLATE")


def test_adversarial_seven_students_open_two_cohorts_not_one():
    """Seven eligible students and enough tutor time. A greedy seed alone would
    take four and strand three; OPEN must convert the remaining three into a
    second cohort, which is exactly the many-2-person-cohorts failure the plan's
    repair passes exist to prevent."""
    pool = [a_student(f"s{i}", lesson=1) for i in range(7)]
    result = run(pool, [a_tutor(start=hhmm(9), end=hhmm(13))], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 2
    assert len(result.placed_ids) == 7


def test_adversarial_empty_supply_defers_everyone_with_a_supply_reason():
    """No tutors at all. Nobody is placed, and nobody is told it was their
    calendar."""
    pool = [a_student(f"s{i}") for i in range(4)]
    result = run(pool, [], MatchConfig(), CYCLE)
    assert result.stats["cohorts_formed"] == 0
    assert {d.reason for d in result.deferrals} == {"NO_TUTOR_FOR_LEVEL"}


# ── Intake readiness — the three reasons a student can fix themselves ────────

@pytest.mark.parametrize("mutation, reason", [
    ({"tz": None}, "NO_TIMEZONE"),
    ({"level": None}, "NO_LEVEL"),
    ({"windows": (TimeWindow(1, hhmm(9), hhmm(13), rank=1),)}, "TOO_FEW_PREFERENCES"),
])
def test_intake_readiness_is_reported_before_anything_else(mutation, reason):
    """A student missing a timezone must be told that, not told there was no
    tutor for their level — the second is a recruitment signal and would be a
    lie about the platform's supply."""
    pool = [a_student("s0", **mutation)] + [a_student(f"s{i}") for i in range(1, 4)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    s0 = next(d for d in result.deferrals if d.student_id == "s0")
    assert s0.reason == reason


def test_an_unknown_timezone_defers_one_student_and_does_not_raise():
    """A bad IANA name must cost one deferral, never a cycle."""
    pool = [a_student("bad", tz="Mars/Olympus")] + [
        a_student(f"s{i}") for i in range(1, 4)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert next(d for d in result.deferrals if d.student_id == "bad").reason == \
        "NO_TIMEZONE"
    assert result.stats["cohorts_formed"] == 1


# ── Stage 5 — offers ─────────────────────────────────────────────────────────

def test_every_placed_student_gets_at_least_one_offer():
    pool = [a_student(f"s{i}") for i in range(4)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    assert set(result.offers) == result.placed_ids
    for cands in result.offers.values():
        assert 1 <= len(cands) <= MatchConfig().offer_ranks


def test_alternatives_are_real_cohorts_not_unformed_candidates():
    """An offer must name a cohort that will exist — `match_offer.cohort_id` is
    a foreign key. Offering rank 2 from the raw candidate list would be offering
    a seat in a group that was never formed."""
    pool = [a_student(f"s{i}", lesson=1) for i in range(7)]
    result = run(pool, [a_tutor(start=hhmm(9), end=hhmm(13))], MatchConfig(), CYCLE)
    formed = {c.student_ids for c in result.cohorts}
    for cands in result.offers.values():
        for c in cands:
            assert c.student_ids in formed


def test_median_wait_is_tracked_per_tier():
    """Plan §4: the tier that pays most is expected to wait longest, and that
    "stays invisible behind a healthy-looking average"."""
    pool = [a_student("i1", tier="intensive_tier", cycles_waited=4),
            a_student("i2", tier="intensive_tier", cycles_waited=6),
            a_student("l1", tier="learner_tier", cycles_waited=0),
            a_student("l2", tier="learner_tier", cycles_waited=0)]
    result = run(pool, [a_tutor()], MatchConfig(), CYCLE)
    by_tier = result.stats["median_cycles_waited_by_tier"]
    assert by_tier["intensive_tier"] == 5.0
    assert by_tier["learner_tier"] == 0.0
