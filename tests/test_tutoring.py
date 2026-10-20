"""Plan Phase 2 — the home/guest relation and the 1-on-1 lane.

The interesting rules here are the ones that behave differently on a platform
with one tutor than on a platform with forty, because that is the platform this
ships onto and every rule in §1.2/§1.4 was written for the other one.

What is worth asserting:

1. **Supply-dependent rules are advisory below the threshold and binding above
   it.** A 60% concentration cap reads 100% on day one; enforced hard it blocks
   every assignment that will ever be attempted. Getting this backwards makes the
   feature unusable in exactly the situation it launches into.
2. **The cap is checked against the PROJECTED share, not the current one.** A cap
   checked before the assignment always passes the assignment that breaches it.
3. **Slot expansion is timezone arithmetic**, and `day_of_week` is 0=Mon like
   Python's `weekday()` (fixed 2026-10-17: this file used to assert 0=Sun while
   the web schedule wrote 0=Mon, so every slot was offered a day early).
4. **A degraded read is not a zero.** A balance that failed to load must not
   render as "you have no sessions left".

`app.book_one_to_one` and `app.cancel_one_to_one` are not tested here — they are
SQL, and they were exercised against the live database when §74 was applied
(refusal words, the slot collision across two students, the append-only trigger,
the refund window). Mocking a Postgres function to assert what it returns would
test the mock.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.services import booking_service, matching_service


# ---------------------------------------------------------------------------
# Ratio parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("3:1", (3, 1)),
    ("2:1", (2, 1)),
    ("1:0", (1, 0)),          # a cohort with no guest rota at all
    (None, (3, 1)),
    ("", (3, 1)),
    ("garbage", (3, 1)),
    ("0:1", (3, 1)),          # a home share of zero is not a rota, it is a bug
    ("-1:2", (3, 1)),
])
def test_ratio_parsing_never_raises(raw, expected):
    """A malformed config value must degrade to the default. This parser has a
    twin inside app.generate_cohort_sessions, and if either one raised, a bad
    config row would stop a cohort's calendar being generated."""
    assert matching_service.parse_ratio(raw) == expected


# ---------------------------------------------------------------------------
# The concentration cap
# ---------------------------------------------------------------------------

def _links(rows):
    """Patch the two link reads concentration() makes."""
    def fake_conc(tutor_id):
        mine = [r for r in rows if r["tutor_id"] == tutor_id]
        total = {r["student_id"] for r in rows}
        return {
            "tutor_students": len({r["student_id"] for r in mine}),
            "total_students": len(total),
            "pct": round(100.0 * len({r["student_id"] for r in mine}) / len(total), 1)
            if total else 0.0,
        }
    return fake_conc


def test_cap_is_advisory_below_the_supply_threshold():
    """One tutor holds 100% of students by definition. Enforced hard, the cap
    blocks the only assignment anyone could ever make."""
    with (
        patch.object(matching_service, "active_tutor_count", return_value=1),
        patch.object(matching_service, "concentration",
                     _links([{"student_id": "s1", "tutor_id": "t1"}])),
        patch.object(matching_service, "_already_linked", return_value=False),
        patch.object(matching_service, "_has_any_active_link", return_value=False),
        patch.object(matching_service, "_config", return_value=dict(matching_service._DEFAULTS)),
    ):
        verdict = matching_service.evaluate_assignment("s2", "t1", "guest")

    assert verdict["allowed"] is True
    assert verdict["blocking"] == []
    assert verdict["concentration"]["projected_pct"] == 100.0
    assert any("advisory" in a for a in verdict["advisories"])
    assert verdict["supply"]["binding"] is False


def test_cap_binds_once_supply_is_there():
    with (
        patch.object(matching_service, "active_tutor_count", return_value=8),
        patch.object(matching_service, "concentration",
                     _links([{"student_id": f"s{i}", "tutor_id": "t1"} for i in range(9)]
                            + [{"student_id": "s9", "tutor_id": "t2"}])),
        patch.object(matching_service, "_already_linked", return_value=False),
        patch.object(matching_service, "_has_any_active_link", return_value=False),
        patch.object(matching_service, "_config", return_value=dict(matching_service._DEFAULTS)),
    ):
        verdict = matching_service.evaluate_assignment("s10", "t1", "guest")

    assert verdict["allowed"] is False
    assert any("concentration cap" in b for b in verdict["blocking"])
    assert verdict["supply"]["binding"] is True


