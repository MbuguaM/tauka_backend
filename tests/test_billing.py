"""Monthly plans through Paystack (§151).

Pinned:
1. Checkout: refuses a second live plan; the plan's price and our reference go to Paystack.
2. A paid first month: lessons, tier, period, and the customer noted. A repeat does nothing.
3. A required step failing puts the payment back to pending, so a retry can finish it.
4. Renewals match on the customer, grant under their own reference, once; a wrong amount grants nothing.
5. A failed charge opens grace; paying the month closes it.
6. Cancel: stops Paystack now, access to period end, then ended and free. Reactivate undoes it.
7. daily(): M-Pesa reminded once; an unpaid period opens grace; card gets leeway.
8. M-Pesa stays behind its flag and is priced in shillings.
9. Tokens never reach the learner's view.
10. Webhook routing, and a refund event on a monthly payment alerts ops rather than crashing.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import billing_service as bs
from app.services import payment_service as ps
from app.services.billing_service import BillingRefused
from app.services.refund_service import provider_update
from tests.fakedb import FakeDB

NOW = datetime(2027, 1, 10, 12, tzinfo=timezone.utc)
S = "11111111-1111-4111-8111-111111111111"


def _sub(**over):
    return {"student_id": S, "provider": "paystack", "channel": "card", "tier": "tutor_tier", "status": "active",
            "email": "l@x.test", "customer_code": "CUS_1", "subscription_code": "SUB_1", "email_token": "tok",
            "plan_code": "PLN_t", "current_period_end": (NOW + timedelta(days=20)).isoformat(), **over}


@pytest.fixture
def db():
    fake = FakeDB({"billing_subscription": [], "billing_payment": [], "student": [{"id": S, "tier": "free_tier"}],
                   "student_payment": [], "feature_flag": [{"key": "mpesa_payments", "status": "coming"}]},
                  unique={"billing_payment": [("reference",)]})
    tiers, grant, mail, start, resolve = AsyncMock(), patch("app.services.lesson_service.grant_paid_month"), \
        AsyncMock(return_value={"status": "sent"}), AsyncMock(return_value=True), patch("app.services.grace_service.resolve")
    with patch.object(bs, "db", fake), patch.object(ps, "db", fake), patch("app.core.flags.db", fake), \
         patch("app.services.tier_service.change_tier", tiers), grant as granted, \
         patch("app.services.email_service.send_template_email", mail), \
         patch("app.services.grace_service.start", start), resolve as resolved, \
         patch("app.services.pause_service.resume_after_payment"), \
         patch("app.services.pause_service.is_pausing", return_value=False), \
         patch("app.services.makeup_service.forfeit_on_cancellation") as forfeit, \
         patch("app.services.grace_service.leave_pod_on_cancel") as leave, \
         patch.object(bs, "_email_of", return_value="L@x.test"), \
         patch.object(ps.settings, "payment_provider", "paystack"), patch.object(ps.settings, "paystack_secret_key", "sk"), \
         patch.object(bs.settings, "paystack_plan_tutor", "PLN_t"), patch.object(bs.settings, "mpesa_kes_tutor", 9700):
        fake.tiers, fake.grant, fake.mail, fake.grace_start, fake.grace_resolve = tiers, granted, mail, start, resolved
        fake.forfeit, fake.leave = forfeit, leave
        yield fake


def _paystack(status=200, payload=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(status, json=payload or {"status": True, "data": {"authorization_url": "https://pay/x"}})
    real = httpx.AsyncClient
    return patch("app.services.payment_service.httpx.AsyncClient",
                 lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def _first_paid(db):
    with _paystack():
        await bs.checkout(S, "tutor_tier")
    return db.rows["billing_payment"][0]


# 1
@pytest.mark.asyncio
async def test_checkout_sends_plan_and_reference(db):
    seen = []
    with _paystack(seen=seen):
        out = await bs.checkout(S, "tutor_tier")
    method, path, body = seen[0]
    assert out["checkout_url"] == "https://pay/x" and path == "/transaction/initialize"
    assert body["plan"] == "PLN_t" and body["reference"].startswith("mo-") and body["amount"] == "7500"
    assert body["email"] == "L@x.test" and db.rows["billing_subscription"][0]["status"] == "pending"
    with pytest.raises(BillingRefused) as e:
        db.rows["billing_subscription"][0]["status"] = "active"
        await bs.checkout(S, "tutor_tier")
    assert e.value.reason == "already_subscribed"


@pytest.mark.asyncio
async def test_checkout_needs_a_plan_code_and_can_restart_after_ending(db):
    with patch.object(bs.settings, "paystack_plan_tutor", ""), pytest.raises(BillingRefused) as e:
        await bs.checkout(S, "tutor_tier")
    assert e.value.reason == "not_configured"
    db.rows["billing_subscription"].append(_sub(status="ended", subscription_code=None))
    with _paystack():
        await bs.checkout(S, "tutor_tier")
    assert len(db.rows["billing_subscription"]) == 1 and db.rows["billing_subscription"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_a_paystack_refusal_marks_the_payment_failed(db):
    with _paystack(400, {"status": False, "message": "No"}), pytest.raises(BillingRefused) as e:
        await bs.checkout(S, "tutor_tier")
    assert e.value.reason == "payment_failed" and db.rows["billing_payment"][0]["status"] == "failed"


# 2
@pytest.mark.asyncio
async def test_first_month(db):
    payment = await _first_paid(db)
    charge = {"event": "charge.success", "data": {"status": "success", "reference": payment["reference"], "amount": 7500,
                                                  "currency": "USD", "customer": {"customer_code": "CUS_1"}}}
    assert await ps.handle_paystack_event(charge) is True
    sub, student = db.rows["billing_subscription"][0], db.rows["student"][0]
    assert sub["status"] == "active" and sub["customer_code"] == "CUS_1" and student["subscription_status"] == "active"
    assert student["current_period_end"] == sub["current_period_end"]
    db.grant.assert_called_once_with(S, "tutor_tier", payment["reference"])
    db.tiers.assert_awaited_once_with(S, "tutor_tier", "self")
    assert db.rows["student_payment"][0]["source"] == "paystack" and db.rows["student_payment"][0]["amount_minor"] == 7500
    await ps.handle_paystack_event(charge)
    db.grant.assert_called_once()
    assert len(db.rows["student_payment"]) == 1


@pytest.mark.asyncio
async def test_first_month_wrong_amount_grants_nothing(db):
    payment = await _first_paid(db)
    await ps.settle(payment["reference"], 100, "USD")
    db.grant.assert_not_called()
    assert db.rows["billing_payment"][0]["status"] == "pending"


# 3
@pytest.mark.asyncio
async def test_a_failed_grant_can_be_retried(db):
    payment = await _first_paid(db)
    db.grant.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await bs.settle_month(payment, NOW)
    assert db.rows["billing_payment"][0]["status"] == "pending"
    db.grant.side_effect = None
    assert await bs.settle_month(payment, NOW)
    assert db.rows["billing_payment"][0]["status"] == "paid"


# 4
@pytest.mark.asyncio
async def test_renewal(db):
    db.rows["billing_subscription"].append(_sub())
    end = datetime.fromisoformat(db.rows["billing_subscription"][0]["current_period_end"])
    data = {"status": "success", "reference": "T999", "amount": 7500, "currency": "USD",
            "customer": {"customer_code": "CUS_1"}, "plan": {"plan_code": "PLN_t"}}
    assert await ps.handle_paystack_event({"event": "charge.success", "data": data}) is True
    db.grant.assert_called_once_with(S, "tutor_tier", "T999")
    assert datetime.fromisoformat(db.rows["billing_subscription"][0]["current_period_end"]).month == end.month + 1
    await ps.handle_paystack_event({"event": "charge.success", "data": data})
    db.grant.assert_called_once()


@pytest.mark.asyncio
async def test_renewal_that_does_not_match_grants_nothing(db):
    db.rows["billing_subscription"].append(_sub())
    data = {"status": "success", "reference": "T1", "amount": 100, "currency": "USD",
            "customer": {"customer_code": "CUS_1"}, "plan": {"plan_code": "PLN_t"}}
    assert await bs.renewal(data) is False
    assert await bs.renewal({**data, "amount": 7500, "customer": {"customer_code": "CUS_x"}}) is False
    assert await bs.renewal({"reference": "T2", "customer": {"customer_code": "CUS_1"}}) is False
    db.grant.assert_not_called()


# 5
@pytest.mark.asyncio
async def test_failed_charge_opens_grace_and_paying_closes_it(db):
    db.rows["billing_subscription"].append(_sub())
    event = {"event": "invoice.payment_failed", "data": {"subscription": {"subscription_code": "SUB_1"}, "invoice_code": "INV_1"}}
    assert await ps.handle_paystack_event(event) is True
    assert db.rows["billing_subscription"][0]["status"] == "past_due" and db.rows["student"][0]["subscription_status"] == "past_due"
    db.grace_start.assert_awaited_once()
    assert db.grace_start.await_args.args[:2] == (S, "INV_1")
    seen = []
    with _paystack(seen=seen):
        out = await bs.pay_month(S)
    assert out["checkout_url"] == "https://pay/x" and seen[0][2]["channels"] == ["card"] and "plan" not in seen[0][2]
    await ps.settle(db.rows["billing_payment"][0]["reference"], 7500, "USD")
    assert db.rows["billing_subscription"][0]["status"] == "active"
    db.grace_resolve.assert_called_once()


# 6
@pytest.mark.asyncio
async def test_cancel_runs_to_period_end_then_ends(db):
    db.rows["billing_subscription"].append(_sub())
    seen = []
    with _paystack(payload={"status": True, "data": {}}, seen=seen):
        await bs.cancel(S, NOW)
    assert seen[0][1] == "/subscription/disable" and seen[0][2] == {"code": "SUB_1", "token": "tok"}
    assert db.rows["billing_subscription"][0]["status"] == "cancelling"
    db.tiers.assert_not_awaited()
    assert (await bs.daily(NOW))["ended"] == 0
    assert (await bs.daily(NOW + timedelta(days=21)))["ended"] == 1
    row = db.rows["billing_subscription"][0]
    assert row["status"] == "ended" and row["email_token"] is None
    db.tiers.assert_awaited_once_with(S, "free_tier", "self")
    db.forfeit.assert_called_once_with(S)
    db.leave.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_before_paystack_reports_the_subscription_is_refused(db):
    db.rows["billing_subscription"].append(_sub(subscription_code=None))
    with pytest.raises(BillingRefused) as e:
        await bs.cancel(S, NOW)
    assert e.value.reason == "not_ready" and db.rows["billing_subscription"][0]["status"] == "active"


@pytest.mark.asyncio
async def test_reactivate(db):
    db.rows["billing_subscription"].append(_sub(status="cancelling"))
    seen = []
    with _paystack(payload={"status": True, "data": {}}, seen=seen):
        await bs.reactivate(S, NOW)
    assert seen[0][1] == "/subscription/enable" and db.rows["billing_subscription"][0]["status"] == "active"


@pytest.mark.asyncio
async def test_paystack_cancelling_it_is_noticed(db):
    db.rows["billing_subscription"].append(_sub())
    assert await ps.handle_paystack_event({"event": "subscription.not_renew", "data": {"subscription_code": "SUB_1"}})
    assert db.rows["billing_subscription"][0]["status"] == "cancelling"


@pytest.mark.asyncio
async def test_subscription_created_stores_the_token(db):
    db.rows["billing_subscription"].append(_sub(status="pending", customer_code=None, subscription_code=None, email_token=None))
    event = {"event": "subscription.create", "data": {"subscription_code": "SUB_9", "email_token": "t9",
                                                      "customer": {"customer_code": "CUS_9", "email": "L@X.test"}}}
    assert await ps.handle_paystack_event(event) is True
    row = db.rows["billing_subscription"][0]
    assert (row["subscription_code"], row["email_token"], row["customer_code"]) == ("SUB_9", "t9", "CUS_9")


# 7
@pytest.mark.asyncio
async def test_daily_reminds_mpesa_once_and_opens_grace_when_unpaid(db):
    end = NOW + timedelta(days=2)
    db.rows["billing_subscription"].append(_sub(channel="mpesa", subscription_code=None, current_period_end=end.isoformat()))
    assert (await bs.daily(NOW))["reminded"] == 1
    assert (await bs.daily(NOW))["reminded"] == 0
    assert db.mail.await_args.args[1] == "billing_month_due"
    assert (await bs.daily(end + timedelta(minutes=1)))["missed"] == 1
    assert db.rows["billing_subscription"][0]["status"] == "past_due"
    db.grace_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_card_renewal_gets_leeway(db):
    end = NOW - timedelta(days=1)
    db.rows["billing_subscription"].append(_sub(current_period_end=end.isoformat()))
    assert (await bs.daily(NOW))["missed"] == 0
    assert (await bs.daily(end + timedelta(days=3)))["missed"] == 1


# 8
@pytest.mark.asyncio
async def test_mpesa_flag_and_price(db):
    with pytest.raises(BillingRefused) as e:
        await bs.checkout(S, "tutor_tier", "mpesa")
    assert e.value.reason == "not_enabled"
    db.rows["feature_flag"][0]["status"] = "ready"
    seen = []
    with _paystack(seen=seen):
        await bs.checkout(S, "tutor_tier", "mpesa")
    body = seen[0][2]
    assert body["currency"] == "KES" and body["amount"] == "970000" and body["channels"] == ["mobile_money"] and "plan" not in body


# 9
def test_summary_hides_tokens(db):
    db.rows["billing_subscription"].append(_sub())
    view = bs.summary(S)
    assert "tok" not in json.dumps(view) and "SUB_1" not in json.dumps(view)
    assert view["plan_name"] == "Tutor" and view["amount_minor"] == 7500 and view["can_pay_now"] is False


def test_history_lists_paid_months_only(db):
    db.rows["billing_payment"] += [
        {"id": "a", "student_id": S, "tier": "tutor_tier", "channel": "card", "amount_minor": 7500, "currency": "USD",
         "status": "paid", "paid_at": NOW.isoformat()},
        {"id": "b", "student_id": S, "tier": "tutor_tier", "channel": "card", "amount_minor": 7500, "currency": "USD",
         "status": "pending"}]
    rows = bs.history(S)
    assert [r["id"] for r in rows] == ["a"] and rows[0]["description"] == "Tutor · one month"


# 10
@pytest.mark.asyncio
async def test_refund_event_on_a_monthly_payment_alerts_ops(db):
    db.rows["billing_payment"].append({"id": "p1", "student_id": S, "status": "paid", "amount_minor": 7500, "currency": "USD"})
    with patch("app.services.refund_service.db", db):
        assert await provider_update("mo-p1", "refund.processed", NOW) is False
    assert db.mail.await_args.args[2]["outside"] is True


@pytest.mark.asyncio
async def test_demo_renew_and_fail(db):
    db.rows["billing_subscription"].append(_sub(provider="demo"))
    assert (await bs.demo_event(S, "renew"))["status"] == "renewed"
    db.grant.assert_called_once()
    assert (await bs.demo_event(S, "fail"))["status"] == "past_due"
    with pytest.raises(BillingRefused):
        await bs.demo_event(S, "nope")
