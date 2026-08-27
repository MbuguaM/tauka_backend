"""Plan Phase 4 — termly commitments.

A commitment is a **contract**, and the things worth testing are the ones that
would let the contract and the entitlement drift apart, or let a promise be
broken quietly:

1. **A commitment is not a tier.** §4.4: putting "what you can use" and "what you
   promised" in one column breaks every gate that switches on it. The commitment
   names a tier; `student.tier` remains the entitlement axis.
2. **The Stripe metadata must not carry `tier`.** `_handle_checkout_complete`
   dispatches on metadata keys, and a `tier` key would route a term purchase into
   `_activate_subscription`.
3. **Activation is idempotent.** Stripe replays webhooks; a double grant would
   overwrite `original_tier` with the tier the student is already on.
4. **The milestone means the phase, not its last page.** Units are self-paced, so
   a student can reach unit 15 with 12 behind them.
5. **The guarantee is enforced in the matcher**, and it is the one blocking rule
   there that is not supply-dependent — a thin platform is no reason to break a
   contract.
6. **D-18: nothing is purchasable.** Checkout refuses by name rather than letting
   Stripe reject an empty price id.
"""
import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import commitment_service as cs
from app.services import matching_service as ms


def _cfg(**overrides):
    values = dict(cs._DEFAULTS)
    values.update(overrides)
    return patch.object(cs, "_config", return_value=values)


# ---------------------------------------------------------------------------
# Term windows
# ---------------------------------------------------------------------------

def test_a_term_is_configurable_not_a_constant():
    with _cfg(**{"commitment.term_months": "3"}):
        start, end = cs.term_window(date(2026, 9, 1))
    assert (start, end) == (date(2026, 9, 1), date(2026, 12, 1))

    with _cfg(**{"commitment.term_months": "6"}):
        start, end = cs.term_window(date(2026, 9, 1))
    assert end == date(2027, 3, 1)


def test_a_term_starting_on_the_31st_does_not_overflow_the_month():
    """31 August + 3 months is 30 November, not 1 December."""
    with _cfg(**{"commitment.term_months": "3"}):
        _, end = cs.term_window(date(2026, 8, 31))
    assert end == date(2026, 11, 30)


def test_a_term_crossing_a_year_boundary():
    with _cfg(**{"commitment.term_months": "3"}):
        _, end = cs.term_window(date(2026, 11, 15))
    assert end == date(2027, 2, 15)


def test_a_nonsense_term_length_falls_back():
    with _cfg(**{"commitment.term_months": "soon"}):
        _, end = cs.term_window(date(2026, 9, 1))
    assert end == date(2026, 12, 1)


# ---------------------------------------------------------------------------
# Milestone targets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("unit_order:15", 15),
    ("unit_order:1", 1),
    (None, None),
    ("", None),
    # A level name would reference `unit.level`, which does not exist — the
    # matching plan's §5.4 proposed it and it was never built. Accepting it would
    # give a hook that silently never fires.
    ("level:1", None),
    ("phase_1", None),
    ("unit_order:0", None),
    ("unit_order:-3", None),
    ("unit_order:many", None),
])
def test_only_a_unit_order_threshold_is_a_valid_milestone(raw, expected):
    assert cs.parse_milestone(raw) == expected


def _milestone_db(units, progress):
    fake = MagicMock()

    def table(name):
        t = MagicMock()
        if name == "unit":
            t.select.return_value.eq.return_value.is_.return_value.lte.return_value \
                .execute.return_value = MagicMock(data=units)
        elif name == "unit_progress":
            t.select.return_value.eq.return_value.in_.return_value.execute.return_value = \
                MagicMock(data=progress)
        return t

    fake.schema.return_value.table.side_effect = table
    return fake


COMMITMENT = {"id": "c1", "student_id": "s1", "course_id": "course-1",
              "curriculum_milestone_target": "unit_order:3"}


async def test_the_milestone_means_every_unit_up_to_the_target():
    """Units are self-paced, so a student can reach unit 3 with 2 behind them.
    "Complete Phase 1" means the phase, not its last page."""
    units = [{"id": "u1", "unit_order": 1}, {"id": "u2", "unit_order": 2},
             {"id": "u3", "unit_order": 3}]
    partial = [{"unit_id": "u1", "status": "completed"},
               {"unit_id": "u3", "status": "completed"}]
    with patch.object(cs, "db", _milestone_db(units, partial)):
        assert await cs.check_milestone(COMMITMENT) is False

    everything = [{"unit_id": u["id"], "status": "completed"} for u in units]
    with patch.object(cs, "db", _milestone_db(units, everything)):
        assert await cs.check_milestone(COMMITMENT) is True