def test_cap_uses_the_projected_share_not_the_current_one():
    """Nine of ten students on t1 is 90% and already over a 60% cap — but the
    number that decides the NEXT assignment has to include it. A cap evaluated
    on the pre-assignment share always waves through the one that breaches it."""
    rows = [{"student_id": f"s{i}", "tutor_id": "t1"} for i in range(3)]
    rows += [{"student_id": f"s{i}", "tutor_id": "t2"} for i in range(3, 6)]
    with (
        patch.object(matching_service, "active_tutor_count", return_value=8),
        patch.object(matching_service, "concentration", _links(rows)),
        patch.object(matching_service, "_already_linked", return_value=False),
        patch.object(matching_service, "_has_any_active_link", return_value=False),
        patch.object(matching_service, "_config", return_value=dict(matching_service._DEFAULTS)),
    ):
        verdict = matching_service.evaluate_assignment("s6", "t1", "guest")

    # 3/6 = 50% now; 4/7 = 57.1% projected. Both under 60, so this is allowed —
    # the assertion is that the number reported is the projected one.
    assert verdict["concentration"]["pct"] == 50.0
    assert verdict["concentration"]["projected_pct"] == 57.1


def test_an_unknown_role_is_refused():
    with (
        patch.object(matching_service, "active_tutor_count", return_value=8),
        patch.object(matching_service, "concentration",
                     _links([{"student_id": "s1", "tutor_id": "t1"}])),
        patch.object(matching_service, "_already_linked", return_value=False),
        patch.object(matching_service, "_has_any_active_link", return_value=False),
        patch.object(matching_service, "_config", return_value=dict(matching_service._DEFAULTS)),
    ):
        verdict = matching_service.evaluate_assignment("s2", "t1", "mentor")
    assert verdict["allowed"] is False


# ---------------------------------------------------------------------------
# Slot expansion
# ---------------------------------------------------------------------------

def _availability(day_of_week, start="09:00:00", end="11:00:00", tz="UTC"):
    return [{
        "id": "av-1", "tutor_id": "t1", "day_of_week": day_of_week,
        "start_time": start, "end_time": end, "timezone": tz,
    }]


def test_day_of_week_is_monday_zero():
    """app.tutor_availability.day_of_week is 0=Mon..6=Sun — what the web
    schedule writes (its grid is Mon..Sun), what the student's preferences and
    the matcher use, and Python's weekday(). Get it wrong and every slot lands
    one day out."""
    with (
        patch.object(booking_service, "_weekly_windows", return_value=_availability(0)),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        slots = booking_service.expand_slots(["t1"], 50, 14)

    assert slots, "expected slots inside a 14-day horizon"
    for s in slots:
        when = datetime.fromisoformat(s["starts_at"].replace("Z", "+00:00"))
        assert when.weekday() == 0, f"{s['starts_at']} is not a Monday"


def test_windows_are_read_in_the_tutors_own_zone():
    """09:00 Monday in Addis (UTC+3, no DST) is 06:00 UTC, on the same Monday."""
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(0, "09:00:00", "10:00:00", "Africa/Addis_Ababa")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        slots = booking_service.expand_slots(["t1"], 50, 14)
    assert slots
    for s in slots:
        when = datetime.fromisoformat(s["starts_at"].replace("Z", "+00:00"))
        assert (when.weekday(), when.hour, when.minute) == (0, 6, 0), s["starts_at"]


def test_a_window_may_run_to_midnight():
    """The schedule offers every hour, so the last window ends at 24:00:00 —
    which Postgres stores and datetime.time cannot parse."""
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(2, "23:00:00", "24:00:00")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        slots = booking_service.expand_slots(["t1"], 50, 14)
    assert slots and all(s["starts_at"][11:16] == "23:00" for s in slots)


def test_slots_never_run_past_the_window_end():
    """A 50-minute session cannot start at 10:30 in a window ending at 11:00."""
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(3, "09:00:00", "11:00:00")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        slots = booking_service.expand_slots(["t1"], 50, 14)

    starts = {s["starts_at"][11:16] for s in slots}
    assert starts == {"09:00", "09:50"}, starts


def test_a_shorter_duration_yields_more_slots():
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(3, "09:00:00", "11:00:00")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        fifty = booking_service.expand_slots(["t1"], 50, 7)
        twentyfive = booking_service.expand_slots(["t1"], 25, 7)
    assert len(twentyfive) > len(fifty)


def test_a_taken_slot_is_not_offered():
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(3, "09:00:00", "11:00:00")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        all_slots = booking_service.expand_slots(["t1"], 50, 14)
    first = all_slots[0]["starts_at"]

    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(3, "09:00:00", "11:00:00")),
        patch.object(booking_service, "_taken", return_value={("t1", first)}),
    ):
        remaining = booking_service.expand_slots(["t1"], 50, 14)

    assert first not in {s["starts_at"] for s in remaining}
    assert len(remaining) == len(all_slots) - 1


