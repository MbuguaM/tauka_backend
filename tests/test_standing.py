"""Plan Phase 6 — tutor standing (§1.6).

Standing gates assignment, which is upstream of pay, so the tests that matter
are about what it refuses to do rather than what it computes.

What is worth asserting:

1. **The ladder is walked downward.** A tutor who clears `lead` must not be
   stopped at `senior` by loop order. This is a one-character bug that only
   shows up once somebody is actually good enough to be promoted twice, i.e.
   years after it is written.
2. **A missing input is not a zero.** `avg_rating` is None when nobody has rated
   a tutor. A tutor nobody has rated has not been rated badly, and `None < 4.3`
   raising or coercing to 0 are two different wrong answers.
3. **Whole months are counted on the calendar.** `days // 30` gives twelve
   "months" in 360 days, so every threshold arrives early and the error grows
   with the threshold.
4. **The first-pick gate is advisory below the supply threshold.** On a
   one-tutor platform where everyone is `provisional`, a binding gate refuses
   every assignment that will ever be attempted.
5. **Mentorship is NOT supply-waived.** Unlike first pick, waiving it would put
   a tutor with no delivered sessions in charge of one who has them.
6. **An unchanged verdict writes no new row.** `tutor_standing` is a history of
   transitions; a row a month per tutor destroys `effective_from`'s meaning.
7. **No telemetry input reaches `inputs`.** The one rule in CLAUDE.md that this
   table is most tempted to break.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services import standing_service


LADDER = {
    "established": {"min_sessions": 20, "min_months": 3},
    "senior": {"min_sessions": 100, "min_months": 12,
               "min_avg_rating": 4.3, "min_ratings": 5,
               "min_retention_months": 4},
    "lead": {"min_sessions": 250, "min_months": 24,
             "min_avg_rating": 4.5, "min_ratings": 15,
             "min_retention_months": 6, "min_retained_referrals": 5},
}


def _inputs(**over):
    base = {
        "sessions": 0, "avg_rating": None, "rating_count": 0,
        "tenure_months": 0, "mean_retention_months": 0.0,
        "retained_referrals": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def test_a_brand_new_tutor_is_provisional():
    """The state of every tutor on this platform today, and for months."""
    v = standing_service.verdict(_inputs(), LADDER)
    assert v["standing"] == "provisional"
    assert v["next"] == "established"
    assert v["misses"], "a refusal with no reason is not actionable"


def test_the_ladder_is_walked_downward_so_lead_is_reachable():
    """Clearing `lead` must return `lead`, not stop at `senior`."""
    v = standing_service.verdict(_inputs(
        sessions=400, avg_rating=4.9, rating_count=40,
        tenure_months=30, mean_retention_months=9.0, retained_referrals=8,
    ), LADDER)
    assert v["standing"] == "lead"


def test_one_missed_criterion_holds_the_whole_rung():
    """Every criterion on a rung, not any. A tutor with 400 sessions and four
    ratings is not senior — the rating sample is the point of `min_ratings`."""
    v = standing_service.verdict(_inputs(
        sessions=400, avg_rating=5.0, rating_count=4,
        tenure_months=30, mean_retention_months=9.0, retained_referrals=8,
    ), LADDER)
    assert v["standing"] == "established"


def test_no_ratings_is_not_a_bad_rating():
    """`avg_rating=None` must fail a rating threshold without raising and
    without being read as zero. A tutor nobody has rated has not been rated
    badly."""
    v = standing_service.verdict(_inputs(
        sessions=400, avg_rating=None, rating_count=0,
        tenure_months=30, mean_retention_months=9.0, retained_referrals=8,
    ), LADDER)
    assert v["standing"] == "established"
    assert any("none yet" in m for m in
               standing_service._meets(LADDER["senior"], _inputs(
                   sessions=400, avg_rating=None, rating_count=0))[1])


def test_an_unknown_criterion_refuses_the_rung_rather_than_ignoring_it():
    """A rubric hand-edited into a shape this code does not implement must not
    silently drop the constraint — dropping it promotes people."""
    ladder = {"established": {"min_sessions": 1, "min_unicorns": 3}}
    v = standing_service.verdict(_inputs(sessions=99), ladder)
    assert v["standing"] == "provisional"


def test_a_broken_rubric_falls_back_rather_than_stopping_the_run():
    with patch.object(standing_service, "_config",
                      return_value={"standing.rubric": "{not json"}):
        assert "established" in standing_service.rubric()


# ---------------------------------------------------------------------------
# Month arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [
    (datetime(2025, 1, 15, tzinfo=timezone.utc), datetime(2025, 1, 31, tzinfo=timezone.utc), 0),
    (datetime(2025, 1, 15, tzinfo=timezone.utc), datetime(2025, 2, 14, tzinfo=timezone.utc), 0),
    (datetime(2025, 1, 15, tzinfo=timezone.utc), datetime(2025, 2, 15, tzinfo=timezone.utc), 1),
    (datetime(2025, 1, 15, tzinfo=timezone.utc), datetime(2026, 1, 15, tzinfo=timezone.utc), 12),
    # The one `days // 30` gets wrong: 360 days is not twelve months.
    (datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 12, 27, tzinfo=timezone.utc), 11),
])
def test_months_are_counted_on_the_calendar(a, b, expected):
    assert standing_service._months_between(a, b) == expected


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------

def _cfg(**over):
    values = {"standing.first_pick_min": "senior", "match.min_tutor_supply": "4"}
    values.update(over)
    return lambda keys: values


def test_first_pick_is_advisory_below_the_supply_threshold():
    """One tutor, `provisional`, and a gate that must not block the only
    assignment anyone could make. `eligible` is permissive; `meets_threshold`
    still tells the truth."""
    with (
        patch.object(standing_service, "_config", _cfg()),
        patch.object(standing_service.matching_service, "active_tutor_count", return_value=1),
        patch.object(standing_service, "current_standing", return_value={"standing": "provisional"}),
    ):
        gate = standing_service.first_pick_eligible("t1")
    assert gate["eligible"] is True
    assert gate["meets_threshold"] is False
    assert gate["binding"] is False
    assert "advisory" in gate["reason"]


def test_first_pick_binds_once_there_is_supply():
    with (
        patch.object(standing_service, "_config", _cfg()),
        patch.object(standing_service.matching_service, "active_tutor_count", return_value=12),
        patch.object(standing_service, "current_standing", return_value={"standing": "established"}),
    ):
        gate = standing_service.first_pick_eligible("t1")
    assert gate["eligible"] is False
    assert gate["binding"] is True


def test_a_nonsense_first_pick_threshold_degrades_to_senior():
    """A config typo must not make every tutor eligible for everything."""
    with (
        patch.object(standing_service, "_config", _cfg(**{"standing.first_pick_min": "wizard"})),
        patch.object(standing_service.matching_service, "active_tutor_count", return_value=12),
        patch.object(standing_service, "current_standing", return_value={"standing": "established"}),
    ):
        gate = standing_service.first_pick_eligible("t1")
    assert gate["required"] == "senior"
    assert gate["eligible"] is False


def test_mentorship_is_never_supply_waived():
    """Unlike first pick. Waiving it would put a tutor with no delivered
    sessions in charge of one who has them, and nobody is waiting to be
    mentored on a one-tutor platform anyway."""
    with patch.object(standing_service, "current_standing",
                      return_value={"standing": "provisional"}):
        assert standing_service.mentorship_eligible("t1")["eligible"] is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_an_unchanged_verdict_opens_no_new_row():
    """`tutor_standing` is a history of transitions. A row per tutor per month
    buries the two that matter and makes `effective_from` meaningless."""
    table = MagicMock()
    with (
        patch.object(standing_service, "current_standing",
                     return_value={"id": "row1", "standing": "senior"}),
        patch.object(standing_service.db, "schema", return_value=MagicMock(table=table)),
    ):
        out = standing_service._record("t1", "senior", {}, datetime.now(timezone.utc))
    assert out["changed"] is False
    table.return_value.insert.assert_not_called()


def test_a_changed_verdict_closes_the_old_row_before_opening_a_new_one():
    """The partial unique index permits exactly one open row per tutor, so the
    reverse order is refused by the database."""
    calls = []
    table = MagicMock()
    table.return_value.update.side_effect = lambda payload: (
        calls.append(("update", payload)) or MagicMock())
    table.return_value.insert.side_effect = lambda payload: (
        calls.append(("insert", payload)) or MagicMock())

    with (
        patch.object(standing_service, "current_standing",
                     return_value={"id": "row1", "standing": "established"}),
        patch.object(standing_service.db, "schema", return_value=MagicMock(table=table)),
    ):
        out = standing_service._record("t1", "senior", {}, datetime.now(timezone.utc))

    assert out["changed"] is True and out["from"] == "established"
    kinds = [c[0] for c in calls]
    assert kinds.index("update") < kinds.index("insert")
    assert calls[kinds.index("update")][1].get("effective_to")


# ---------------------------------------------------------------------------
# The telemetry prohibition
# ---------------------------------------------------------------------------

def test_gathered_inputs_contain_no_telemetry_field():
    """CLAUDE.md: never gate a feature on an activity or telemetry number.
    Standing gates assignment, so this is the strongest form of that rule. The
    `inputs` snapshot is the audit trail for it — if a field from
    `activity_summary` or `ai_usage` ever appears here, the snapshot is what
    makes it visible."""
    with (
        patch.object(standing_service.payout_service, "quality_inputs",
                     return_value={"mean_stars": 4.5, "rating_count": 9}),
        patch.object(standing_service, "retention_inputs",
                     return_value={"tenure_months": 5, "mean_retention_months": 3.0,
                                   "link_count": 2, "active_links": 2}),
        patch.object(standing_service, "referral_inputs",
                     return_value={"signups": 3, "retained": 2}),
    ):
        got = standing_service.gather_inputs("t1", {"cohort": 4, "one_to_one": 2,
                                                    "minutes": 300})

    assert got["sessions"] == 6
    forbidden = ("active_minutes", "streak", "ai_", "tokens", "activity", "usage")
    assert not [k for k in got if any(f in k for f in forbidden)], got.keys()


def test_a_failed_retention_read_promotes_nobody():
    """Zero, not a guess. A failed read that returned a long tenure would
    promote a tutor on the strength of a database error."""
    failing = MagicMock()
    failing.table.side_effect = RuntimeError("boom")
    with patch.object(standing_service.db, "schema", return_value=failing):
        got = standing_service.retention_inputs("t1")
    assert got == {"tenure_months": 0, "mean_retention_months": 0.0,
                   "link_count": 0, "active_links": 0}