async def test_an_in_progress_unit_does_not_count_as_complete():
    units = [{"id": "u1", "unit_order": 1}]
    with patch.object(cs, "db", _milestone_db(
            units, [{"unit_id": "u1", "status": "in_progress"}])):
        assert await cs.check_milestone(
            {**COMMITMENT, "curriculum_milestone_target": "unit_order:1"}) is False


async def test_an_unreachable_target_is_never_satisfied():
    """The course has 12 units against a 60-unit curriculum, so a target of 15
    is genuinely unreachable today. Reported, never treated as satisfied."""
    with patch.object(cs, "db", _milestone_db([], [])):
        assert await cs.check_milestone(
            {**COMMITMENT, "curriculum_milestone_target": "unit_order:15"}) is False


async def test_a_commitment_with_no_target_has_no_milestone():
    with patch.object(cs, "db", _milestone_db([{"id": "u1", "unit_order": 1}], [])):
        assert await cs.check_milestone(
            {**COMMITMENT, "curriculum_milestone_target": None}) is False


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def _commitment_db(row, capture=None):
    fake = MagicMock()
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .limit.return_value.execute.return_value = MagicMock(data=[row] if row else [])
    if capture is not None:
        fake.schema.return_value.table.return_value.update.side_effect = \
            lambda payload: (capture.append(payload),
                             MagicMock(eq=MagicMock(return_value=MagicMock(
                                 execute=MagicMock()))))[1]
    return fake


PREPAY = {"id": "c1", "student_id": "s1", "status": "pending", "tier": "tutor_tier",
          "billing_mode": "prepay", "installments_total": 1, "installments_paid": 0}


async def test_activation_grants_the_tier_the_commitment_names():
    """The tier lives in student.tier with source='commitment'; the contract
    lives in enrollment_commitment. Neither is the other."""
    updates = []
    change = AsyncMock(return_value={})
    with (
        patch.object(cs, "db", _commitment_db(PREPAY, updates)),
        patch("app.services.tier_service.change_tier", change),
    ):
        result = await cs.activate("c1", stripe_reference="cs_test_1")

    assert result["activated"] is True
    change.assert_awaited_once_with("s1", "tutor_tier", "commitment")
    assert updates[0]["status"] == "active"
    # A prepay commitment is paid in full the moment it activates.
    assert updates[0]["installments_paid"] == 1
    # The tier is NOT written on this row — it names a tier, it does not hold one.
    assert "tier" not in updates[0]


async def test_activating_twice_does_not_double_grant():
    """Stripe replays webhooks. A second change_tier would overwrite
    original_tier with the tier the student is already on."""
    change = AsyncMock()
    with (
        patch.object(cs, "db", _commitment_db({**PREPAY, "status": "active"})),
        patch("app.services.tier_service.change_tier", change),
    ):
        result = await cs.activate("c1")
    assert result["activated"] is False
    assert result["reason"] == "already_active"
    change.assert_not_awaited()


async def test_a_cancelled_commitment_cannot_be_activated():
    change = AsyncMock()
    with (
        patch.object(cs, "db", _commitment_db({**PREPAY, "status": "cancelled"})),
        patch("app.services.tier_service.change_tier", change),
    ):
        result = await cs.activate("c1")
    assert result["activated"] is False
    change.assert_not_awaited()


async def test_an_installment_activation_counts_only_the_first_payment():
    updates = []
    with (
        patch.object(cs, "db", _commitment_db(
            {**PREPAY, "billing_mode": "installment", "installments_total": 3}, updates)),
        patch("app.services.tier_service.change_tier", AsyncMock()),
    ):
        await cs.activate("c1")
    assert updates[0]["installments_paid"] == 1


# ---------------------------------------------------------------------------
# Instalments
# ---------------------------------------------------------------------------

def _installment_db(row, capture):
    fake = MagicMock()
    fake.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .limit.return_value.execute.return_value = MagicMock(data=[row] if row else [])
    fake.schema.return_value.table.return_value.update.side_effect = \
        lambda payload: (capture.append(payload),
                         MagicMock(eq=MagicMock(return_value=MagicMock(
                             execute=MagicMock()))))[1]
    return fake


async def test_an_instalment_counts_up():
    updates = []
    with patch.object(cs, "db", _installment_db(
            {"id": "c1", "installments_paid": 1, "installments_total": 3,
             "status": "active"}, updates)):
        result = await cs.record_installment("sub_1")
    assert result["recorded"] is True
    assert updates[0]["installments_paid"] == 2


async def test_a_duplicated_webhook_cannot_overcount():
    """Clamping here means a duplicate is a no-op rather than a 500 in the
    Stripe delivery log — which is what makes Stripe retry it."""
    updates = []
    with patch.object(cs, "db", _installment_db(
            {"id": "c1", "installments_paid": 3, "installments_total": 3,
             "status": "active"}, updates)):
        result = await cs.record_installment("sub_1")
    assert result["recorded"] is False
    assert result["reason"] == "already_complete"
    assert updates == []