def test_an_unknown_timezone_keeps_the_tutor_visible():
    """A dropped tutor is invisible and unexplicable; a tutor shown at the wrong
    hour is at least a reportable bug. Neither is good, one is recoverable."""
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(3, tz="Mars/Olympus")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        slots = booking_service.expand_slots(["t1"], 50, 14)
    assert slots


def test_an_unparseable_window_is_skipped_not_fatal():
    bad = _availability(3)
    bad[0]["end_time"] = "not-a-time"
    with (
        patch.object(booking_service, "_weekly_windows", return_value=bad),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        assert booking_service.expand_slots(["t1"], 50, 14) == []


def test_no_tutors_means_no_query_and_no_slots():
    assert booking_service.expand_slots([], 50, 28) == []


def test_slots_are_ordered_soonest_first():
    with (
        patch.object(booking_service, "_weekly_windows",
                     return_value=_availability(3, "09:00:00", "12:00:00")),
        patch.object(booking_service, "_taken", return_value=set()),
    ):
        slots = booking_service.expand_slots(["t1"], 50, 21)
    assert slots == sorted(slots, key=lambda s: (s["starts_at"], s["tutor_id"]))


# ---------------------------------------------------------------------------
# The entitlement balance
# ---------------------------------------------------------------------------

def _ledger(rows):
    fake = MagicMock()
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .eq.return_value.order.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=rows)
    return fake


async def test_balance_is_a_fold_over_the_ledger():
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    rows = [
        {"direction": "grant", "quantity": 2, "expires_at": future},
        {"direction": "consume", "quantity": 1, "expires_at": None},
        {"direction": "refund", "quantity": 1, "expires_at": None},
        {"direction": "consume", "quantity": 1, "expires_at": None},
    ]
    with patch.object(booking_service, "db", _ledger(rows)):
        result = await booking_service.get_balance("s1")
    assert result["balance"] == 1
    assert result["degraded"] is False


async def test_an_expired_grant_stops_counting():
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = [
        {"direction": "grant", "quantity": 2, "expires_at": past},
        {"direction": "grant", "quantity": 1, "expires_at": None},
    ]
    with patch.object(booking_service, "db", _ledger(rows)):
        result = await booking_service.get_balance("s1")
    # The expired tier grant is gone; the bought top-up (expires_at NULL) rolls.
    assert result["balance"] == 1


async def test_a_failed_read_is_not_a_zero_balance():
    """"We could not check" and "you have none" are different sentences and lead
    to different actions. Rendering the first as the second turns a retry into a
    support ticket."""
    fake = MagicMock()
    fake.schema.side_effect = Exception("connection reset")
    with patch.object(booking_service, "db", fake):
        result = await booking_service.get_balance("s1")
    assert result["balance"] is None
    assert result["degraded"] is True


# ---------------------------------------------------------------------------
# The cycle grant
# ---------------------------------------------------------------------------

async def test_only_intensive_grants_one_to_ones():
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=30)
    with patch.object(booking_service, "_config",
                      return_value=dict(booking_service._DEFAULTS)):
        for tier in ("free_tier", "learner_tier", "tutor_tier"):
            result = await booking_service.grant_cycle_entitlement("s1", tier, start, end)
            assert result["granted"] == 0
            assert result["reason"] == "tier_grants_none"


async def test_a_repeated_cycle_grant_is_absorbed_not_doubled():
    """idx_session_entitlement_one_cycle_grant is UNIQUE on
    (student_id, kind, period_start). A retry, a double-trigger or a clock skew
    must not hand out a second month's sessions — losing that race is success."""
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=30)
    fake = MagicMock()
    fake.schema.return_value.table.return_value.insert.return_value.execute.side_effect = \
        Exception('duplicate key value violates unique constraint '
                  '"idx_session_entitlement_one_cycle_grant"')
    with (
        patch.object(booking_service, "db", fake),
        patch.object(booking_service, "_config", return_value=dict(booking_service._DEFAULTS)),
    ):
        result = await booking_service.grant_cycle_entitlement(
            "s1", "intensive_tier", start, end)
    assert result["granted"] == 0
    assert result["reason"] == "already_granted_this_cycle"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_every_refusal_word_has_a_readable_message():
    for word in booking_service.REFUSALS:
        assert booking_service.BookingRefused(word).message
        assert not booking_service.BookingRefused(word).message.startswith(word)


