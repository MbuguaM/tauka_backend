"""Plan Phase 3a — the payout rail.

This is the first code in the system where being wrong costs someone money, so
the tests are about arithmetic and about refusing to guess:

1. **The cadence guard.** `/tasks/daily` fires every day and these jobs must fire
   once a month. A guard that is wrong in the permissive direction pays everyone
   thirty times.
2. **Period arithmetic.** "The month that just ended" computed by subtracting 30
   days drifts, and eventually pays one month twice.
3. **Integer money, end to end.** Two basis-point multipliers applied to minor
   units, divided once. Floats here produce a rate that differs by a fraction of
   a shilling every month and never reconciles.
4. **Refusing to guess a country.** A tutor with no `country_code`, or no rate
   card for it, is *unrateable* — reported, not defaulted. Guessing means paying
   someone a rate from a place they do not live in.
5. **Quality never punishes thin evidence.** Too few ratings means ×1.0, not a
   cut.

The SQL is not re-tested here — `app.append_float_ledger`, the append-only
trigger, the FX CHECK and the uniqueness rules were exercised against the live
database when §75 was applied.
"""
import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.services import payout_service as ps


# ---------------------------------------------------------------------------
# Cadence and periods
# ---------------------------------------------------------------------------

def _cfg(**overrides):
    values = dict(ps._DEFAULTS)
    values.update(overrides)
    return patch.object(ps, "_config", return_value=values)


def test_the_guard_fires_on_the_cadence_day_and_no_other():
    with _cfg():
        assert ps.is_cadence_day(date(2026, 9, 1)) is True
        for day in (2, 3, 15, 28, 30):
            assert ps.is_cadence_day(date(2026, 9, day)) is False, day


def test_a_cadence_day_past_the_end_of_the_month_still_fires():
    """A value of 31 must not skip February entirely — the job would simply
    never run in a month that is too short, and nobody would be paid."""
    with _cfg(**{"payout.cadence_day": "31"}):
        assert ps.is_cadence_day(date(2026, 2, 28)) is True   # clamped
        assert ps.is_cadence_day(date(2026, 3, 31)) is True
        assert ps.is_cadence_day(date(2026, 3, 30)) is False


def test_a_nonsense_cadence_day_falls_back_to_the_first():
    with _cfg(**{"payout.cadence_day": "banana"}):
        assert ps.is_cadence_day(date(2026, 9, 1)) is True
    with _cfg(**{"payout.cadence_day": "0"}):
        assert ps.is_cadence_day(date(2026, 9, 1)) is True


@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 1),  (date(2026, 8, 1),  date(2026, 8, 31))),
    (date(2026, 3, 1),  (date(2026, 2, 1),  date(2026, 2, 28))),
    (date(2026, 1, 1),  (date(2025, 12, 1), date(2025, 12, 31))),
])
def test_previous_period_is_a_calendar_month_not_thirty_days(today, expected):
    """Subtracting 30 days drifts and eventually pays one month twice."""
    assert ps.previous_period(today) == expected


# ---------------------------------------------------------------------------
# Rate computation
# ---------------------------------------------------------------------------

def test_tenure_is_whole_months_not_days():
    assert ps.tenure_months("2026-01-15T00:00:00Z", date(2026, 8, 1)) == 7
    assert ps.tenure_months("2026-08-31T00:00:00Z", date(2026, 9, 1)) == 1
    assert ps.tenure_months(None, date(2026, 9, 1)) == 0
    assert ps.tenure_months("not a date", date(2026, 9, 1)) == 0


def test_tenure_ladder_is_flat_topped():
    """A rate that keeps climbing forever is a promise nobody costed."""
    assert ps._ladder_bp(0, ps.TENURE_LADDER_BP) == 10000
    assert ps._ladder_bp(5, ps.TENURE_LADDER_BP) == 10000
    assert ps._ladder_bp(6, ps.TENURE_LADDER_BP) == 10500
    assert ps._ladder_bp(13, ps.TENURE_LADDER_BP) == 11000
    assert ps._ladder_bp(600, ps.TENURE_LADDER_BP) == 11500


def _rate_env(card, quality):
    return (
        patch.object(ps, "_current_rate_card", return_value=card),
        patch.object(ps, "quality_inputs", return_value=quality),
    )