async def test_an_unrelated_subscription_invoice_is_ignored_not_an_error():
    """An ordinary monthly subscription's invoice finds no commitment. It must
    not raise, or the webhook fails and Stripe retries it forever."""
    with patch.object(cs, "db", _installment_db(None, [])):
        result = await cs.record_installment("sub_monthly")
    assert result["recorded"] is False
    assert result["reason"] == "no_such_commitment"


# ---------------------------------------------------------------------------
# D-18 — nothing is purchasable
# ---------------------------------------------------------------------------

def test_checkout_refuses_by_name_when_no_price_is_configured():
    """Beats letting Stripe reject an empty price id with an error nobody can
    act on."""
    with (
        patch("app.config.settings.stripe_price_term_prepay", ""),
        patch("app.config.settings.stripe_price_term_installment", ""),
    ):
        for mode in ("prepay", "installment"):
            with pytest.raises(cs.CommitmentUnavailable) as caught:
                cs._price_id(mode)
            assert f"STRIPE_PRICE_TERM_{mode.upper()}" in str(caught.value)


def test_a_configured_price_is_returned():
    with patch("app.config.settings.stripe_price_term_prepay", "price_abc"):
        assert cs._price_id("prepay") == "price_abc"


# ---------------------------------------------------------------------------
# The guarantee, enforced in the matcher
# ---------------------------------------------------------------------------

def _match_env(locked):
    return (
        patch.object(ms, "locked_by_commitment", return_value=locked),
        patch.object(ms, "active_tutor_count", return_value=1),
        patch.object(ms, "concentration",
                     return_value={"tutor_students": 0, "total_students": 0, "pct": 0.0}),
        patch.object(ms, "_already_linked", return_value=False),
        patch.object(ms, "_has_any_active_link", return_value=False),
        patch.object(ms, "_config", return_value=dict(ms._DEFAULTS)),
    )


def test_a_commitment_blocks_a_home_reassignment_even_on_a_thin_platform():
    """The one blocking rule in evaluate_assignment that is NOT supply-dependent.
    A thin platform is no reason to break a contract someone paid for."""
    a, b, c, d, e, f = _match_env("tutor-locked")
    with a, b, c, d, e, f:
        verdict = ms.evaluate_assignment("s1", "tutor-other", "home")
    assert verdict["allowed"] is False
    assert any("termly commitment" in x for x in verdict["blocking"])
    # And the supply advisory is still reported alongside it.
    assert verdict["supply"]["binding"] is False


def test_a_commitment_does_not_block_adding_guests():
    """The promise is about who their HOME tutor is. Adding guests around them
    is exactly what §1.2 wants."""
    a, b, c, d, e, f = _match_env("tutor-locked")
    with a, b, c, d, e, f:
        verdict = ms.evaluate_assignment("s1", "tutor-other", "guest")
    assert verdict["allowed"] is True


def test_reassigning_to_the_locked_tutor_is_fine():
    a, b, c, d, e, f = _match_env("tutor-locked")
    with a, b, c, d, e, f:
        verdict = ms.evaluate_assignment("s1", "tutor-locked", "home")
    assert verdict["allowed"] is True


def test_no_commitment_means_no_constraint():
    a, b, c, d, e, f = _match_env(None)
    with a, b, c, d, e, f:
        verdict = ms.evaluate_assignment("s1", "anyone", "home")
    assert verdict["allowed"] is True


def test_a_failed_commitment_lookup_does_not_freeze_matching():
    """None, not a raise: one bad query must not block every assignment."""
    fake = MagicMock()
    fake.schema.side_effect = Exception("connection reset")
    with patch.object(cs, "db", fake):
        assert cs.locked_tutor_for("s1") is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def test_cancelling_does_not_revert_the_tier():
    """The refund and the entitlement are separate decisions. A cancel that
    silently downgrades someone mid-conversation is a support incident."""
    change = AsyncMock()
    with (
        patch.object(cs, "db", _commitment_db({"id": "c1", "status": "active",
                                               "student_id": "s1"}, [])),
        patch("app.services.tier_service.change_tier", change),
    ):
        result = await cs.cancel("c1", "admin-1", "student asked")
    assert result["cancelled"] is True
    assert result["tier_unchanged"] is True
    change.assert_not_awaited()


async def test_an_already_cancelled_commitment_cannot_be_cancelled_twice():
    with patch.object(cs, "db", _commitment_db({"id": "c1", "status": "cancelled",
                                                "student_id": "s1"}, [])):
        result = await cs.cancel("c1", "admin-1")
    assert result["cancelled"] is False


def test_days_remaining_never_goes_negative():
    past = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    assert cs._days_remaining(past) == 0
    future = (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()
    assert cs._days_remaining(future) == 10
    assert cs._days_remaining(None) is None
    assert cs._days_remaining("not a date") is None