def test_an_unknown_refusal_still_produces_a_message():
    assert booking_service.BookingRefused("something_new").message


def test_the_refusal_word_is_pulled_out_of_a_postgrest_envelope():
    """The function raises single words precisely so this is a lookup rather
    than prose-parsing; the word arrives wrapped in an error envelope."""
    exc = Exception('{"code":"P0001","message":"not_your_tutor","details":null}')
    assert booking_service._refusal_from(exc) == "not_your_tutor"
    assert booking_service._refusal_from(Exception("connection reset by peer")) is None


async def test_booking_a_non_standard_duration_never_reaches_the_database():
    with pytest.raises(booking_service.BookingRefused) as caught:
        await booking_service.book("s1", "t1", "2026-09-01T10:00:00Z", 45)
    assert caught.value.reason == "invalid_duration"


# ---------------------------------------------------------------------------
# Roster shaping
# ---------------------------------------------------------------------------

def _roster_db(links):
    """Phase 6 changed the shape of this query and this helper with it.

    `get_roster` used to filter `.eq("state", "active")`; it now filters
    `.in_("state", ["active", "handoff_pending"])`, because filtering to
    `active` alone made a home tutor vanish from the student's own screen the
    moment a handoff began — see `test_handoff.py`. The mock chain has to match
    the real one or it asserts against a query nobody makes.
    """
    fake = MagicMock()
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .in_.return_value.order.return_value.execute.return_value = MagicMock(data=links)
    return fake


async def test_roster_separates_home_from_guests():
    """§2.1 asks for visually distinct treatment. One list with a role field is
    a client that will eventually forget to partition it."""
    links = [
        {"id": "l1", "tutor_id": "t1", "role": "home", "source": "referral",
         "state": "active", "started_at": "2026-08-01T00:00:00Z"},
        {"id": "l2", "tutor_id": "t2", "role": "guest", "source": "matched",
         "state": "active", "started_at": "2026-08-02T00:00:00Z"},
        {"id": "l3", "tutor_id": "t3", "role": "guest", "source": "matched",
         "state": "active", "started_at": "2026-08-03T00:00:00Z"},
    ]
    with (
        patch.object(matching_service, "db", _roster_db(links)),
        patch.object(matching_service, "_tutor_display", return_value={}),
        patch.object(matching_service, "active_tutor_count", return_value=8),
        patch.object(matching_service, "_config", return_value=dict(matching_service._DEFAULTS)),
    ):
        roster = await matching_service.get_roster("s1")

    assert roster["home"]["tutor_id"] == "t1"
    assert [g["tutor_id"] for g in roster["guests"]] == ["t2", "t3"]
    assert roster["guest_pool_complete"] is True
    assert roster["supply_limited"] is False


async def test_a_thin_guest_pool_says_so_rather_than_looking_empty():
    links = [{"id": "l1", "tutor_id": "t1", "role": "home", "source": "admin",
              "state": "active", "started_at": "2026-08-01T00:00:00Z"}]
    with (
        patch.object(matching_service, "db", _roster_db(links)),
        patch.object(matching_service, "_tutor_display", return_value={}),
        patch.object(matching_service, "active_tutor_count", return_value=1),
        patch.object(matching_service, "_config", return_value=dict(matching_service._DEFAULTS)),
    ):
        roster = await matching_service.get_roster("s1")

    assert roster["guests"] == []
    assert roster["guest_pool_complete"] is False
    # The distinction that matters: incomplete BECAUSE there is nobody to
    # complete it with, not because someone forgot.
    assert roster["supply_limited"] is True


async def test_a_referral_to_a_departed_tutor_is_not_a_preference():
    """The student keeps their attribution — and the referring tutor keeps the
    revenue share — while being matched to someone who can actually teach."""
    fake = MagicMock()
    signup = MagicMock(data=[{"tutor_id": "t-gone"}])
    tutor = MagicMock(data=[{"id": "t-gone", "status": "inactive", "deleted_at": None}])
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .limit.return_value.execute.side_effect = [signup, tutor]
    with patch.object(matching_service, "db", fake):
        assert matching_service.preferred_home_tutor("s1") is None


# ---------------------------------------------------------------------------
# Guest rotation
# ---------------------------------------------------------------------------

