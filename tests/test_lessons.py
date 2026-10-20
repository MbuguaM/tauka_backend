"""Paid lesson unlocks — enrolment plan §2c/§2d, migration §91.

The sequencing and the lock live in `app.grant_lesson_slots()` (tested against
the database when §91 was applied). What is tested here is the Python side:
each paid month asks for the right number of lessons under a stable,
idempotent reference, and the webhook grants exactly once per month.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import lesson_service as ls
from app.services import stripe_service as ss


class _FakeRpcDB:
    def __init__(self):
        self.calls = []

    def schema(self, _name):
        return self

    def rpc(self, name, params):
        self.calls.append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data={"ok": True}))


def test_lessons_per_month_follow_the_tier_pace():
    assert ls.lessons_per_month("learner_tier") == 2
    assert ls.lessons_per_month("tutor_tier") == 4
    assert ls.lessons_per_month("intensive_tier") == 6
    assert ls.lessons_per_month("free_tier") == 0
    assert ls.lessons_per_month(None) == 0


def test_paid_month_grants_the_tier_count_keyed_on_the_invoice():
    fake = _FakeRpcDB()
    with patch.object(ls, "db", fake):
        ls.grant_paid_month("stu-1", "tutor_tier", "in_123")
    assert fake.calls == [("grant_lesson_slots", {
        "p_student_id": "stu-1", "p_count": 4,
        "p_source": "paid_month", "p_grant_ref": "in_123",
    })]


def test_free_tier_or_missing_ref_grants_nothing():
    fake = _FakeRpcDB()
    with patch.object(ls, "db", fake):
        assert ls.grant_paid_month("stu-1", "free_tier", "in_1") is None
        assert ls.grant_paid_month("stu-1", "tutor_tier", "") is None
    assert fake.calls == []


def test_gift_months_are_dated_forward_one_month_each():
    fake = _FakeRpcDB()
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with patch.object(ls, "db", fake):
        ls.grant_gift_months("stu-1", "gift-9", "tutor_tier", 3, start)
    refs = [p["p_grant_ref"] for _, p in fake.calls]
    starts = [p["p_available_from"] for _, p in fake.calls]
    assert refs == ["gift-9:0", "gift-9:1", "gift-9:2"]
    assert starts == [(start + timedelta(days=30 * k)).isoformat() for k in range(3)]
    assert all(p["p_count"] == 4 and p["p_source"] == "gift_month" for _, p in fake.calls)


@pytest.mark.asyncio
async def test_renewal_invoice_grants_that_months_lessons():
    student = MagicMock()
    student.schema.return_value.table.return_value.select.return_value \
        .eq.return_value.execute.return_value = SimpleNamespace(
            data=[{"id": "stu-1", "tier": "intensive_tier"}])
    with patch("app.services.supabase_client.supabase_admin", student), \
         patch("app.services.lesson_service.grant_paid_month") as grant, \
         patch("app.services.commitment_service.record_installment",
               AsyncMock(return_value={"recorded": False})):
        await ss._handle_invoice_paid({
            "id": "in_77", "subscription": "sub_1", "billing_reason": "subscription_cycle"})
    grant.assert_called_once_with("stu-1", "intensive_tier", "in_77")


@pytest.mark.asyncio
async def test_first_invoice_is_not_granted_twice():
    """Month 1 is granted at checkout completion; the subscription_create
    invoice must not grant again."""
    with patch("app.services.lesson_service.grant_paid_month") as grant, \
         patch("app.services.commitment_service.record_installment", AsyncMock()):
        await ss._handle_invoice_paid({
            "id": "in_1", "subscription": "sub_1", "billing_reason": "subscription_create"})
    grant.assert_not_called()


@pytest.mark.asyncio
async def test_renewal_grant_failure_reaches_stripe_for_a_retry():
    """A paid month with no lessons is a customer charged for nothing, so the
    error is not swallowed: Stripe retries, and the grant is idempotent."""
    student = MagicMock()
    student.schema.return_value.table.return_value.select.return_value \
        .eq.return_value.execute.return_value = SimpleNamespace(
            data=[{"id": "stu-1", "tier": "tutor_tier"}])
    with patch("app.services.supabase_client.supabase_admin", student), \
         patch("app.services.lesson_service.grant_paid_month",
               side_effect=RuntimeError("db down")), \
         patch("app.services.commitment_service.record_installment", AsyncMock()):
        with pytest.raises(RuntimeError):
            await ss._handle_invoice_paid({
                "id": "in_9", "subscription": "sub_1", "billing_reason": "subscription_cycle"})
