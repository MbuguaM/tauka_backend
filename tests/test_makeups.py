"""Make-up sessions — enrolment plan §2e, migration §92.

The database half (minutes from head-count, the 3-learner cap, credits granted
on cancellation and spent on attendance, tutor may only mark attendance) was
tested against Postgres in a rolled-back transaction before §92 was applied.
These tests cover the Python half: who needs a seat, and how they are grouped.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import makeup_service as ms
from app.services.makeup_planner import (
    OpenGroup, Waiting, Window, find_slot, group_sizes, occurrences, plan_makeups,
)

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)          # a Friday
PAID = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
MON_23_0600 = datetime(2026, 11, 23, 6, tzinfo=timezone.utc)

# Tutor A teaches Mondays 09:00–12:00 in Addis (UTC+3) = 06:00–09:00 UTC.
ADDIS_MON = Window(0, 9 * 60, 12 * 60, "Africa/Addis_Ababa")
# A London learner free Mondays 06:00–10:00 (UTC+0 in November).
LONDON_MON = Window(0, 6 * 60, 10 * 60, "Europe/London", rank=1)


def _w(sid, course="c1", due_days=10, funded="month", pool=("A",), windows=(LONDON_MON,)):
    return Waiting(sid, course, funded, PAID if funded == "month" else None,
                   NOW + timedelta(days=due_days), tuple(pool), tuple(windows))


TUTORS = {"A": [ADDIS_MON], "B": [ADDIS_MON]}


# ── time ────────────────────────────────────────────────────────────────────

def test_a_weekly_window_resolves_in_its_own_zone():
    occ = occurrences(ADDIS_MON, NOW, NOW + timedelta(days=7))
    assert occ == [(MON_23_0600, MON_23_0600 + timedelta(hours=3))]


def test_monday_is_day_zero():
    occ = occurrences(Window(0, 0, 60, "UTC"), NOW, NOW + timedelta(days=7))
    assert all(a.weekday() == 0 for a, _ in occ)


def test_the_slot_is_inside_both_the_tutors_and_the_learners_hours():
    score, start = find_slot("A", [_w("a")], TUTORS, {}, NOW)
    assert start == MON_23_0600 and score == 1


def test_a_busy_tutor_moves_the_slot():
    busy = {"A": [(MON_23_0600, MON_23_0600 + timedelta(minutes=50))]}
    _, start = find_slot("A", [_w("a")], TUTORS, busy, NOW)
    assert start == MON_23_0600 + timedelta(minutes=60)   # first 15-min step clear of 06:00–06:50


def test_no_overlap_means_no_slot():
    evening = Window(0, 18 * 60, 20 * 60, "Europe/London")
    assert find_slot("A", [_w("a", windows=(evening,))], TUTORS, {}, NOW) is None


def test_nothing_is_proposed_after_the_deadline():
    assert find_slot("A", [_w("a", due_days=2)], TUTORS, {}, NOW) is None   # next Monday is day 3


def test_a_window_can_end_at_midnight():
    late = Window(0, 23 * 60, 24 * 60, "UTC")
    _, start = find_slot("A", [_w("a", windows=(late,))], {"A": [late]}, {}, NOW)
    assert (start.weekday(), start.hour) == (0, 23)


# ── grouping, tutor pools ───────────────────────────────────────────────────

@pytest.mark.parametrize("n,sizes", [(0, []), (1, [1]), (4, [2, 2]), (5, [3, 2]), (7, [3, 2, 2])])
def test_group_sizes_are_few_and_even(n, sizes):
    assert group_sizes(n) == sizes


def test_a_single_waiting_learner_still_gets_a_session_with_their_tutor():
    plan = plan_makeups([_w("a")], [], TUTORS, {}, NOW)
    (group, prop), = plan.new_groups
    assert [w.student_id for w in group] == ["a"]
    assert (prop.tutor_id, prop.starts_at, prop.note) == ("A", MON_23_0600, None)


def test_a_tutor_is_only_ever_one_from_the_learners_own_pool():
    plan = plan_makeups([_w("a", pool=("B",))], [], TUTORS, {}, NOW)
    assert plan.new_groups[0][1].tutor_id == "B"


def test_learners_share_a_group_only_through_a_tutor_in_all_their_pools():
    plan = plan_makeups([_w("a", pool=("A",)), _w("b", pool=("B",))], [], TUTORS, {}, NOW)
    assert sorted(len(g) for g, _ in plan.new_groups) == [1, 1]
    plan = plan_makeups([_w("a", pool=("A", "B")), _w("b", pool=("B",))], [], TUTORS, {}, NOW)
    (group, prop), = plan.new_groups
    assert prop.tutor_id == "B" and len(group) == 2


def test_the_home_tutor_is_preferred_when_group_size_is_equal():
    plan = plan_makeups([_w("a", pool=("B", "A"))], [], TUTORS, {}, NOW)
    assert plan.new_groups[0][1].tutor_id == "B"


def test_three_learners_get_thirty_minutes_inside_everyones_hours():
    plan = plan_makeups([_w("a"), _w("b"), _w("c")], [], TUTORS, {}, NOW)
    (group, prop), = plan.new_groups
    assert len(group) == 3 and prop.starts_at == MON_23_0600


def test_courses_are_never_mixed():
    plan = plan_makeups([_w("a", "c1"), _w("b", "c2")], [], TUTORS, {}, NOW)
    assert sorted(len(g) for g, _ in plan.new_groups) == [1, 1]


def test_no_pool_and_no_time_are_reported_for_the_admin():
    plan = plan_makeups([_w("a", pool=())], [], TUTORS, {}, NOW)
    assert plan.new_groups[0][1].note == "no_tutor_pool"
    evening = Window(0, 18 * 60, 20 * 60, "Europe/London")
    plan = plan_makeups([_w("b", windows=(evening,))], [], TUTORS, {}, NOW)
    assert plan.new_groups[0][1].note == "no_common_time"


def test_one_run_never_proposes_a_tutor_twice_at_the_same_moment():
    plan = plan_makeups([_w("a", "c1"), _w("b", "c2")], [], TUTORS, {}, NOW)
    starts = [p.starts_at for _, p in plan.new_groups]
    assert len(set(starts)) == 2


def test_a_learner_joins_an_open_group_whose_tutor_and_time_suit_them():
    member = _w("m")
    group = OpenGroup("s1", "c1", 1, NOW + timedelta(days=10), "A", MON_23_0600, (member,))
    busy = {"A": [(MON_23_0600, MON_23_0600 + timedelta(minutes=15))]}   # the group's own hold
    plan = plan_makeups([_w("a")], [group], TUTORS, busy, NOW)
    assert [(sid, w.student_id, p.tutor_id) for sid, w, p in plan.joins] == [("s1", "a", "A")]
    assert plan.new_groups == []


def test_a_learner_whose_pool_lacks_the_groups_tutor_does_not_join():
    group = OpenGroup("s1", "c1", 1, NOW + timedelta(days=10), "A", MON_23_0600, (_w("m"),))
    plan = plan_makeups([_w("a", pool=("B",))], [group], TUTORS, {}, NOW)
    assert plan.joins == [] and plan.new_groups[0][1].tutor_id == "B"


# ── who needs a seat ────────────────────────────────────────────────────────

def _facts(tier="tutor_tier", podded=False, seats=(), credits=0, paid=((PAID, "paid_month"),), pod=()):
    return ms.LearnerFacts("stu", tier, "c1", podded, list(paid), list(seats), credits, tuple(pod))


def _seat(funded="month", status="attended", session_status="completed", days_ago=10, period=PAID):
    return ms.Seat("s", funded, period if funded == "month" else None, status, session_status,
                   NOW - timedelta(days=days_ago))


def test_unpodded_paid_learner_gets_a_month_seat_due_at_month_end():
    w = ms.needs_seat(_facts(), NOW)
    assert w.funded_by == "month" and w.period_start == PAID
    assert w.due_by == datetime(2026, 12, 3, 9, tzinfo=timezone.utc)


def test_tutor_gets_three_seats_a_month_then_stops():
    two = [_seat(days_ago=15), _seat(days_ago=8)]
    assert ms.needs_seat(_facts(seats=two), NOW) is not None
    three = two + [_seat(days_ago=7, status="attended")]
    assert ms.needs_seat(_facts(seats=three), NOW) is None


def test_learner_tier_gets_one_seat_a_month():
    assert ms.needs_seat(_facts(tier="learner_tier", seats=[_seat(days_ago=9)]), NOW) is None


def test_a_withdrawn_seat_does_not_count():
    assert ms.needs_seat(_facts(tier="learner_tier", seats=[_seat(status="withdrawn", days_ago=9)]), NOW)


def test_one_open_seat_at_a_time():
    open_seat = _seat(status="pending", session_status="proposed", days_ago=10)
    assert ms.needs_seat(_facts(seats=[open_seat]), NOW) is None


def test_seats_are_spaced_a_week_apart():
    assert ms.needs_seat(_facts(seats=[_seat(days_ago=3)]), NOW) is None


def test_podded_learner_whose_pod_covers_the_month_needs_no_month_seat_but_credits_still_count():
    # Three pod sessions inside the paid month (3 Nov - 3 Dec) cover a Tutor month.
    pod = [PAID + timedelta(days=d) for d in (8, 15, 22)]
    assert ms.needs_seat(_facts(podded=True, pod=pod), NOW) is None
    w = ms.needs_seat(_facts(podded=True, pod=pod, credits=1), NOW)
    assert w.funded_by == "credit" and w.period_start is None


def test_podded_late_in_the_month_gets_make_ups_for_the_shortfall():
    """§2f item 8, developer 2026-10-18: make-ups cover the shortfall."""
    # Podded with one pod session left in this month, and one make-up already had.
    pod = [PAID + timedelta(days=25)]
    w = ms.needs_seat(_facts(podded=True, pod=pod, seats=[_seat(days_ago=10)]), NOW)
    assert w is not None and w.funded_by == "month"
    # A pod session outside the paid month doesn't count towards it.
    late = [PAID + timedelta(days=d) for d in (25, 40, 47)]
    assert ms.needs_seat(_facts(podded=True, pod=late, seats=[_seat(days_ago=10)]), NOW) is not None
    # One make-up + two pod sessions in the month = 3: covered.
    two = [PAID + timedelta(days=d) for d in (20, 27)]
    assert ms.needs_seat(_facts(podded=True, pod=two, seats=[_seat(days_ago=10)]), NOW) is None


def test_free_tier_with_no_credit_needs_nothing():
    assert ms.needs_seat(_facts(tier="free_tier", paid=()), NOW) is None


def test_outside_a_paid_month_needs_nothing():
    old = ((NOW - timedelta(days=45), "paid_month"),)
    assert ms.needs_seat(_facts(paid=old), NOW) is None


def test_gift_months_are_thirty_days():
    start = NOW - timedelta(days=29)
    assert ms.current_period([(start, "gift_month")], NOW) == (start, start + timedelta(days=30))
    assert ms.current_period([(NOW - timedelta(days=31), "gift_month")], NOW) is None


def test_a_future_dated_gift_month_is_not_current():
    future = NOW + timedelta(days=5)
    assert ms.current_period([(future, "gift_month")], NOW) is None


# ── credits ─────────────────────────────────────────────────────────────────

class _CreditDB:
    def __init__(self, rows):
        self.rows, self.inserted = rows, []

    def schema(self, _):
        return self

    def table(self, _):
        return self

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


def test_cancellation_forfeits_the_whole_balance_once():
    fake = _CreditDB([{"direction": "grant", "quantity": 1}, {"direction": "grant", "quantity": 1},
                      {"direction": "consume", "quantity": 1}])
    with patch.object(ms, "db", fake):
        assert ms.forfeit_on_cancellation("stu") == 1
    assert fake.inserted == [{"student_id": "stu", "direction": "forfeit",
                              "reason": "cancellation", "quantity": 1}]


def test_nothing_to_forfeit_writes_nothing():
    fake = _CreditDB([])
    with patch.object(ms, "db", fake):
        assert ms.forfeit_on_cancellation("stu") == 0
    assert fake.inserted == []


def test_only_complaint_or_admin_credits_are_granted_by_hand():
    with pytest.raises(ValueError):
        ms.grant_credit("stu", "tutor_issue")


# ── admin scheduling ────────────────────────────────────────────────────────

class _ScheduleDB:
    """Just enough of the client for makeup_service.schedule()."""
    def __init__(self, session, seats):
        self.data = {"makeup_session": [session], "makeup_session_participant": seats}
        self.updates = []
        self._table = None
        self._update = None

    def schema(self, _):
        return self

    def table(self, name):
        self._table, self._update = name, None
        return self

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def update(self, row):
        self._update = row
        return self

    def execute(self):
        if self._update is not None:
            self.updates.append((self._table, self._update))
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=self.data[self._table])


def _proposed(**over):
    row = {"id": "s1", "status": "proposed", "due_by": (NOW + timedelta(days=10)).isoformat(),
           "duration_minutes": 20, "proposed_tutor_id": "A", "proposed_at": MON_23_0600.isoformat()}
    row.update(over)
    return row


SEATS = [{"student_id": "a", "status": "pending"}, {"student_id": "b", "status": "pending"}]


def test_confirming_a_proposal_books_its_tutor_and_time():
    fake = _ScheduleDB(_proposed(), SEATS)
    with patch.object(ms, "db", fake), \
         patch.object(ms, "load_pools", return_value={"a": ("A",), "b": ("B", "A")}), \
         patch.object(ms, "load_busy", return_value={"A": [(MON_23_0600, MON_23_0600 + timedelta(minutes=20))]}):
        out = ms.schedule("s1")
    assert out["tutor_id"] == "A"
    assert fake.updates == [("makeup_session", {"tutor_id": "A", "scheduled_at": MON_23_0600.isoformat(),
                                                "status": "scheduled"})]


def test_an_override_tutor_must_be_in_every_learners_pool():
    fake = _ScheduleDB(_proposed(), SEATS)
    with patch.object(ms, "db", fake), \
         patch.object(ms, "load_pools", return_value={"a": ("A",), "b": ("B", "A")}), \
         patch.object(ms, "load_busy", return_value={}):
        with pytest.raises(ms.ScheduleRefused) as exc:
            ms.schedule("s1", tutor_id="B")
    assert exc.value.reason == "tutor_not_in_pool" and fake.updates == []


def test_a_busy_tutor_cannot_be_booked():
    clash = MON_23_0600 + timedelta(days=1)
    fake = _ScheduleDB(_proposed(), SEATS)
    with patch.object(ms, "db", fake), \
         patch.object(ms, "load_pools", return_value={"a": ("A",), "b": ("A",)}), \
         patch.object(ms, "load_busy", return_value={"A": [(clash, clash + timedelta(minutes=50))]}):
        with pytest.raises(ms.ScheduleRefused) as exc:
            ms.schedule("s1", starts_at=clash.isoformat())
    assert exc.value.reason == "tutor_busy"


def test_a_time_after_the_deadline_is_refused():
    late = NOW + timedelta(days=11)
    fake = _ScheduleDB(_proposed(), SEATS)
    with patch.object(ms, "db", fake), \
         patch.object(ms, "load_pools", return_value={"a": ("A",), "b": ("A",)}), \
         patch.object(ms, "load_busy", return_value={}):
        with pytest.raises(ms.ScheduleRefused) as exc:
            ms.schedule("s1", starts_at=late.isoformat())
    assert exc.value.reason == "after_deadline"