CARD = {"id": "card-ke", "country_code": "KE", "currency": "KES",
        "base_rate_minor": 100000, "half_session_bp": 5000, "effective_to": None}


def test_the_rate_is_base_times_two_multipliers_in_integers():
    card, quality = _rate_env(CARD, {"mean_stars": 4.8, "rating_count": 20})
    with card, quality:
        rate = ps.compute_rate(
            {"id": "t1", "created_at": "2024-01-01T00:00:00Z", "country_code": "KE"},
            date(2026, 9, 1))

    # 24+ months → 11500 bp; 4.8 stars → 11000 bp.
    # 100000 * 11500 * 11000 // (10000*10000) = 126500
    assert rate["tenure_multiplier_bp"] == 11500
    assert rate["quality_multiplier_bp"] == 11000
    assert rate["computed_rate_minor"] == 126500
    assert isinstance(rate["computed_rate_minor"], int)
    assert rate["currency"] == "KES"


def test_thin_rating_evidence_is_neutral_not_a_penalty():
    """A single bad night from a student having a bad day must not cut pay."""
    card, quality = _rate_env(CARD, {"mean_stars": 1.0, "rating_count": 2})
    with card, quality:
        rate = ps.compute_rate(
            {"id": "t1", "created_at": "2026-08-01T00:00:00Z", "country_code": "KE"},
            date(2026, 9, 1))
    assert rate["quality_multiplier_bp"] == 10000
    assert rate["computed_rate_minor"] == 100000


def test_enough_evidence_of_poor_quality_does_reduce_the_rate():
    card, quality = _rate_env(CARD, {"mean_stars": 2.9, "rating_count": 30})
    with card, quality:
        rate = ps.compute_rate(
            {"id": "t1", "created_at": "2026-08-01T00:00:00Z", "country_code": "KE"},
            date(2026, 9, 1))
    assert rate["quality_multiplier_bp"] == 9500
    assert rate["computed_rate_minor"] == 95000


def test_a_tutor_with_no_country_is_unrateable_not_defaulted():
    """Guessing a country means paying someone a rate from a place they do not
    live in, and a silent fallback would make that invisible."""
    with patch.object(ps, "_current_rate_card", return_value=CARD):
        assert ps.compute_rate({"id": "t1", "created_at": "2026-01-01T00:00:00Z"},
                               date(2026, 9, 1)) is None
        assert ps.compute_rate({"id": "t1", "created_at": "2026-01-01T00:00:00Z",
                                "country_code": ""}, date(2026, 9, 1)) is None


def test_a_country_with_no_card_is_unrateable():
    with patch.object(ps, "_current_rate_card", return_value=None):
        assert ps.compute_rate({"id": "t1", "created_at": "2026-01-01T00:00:00Z",
                                "country_code": "SE"}, date(2026, 9, 1)) is None


def test_the_rate_keeps_the_inputs_it_was_derived_from():
    """What makes a past rate *explicable* rather than merely reproducible."""
    card, quality = _rate_env(CARD, {"mean_stars": 4.4, "rating_count": 11})
    with card, quality:
        rate = ps.compute_rate(
            {"id": "t1", "created_at": "2025-09-01T00:00:00Z", "country_code": "KE"},
            date(2026, 9, 1))
    assert rate["inputs"]["tenure_months"] == 12
    assert rate["inputs"]["mean_stars"] == 4.4
    assert rate["inputs"]["rating_count"] == 11


def test_the_rate_starts_on_the_first_of_the_month():
    card, quality = _rate_env(CARD, {"mean_stars": None, "rating_count": 0})
    with card, quality:
        rate = ps.compute_rate(
            {"id": "t1", "created_at": "2026-01-01T00:00:00Z", "country_code": "KE"},
            date(2026, 9, 17))
    assert rate["effective_from"] == "2026-09-01"


# ---------------------------------------------------------------------------
# Delivered sessions
# ---------------------------------------------------------------------------