def _rotation_db(pool, booked):
    fake = MagicMock()
    links = MagicMock(data=[{"tutor_id": t} for t in pool])
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .eq.return_value.eq.return_value.execute.return_value = links
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .gte.return_value.in_.return_value.execute.return_value = \
        MagicMock(data=[{"tutor_id": t} for t in booked])
    return fake


def test_a_pool_of_one_is_never_flagged_as_skewed():
    """A pool of one is small, not lopsided. Flagging it would raise a warning on
    every student on a thin platform, which is every student today."""
    with patch.object(matching_service, "db", _rotation_db(["t2"], ["t2"] * 6)):
        r = matching_service.guest_rotation("s1", 90)
    assert r["pool_size"] == 1
    assert r["skewed"] is False


def test_one_guest_taking_everything_is_flagged():
    with patch.object(matching_service, "db",
                      _rotation_db(["t2", "t3", "t4"], ["t2"] * 5 + ["t3"])):
        r = matching_service.guest_rotation("s1", 90)
    assert r["top_share"] > 0.75
    assert r["skewed"] is True


def test_an_even_spread_is_not_flagged():
    with patch.object(matching_service, "db",
                      _rotation_db(["t2", "t3", "t4"], ["t2", "t3", "t4", "t2"])):
        r = matching_service.guest_rotation("s1", 90)
    assert r["skewed"] is False


# ---------------------------------------------------------------------------
# Marking a session delivered (O-18)
# ---------------------------------------------------------------------------
#
# O-18 asked for a sweep from `booked` to `completed`. A sweep would have been
# the WRONG answer, and these tests pin the reason: payout_service pays for
# `completed` rows, so inferring delivery from a past timestamp pays a tutor for
# a session nobody attended. The cohort domain already holds the weaker rule —
# "absence is never inferred" — and inferring delivery is worse, because money
# moves rather than an accusation being made.

async def test_an_invalid_outcome_never_reaches_the_database():
    with pytest.raises(booking_service.BookingRefused) as caught:
        await booking_service.mark_outcome("s1", "probably_happened", "t1")
    assert caught.value.reason == "invalid_outcome"


async def test_marking_delivered_calls_the_function_and_says_so():
    fake = MagicMock()
    with patch.object(booking_service, "db", fake):
        result = await booking_service.mark_outcome("sess-1", "completed", "t1")
    fake.schema.return_value.rpc.assert_called_once()
    name, params = fake.schema.return_value.rpc.call_args[0]
    assert name == "mark_one_to_one_outcome"
    assert params == {"p_session": "sess-1", "p_outcome": "completed"}
    assert result["outcome"] == "completed"


async def test_a_no_show_does_not_return_the_entitlement():
    """The slot was held and the tutor turned up. The cancellation window is
    what a student uses when they cannot make it; refunding a no-show would make
    the window meaningless."""
    with patch.object(booking_service, "db", MagicMock()):
        result = await booking_service.mark_outcome("sess-1", "no_show", "t1")
    assert result["entitlement_returned"] is False
    assert "not returned" in result["message"]


@pytest.mark.parametrize("word", [
    "not_your_session",   # a student cannot mark their own session delivered
    "not_markable",       # already marked, or cancelled
    "not_yet",            # marking a future session would bill for a lesson
                          # that has not happened
    "no_such_session",
])
async def test_every_refusal_from_the_function_is_surfaced(word):
    fake = MagicMock()
    fake.schema.return_value.rpc.return_value.execute.side_effect = Exception(
        '{"code":"P0001","message":"%s"}' % word)
    with patch.object(booking_service, "db", fake):
        with pytest.raises(booking_service.BookingRefused) as caught:
            await booking_service.mark_outcome("sess-1", "completed", "t1")
    assert caught.value.reason == word
    assert caught.value.message


async def test_a_network_fault_while_marking_is_not_a_refusal():
    """Same rule as booking: a dropped connection must not be shown to a tutor
    as "that is not your session"."""
    fake = MagicMock()
    fake.schema.return_value.rpc.return_value.execute.side_effect = \
        Exception("connection reset by peer")
    with patch.object(booking_service, "db", fake):
        with pytest.raises(Exception) as caught:
            await booking_service.mark_outcome("sess-1", "completed", "t1")
    assert not isinstance(caught.value, booking_service.BookingRefused)


async def test_the_unmarked_report_degrades_to_empty_rather_than_raising():
    """It runs inside /tasks/daily beside jobs that must not be stopped by it."""
    fake = MagicMock()
    fake.schema.return_value.rpc.return_value.execute.side_effect = Exception("boom")
    with patch.object(booking_service, "db", fake):
        assert await booking_service.unmarked_sessions() == []
