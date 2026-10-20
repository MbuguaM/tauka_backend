"""M-Pesa (enrolment plan 2.4, §130).

Pinned:
1. Phones normalise to 2547…/2541…; others are refused. Unconfigured refuses.
2. subscribe: a send_invoice Stripe subscription, the phone saved encrypted
   (last four in clear), a pending payment for month 1, an STK push.
3. The callback is not trusted: a wrong token is refused; the payment is
   confirmed by STK query. Paid -> the invoice paid out of band and, for month 1,
   the plan activated (ids, tier, lessons). A cancelled prompt -> cancelled.
   A second callback changes nothing.
4. A renewal invoice for an M-Pesa learner prompts automatically; an overdue one
   opens the grace period.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from app.services import mpesa_service as mp
from app.services.mpesa_service import MpesaRefused
from tests.fakedb import FakeDB

KEY = Fernet.generate_key().decode()
CONF = {"mpesa_consumer_key": "ck", "mpesa_consumer_secret": "cs", "mpesa_shortcode": "174379",
        "mpesa_passkey": "pk", "mpesa_callback_token": "tok", "mpesa_kes_tutor": 9700, "encryption_key": KEY,
        "api_base_url": "https://api.tauka.io"}


@pytest.fixture
def env():
    db = FakeDB({"student": [{"id": "s1", "tier": "free_tier"}], "mpesa_payer": [], "mpesa_payment": []},
                unique={"mpesa_payer": [("student_id",)], "mpesa_payment": [("checkout_request_id",)]})
    fake_stripe = MagicMock()
    fake_stripe.Customer.create.return_value = SimpleNamespace(id="cus_1")
    fake_stripe.Subscription.create.return_value = SimpleNamespace(id="sub_1", latest_invoice="in_1")
    fake_stripe.Invoice.retrieve.return_value = SimpleNamespace(customer="cus_1", subscription="sub_1")
    push = AsyncMock(return_value={"CheckoutRequestID": "ws_1", "MerchantRequestID": "m1"})
    query = AsyncMock(return_value={"ResultCode": "0"})
    patches = [patch.object(mp.settings, k, v) for k, v in CONF.items()]
    for p in patches:
        p.start()
    with patch("app.services.mpesa_service.db", db), patch.dict("sys.modules", {"stripe": fake_stripe}), \
         patch("app.services.stripe_service._PRICE_MAP", {"tutor_tier": "price_tutor"}), \
         patch("app.services.mpesa_service.stk_push", push), patch("app.services.mpesa_service.stk_query", query), \
         patch("app.services.tier_service.change_tier", AsyncMock()) as tier, \
         patch("app.services.lesson_service.grant_paid_month") as grant:
        yield SimpleNamespace(db=db, stripe=fake_stripe, push=push, query=query, tier=tier, grant=grant)
    for p in patches:
        p.stop()


# 1
def test_phones():
    assert mp.normalise_phone("0712 345 678") == "254712345678"
    assert mp.normalise_phone("+254112345678") == "254112345678"
    for bad in ("12345", "0812345678", "+255712345678"):
        with pytest.raises(MpesaRefused):
            mp.normalise_phone(bad)


@pytest.mark.asyncio
async def test_unconfigured_refuses():
    with patch.object(mp.settings, "mpesa_consumer_key", ""):
        with pytest.raises(MpesaRefused) as e:
            await mp.subscribe("s1", "tutor_tier", "0712345678", None)
    assert e.value.reason == "not_configured"


# 2 + 3
@pytest.mark.asyncio
async def test_subscribe_then_confirm_activates(env):
    out = await mp.subscribe("s1", "tutor_tier", "0712345678", "a@x")
    kw = env.stripe.Subscription.create.call_args.kwargs
    assert kw["collection_method"] == "send_invoice" and kw["metadata"]["rail"] == "mpesa"
    payer = env.db.rows["mpesa_payer"][0]
    assert payer["phone_last4"] == "5678" and "0712" not in payer["phone_encrypted"]
    assert mp.payer_phone("s1") == "254712345678"
    pay = env.db.rows["mpesa_payment"][0]
    assert (pay["purpose"], pay["amount_kes"], pay["stripe_invoice_id"], pay["checkout_request_id"]) == \
        ("first_month", 9700, "in_1", "ws_1")
    assert out["status"] == "pending"
    with pytest.raises(MpesaRefused):
        await mp.callback("wrong", {})
    body = {"Body": {"stkCallback": {"CheckoutRequestID": "ws_1", "ResultCode": 0,
                                     "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "QK1"}]}}}}
    assert (await mp.callback("tok", body))["status"] == "paid"
    env.stripe.Invoice.pay.assert_called_once_with("in_1", paid_out_of_band=True)
    st = env.db.rows["student"][0]
    assert (st["stripe_customer_id"], st["stripe_subscription_id"], st["subscription_status"]) == \
        ("cus_1", "sub_1", "active")
    env.tier.assert_awaited_once_with("s1", "tutor_tier", "self")
    env.grant.assert_called_once_with("s1", "tutor_tier", "in_1")
    assert env.db.rows["mpesa_payment"][0]["mpesa_receipt"] == "QK1"
    await mp.callback("tok", body)                                   # again: nothing
    assert env.stripe.Invoice.pay.call_count == 1


@pytest.mark.asyncio
async def test_cancelled_prompt(env):
    await mp.subscribe("s1", "tutor_tier", "0712345678", None)
    env.query.return_value = {"ResultCode": "1032"}
    assert (await mp.confirm("ws_1"))["status"] == "failed"
    assert env.db.rows["mpesa_payment"][0]["status"] == "cancelled"
    env.stripe.Invoice.pay.assert_not_called()


# 4
@pytest.mark.asyncio
async def test_renewal_prompts_and_overdue_opens_grace(env):
    await mp.subscribe("s1", "tutor_tier", "0712345678", None)
    env.db.rows["student"][0].update(stripe_customer_id="cus_1", tier="tutor_tier")
    env.db.rows["mpesa_payment"][0]["status"] = "paid"
    env.push.return_value = {"CheckoutRequestID": "ws_2", "MerchantRequestID": "m2"}
    await mp.on_invoice_created({"id": "in_2", "customer": "cus_1", "collection_method": "send_invoice",
                                 "billing_reason": "subscription_cycle"})
    assert env.db.rows["mpesa_payment"][-1]["stripe_invoice_id"] == "in_2"
    await mp.on_invoice_created({"id": "in_x", "customer": "cus_1", "collection_method": "charge_automatically"})
    assert len(env.db.rows["mpesa_payment"]) == 2
    with patch("app.services.grace_service.start", AsyncMock()) as start:
        env.push.return_value = {"CheckoutRequestID": "ws_3"}
        env.db.rows["mpesa_payment"][-1]["status"] = "failed"
        await mp.on_invoice_overdue({"id": "in_2", "customer": "cus_1", "collection_method": "send_invoice"})
    start.assert_awaited_once_with("s1", "in_2")
    assert env.db.rows["mpesa_payment"][-1]["checkout_request_id"] == "ws_3"