def _sessions_db(cohort_rows, one_to_one_rows, members):
    fake = MagicMock()

    def table(name):
        t = MagicMock()
        if name == "cohort_session":
            t.select.return_value.gte.return_value.lt.return_value.execute.return_value = \
                MagicMock(data=cohort_rows)
        elif name == "one_to_one_session":
            t.select.return_value.gte.return_value.lt.return_value.execute.return_value = \
                MagicMock(data=one_to_one_rows)
        elif name == "cohort_memberships":
            t.select.return_value.in_.return_value.eq.return_value.execute.return_value = \
                MagicMock(data=members)
        return t

    fake.schema.return_value.table.side_effect = table
    return fake


def test_only_completed_sessions_are_paid_for():
    """A `scheduled` session in the past is one nobody marked. Paying for it
    would pay for attendance that was never confirmed — the cohort domain's
    'absence is never inferred' rule, applied to money."""
    cohort = [
        {"cohort_id": "c1", "lead_tutor_id": "t1", "duration_minutes": 50, "status": "completed"},
        {"cohort_id": "c1", "lead_tutor_id": "t1", "duration_minutes": 50, "status": "scheduled"},
        {"cohort_id": "c1", "lead_tutor_id": "t1", "duration_minutes": 50, "status": "cancelled"},
    ]
    with patch.object(ps, "db", _sessions_db(cohort, [], [{"cohort_id": "c1", "user_id": "s1"}])):
        out = ps.delivered_sessions(date(2026, 8, 1), date(2026, 8, 31))
    assert out["t1"]["cohort"] == 1


def test_the_rota_pays_the_tutor_who_LED_not_the_cohort_owner():
    """cohort_session.lead_tutor_id may be a guest. §1.4 pays the delivering
    tutor, and after Phase 2 that column is what 'delivering' means."""
    cohort = [
        {"cohort_id": "c1", "lead_tutor_id": "guest-1", "duration_minutes": 50, "status": "completed"},
    ]
    with patch.object(ps, "db", _sessions_db(cohort, [], [{"cohort_id": "c1", "user_id": "s1"}])):
        out = ps.delivered_sessions(date(2026, 8, 1), date(2026, 8, 31))
    assert "guest-1" in out
    assert out["guest-1"]["cohort"] == 1


def test_both_lanes_are_counted_and_students_recorded():
    cohort = [{"cohort_id": "c1", "lead_tutor_id": "t1", "duration_minutes": 50,
               "status": "completed"}]
    one = [{"tutor_id": "t1", "student_id": "s9", "duration_minutes": 25,
            "status": "completed"}]
    members = [{"cohort_id": "c1", "user_id": "s1"}, {"cohort_id": "c1", "user_id": "s2"}]
    with patch.object(ps, "db", _sessions_db(cohort, one, members)):
        out = ps.delivered_sessions(date(2026, 8, 1), date(2026, 8, 31))
    assert out["t1"]["cohort"] == 1
    assert out["t1"]["one_to_one"] == 1
    assert out["t1"]["minutes"] == 75
    # The students map is what makes the referral share computable at all.
    assert out["t1"]["students"] == {"s1": 1, "s2": 1, "s9": 1}


# ---------------------------------------------------------------------------
# The referral share
# ---------------------------------------------------------------------------

def _share_db(signups):
    fake = MagicMock()
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .gt.return_value.execute.return_value = MagicMock(data=signups)
    return fake


RATE_KES = {"id": "rv1", "currency": "KES", "computed_rate_minor": 100000}


def test_a_referring_tutor_earns_a_share_of_what_another_tutor_taught():
    """§3.1b: two different tutors can be owed money from one session — the
    delivering tutor for teaching it, the referring tutor for the share."""
    sessions = {"deliverer": {"cohort": 0, "one_to_one": 2, "minutes": 100,
                              "students": {"s1": 2}}}
    with patch.object(ps, "db", _share_db([{"student_id": "s1", "vested_share_bp": 500}])):
        share = ps._referral_share_minor("referrer", sessions,
                                         {"deliverer": RATE_KES}, "KES")
    # 100000 per session × 2 sessions × 5% = 10000
    assert share == 10000


def test_a_cohort_share_is_divided_by_the_cohort_size():
    """One session with four members must not pay four full shares."""
    sessions = {"deliverer": {"cohort": 1, "one_to_one": 0, "minutes": 50,
                              "students": {"s1": 1, "s2": 1, "s3": 1, "s4": 1}}}
    with patch.object(ps, "db", _share_db([{"student_id": "s1", "vested_share_bp": 1000}])):
        share = ps._referral_share_minor("referrer", sessions,
                                         {"deliverer": RATE_KES}, "KES")
    # 100000 × 1 × 10% / 4 members = 2500
    assert share == 2500


def test_a_tutor_takes_no_referral_share_of_their_own_teaching():
    """That session is already paid on their own line; taking a share as well
    would pay the same person twice for one session."""
    sessions = {"referrer": {"cohort": 0, "one_to_one": 3, "minutes": 150,
                             "students": {"s1": 3}}}
    with patch.object(ps, "db", _share_db([{"student_id": "s1", "vested_share_bp": 1000}])):
        assert ps._referral_share_minor("referrer", sessions,
                                        {"referrer": RATE_KES}, "KES") == 0


def test_a_cross_currency_share_is_skipped_not_converted():
    """Converting here would apply an FX rate at computation time rather than at
    settlement, which §11.3 forbids."""
    sessions = {"deliverer": {"cohort": 0, "one_to_one": 2, "minutes": 100,
                              "students": {"s1": 2}}}
    eur = {"id": "rv2", "currency": "EUR", "computed_rate_minor": 3000}
    with patch.object(ps, "db", _share_db([{"student_id": "s1", "vested_share_bp": 500}])):
        assert ps._referral_share_minor("referrer", sessions,
                                        {"deliverer": eur}, "KES") == 0


def test_no_vested_signups_means_no_share_and_no_further_reads():
    sessions = {"deliverer": {"cohort": 0, "one_to_one": 5, "minutes": 250,
                              "students": {"s1": 5}}}
    with patch.object(ps, "db", _share_db([])):
        assert ps._referral_share_minor("referrer", sessions,
                                        {"deliverer": RATE_KES}, "KES") == 0


# ---------------------------------------------------------------------------
# Vesting
# ---------------------------------------------------------------------------

def _vesting_db(signups, students):
    fake = MagicMock()
    calls = {"updates": []}

    def table(name):
        t = MagicMock()
        if name == "referral_signup":
            t.select.return_value.execute.return_value = MagicMock(data=signups)
            t.update.side_effect = lambda payload: (
                calls["updates"].append(payload),
                MagicMock(eq=MagicMock(return_value=MagicMock(
                    execute=MagicMock(return_value=MagicMock(data=[]))))))[1]
        elif name == "student":
            t.select.return_value.in_.return_value.execute.return_value = \
                MagicMock(data=students)
        return t

    fake.schema.return_value.table.side_effect = table
    return fake, calls


async def test_vesting_waits_for_the_minimum_tenure():
    """Plan §8 names referral fraud — a tutor referring students they control —
    and says a minimum-tenure gate covers more of it than the cap does."""
    recent = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    fake, calls = _vesting_db(
        [{"id": "r1", "student_id": "s1", "tutor_id": "t1",
          "signed_up_at": recent, "vested_share_bp": 0}],
        [{"id": "s1", "account_status": "active", "deleted_at": None}])
    with patch.object(ps, "db", fake), _cfg():
        result = await ps.recompute_vesting()
    assert result["updated"] == 0
    assert calls["updates"] == []


async def test_vesting_rises_once_the_tenure_is_there():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    fake, calls = _vesting_db(
        [{"id": "r1", "student_id": "s1", "tutor_id": "t1",
          "signed_up_at": old, "vested_share_bp": 0}],
        [{"id": "s1", "account_status": "active", "deleted_at": None}])
    with patch.object(ps, "db", fake), _cfg():
        result = await ps.recompute_vesting()
    assert result["updated"] == 1
    assert calls["updates"][0]["vested_share_bp"] == 1000


async def test_vesting_only_ratchets_up():
    """A student who lapses and returns does not restart the tutor's share at
    zero, and a bad month cannot claw back a share already earned."""
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    fake, calls = _vesting_db(
        [{"id": "r1", "student_id": "s1", "tutor_id": "t1",
          "signed_up_at": old, "vested_share_bp": 1000}],  # already above the band
        [{"id": "s1", "account_status": "active", "deleted_at": None}])
    with patch.object(ps, "db", fake), _cfg():
        result = await ps.recompute_vesting()
    assert result["updated"] == 0
    assert calls["updates"] == []


async def test_a_departed_student_stops_accruing():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    fake, calls = _vesting_db(
        [{"id": "r1", "student_id": "s1", "tutor_id": "t1",
          "signed_up_at": old, "vested_share_bp": 250}],
        [{"id": "s1", "account_status": "active", "deleted_at": "2026-08-01T00:00:00Z"}])
    with patch.object(ps, "db", fake), _cfg():
        result = await ps.recompute_vesting()
    assert result["updated"] == 0


# ---------------------------------------------------------------------------
# Reconciliation and the alert
# ---------------------------------------------------------------------------

def _ledger_db(rows):
    fake = MagicMock()
    fake.schema.return_value.table.return_value.select.return_value.order.return_value \
        .order.return_value.execute.return_value = MagicMock(data=rows)
    return fake


async def test_the_fold_is_computed_per_currency_and_never_mixed():
    rows = [
        {"currency": "USD", "direction": "credit", "amount_minor": 100000,
         "balance_after_minor": 100000, "occurred_at": "1", "created_at": "1"},
        {"currency": "KES", "direction": "credit", "amount_minor": 5000000,
         "balance_after_minor": 5000000, "occurred_at": "2", "created_at": "2"},
        {"currency": "USD", "direction": "debit", "amount_minor": 25000,
         "balance_after_minor": 75000, "occurred_at": "3", "created_at": "3"},
    ]
    with patch.object(ps, "db", _ledger_db(rows)):
        state = await ps.reconcile()
    assert state["currencies"]["USD"]["fold_minor"] == 75000
    assert state["currencies"]["KES"]["fold_minor"] == 5000000
    assert all(c["agrees"] for c in state["currencies"].values())


async def test_a_mismatch_between_fold_and_stamp_is_reported():
    """balance_after_minor is redundant with the fold ON PURPOSE — without it
    there would be nothing for a reconciliation to disagree with."""
    rows = [
        {"currency": "USD", "direction": "credit", "amount_minor": 100000,
         "balance_after_minor": 999999, "occurred_at": "1", "created_at": "1"},
    ]
    with patch.object(ps, "db", _ledger_db(rows)):
        state = await ps.reconcile()
    assert state["currencies"]["USD"]["agrees"] is False


async def test_the_alert_uses_a_per_currency_threshold():
    """A single number cannot mean anything across KES and EUR at once."""
    rows = [
        {"currency": "USD", "direction": "credit", "amount_minor": 10000,
         "balance_after_minor": 10000, "occurred_at": "1", "created_at": "1"},
        {"currency": "KES", "direction": "credit", "amount_minor": 10000,
         "balance_after_minor": 10000, "occurred_at": "2", "created_at": "2"},
    ]
    with (
        patch.object(ps, "db", _ledger_db(rows)),
        _cfg(**{"payout.float_low_balance_minor": '{"USD":50000,"KES":5000000}'}),
        patch("app.config.settings.admin_email", ""),
    ):
        state = await ps.check_float_balance()
    low = {x["currency"] for x in state["low"]}
    assert low == {"USD", "KES"}


async def test_a_currency_with_no_threshold_is_reported_but_not_alerted():
    """Comparing SEK against a USD threshold would be wrong by three orders of
    magnitude, so an unset currency is simply not alerted on."""
    rows = [
        {"currency": "SEK", "direction": "credit", "amount_minor": 1,
         "balance_after_minor": 1, "occurred_at": "1", "created_at": "1"},
    ]
    with (
        patch.object(ps, "db", _ledger_db(rows)),
        _cfg(**{"payout.float_low_balance_minor": '{"USD":50000}'}),
        patch("app.config.settings.admin_email", ""),
    ):
        state = await ps.check_float_balance()
    assert state["low"] == []
    assert "SEK" in state["currencies"]


async def test_a_malformed_threshold_config_does_not_raise():
    rows = [{"currency": "USD", "direction": "credit", "amount_minor": 1,
             "balance_after_minor": 1, "occurred_at": "1", "created_at": "1"}]
    with (
        patch.object(ps, "db", _ledger_db(rows)),
        _cfg(**{"payout.float_low_balance_minor": "not json"}),
        patch("app.config.settings.admin_email", ""),
    ):
        state = await ps.check_float_balance()
    assert state["low"] == []
