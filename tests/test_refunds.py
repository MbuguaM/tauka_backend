"""Refunds on one-off purchases (§146).

Pinned:
1. Finance or ops only, with a reason.
2. An unused card: refunded, the code dies at once, the buyer is told.
3. In use: refused without confirming the access ends; refused after 7 days; within
   7 days the lessons are revoked and the learner drops to free unless paying.
4. A sponsored waitlist seat refunded: the request closes and its story goes.
5. The provider refusing puts everything back; a failed refund can be retried.
6. Refund events update the order; a refund made outside Tauka alerts ops and
   changes nothing; unknown events are ignored.
7. A refunded card can't be redeemed, and a late charge webhook can't revive it.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import commerce_service as cs
from app.services import hierarchy_service
from app.services import payment_service as ps
from app.services import refund_service as rs
from app.services.refund_service import RefundRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 3, 10, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=2)).isoformat()
OLD = (NOW - timedelta(days=8)).isoformat()


def _card(**over):
    return {"id": "g1", "status": "paid", "code": "ABCDEFGHJKLM", "tier": "tutor_tier", "months": 2, "amount_minor": 15000,
            "currency": "USD", "purchaser_email": "buyer@x.test", "payment_provider": "paystack",
            "payment_reference": "gc-g1", "created_at": NOW.isoformat(), **over}


def _seat(**over):
    return {"id": "s1", "mode": "waitlist", "request_id": "r1", "status": "claimed", "tier": "tutor_tier", "months": 3,
            "amount_minor": 22500, "currency": "USD", "sponsor_email": "giver@x.test", "payment_provider": "paystack",
            "payment_reference": "sp-s1", "recipient_id": "learner", "claimed_at": RECENT,
            "created_at": NOW.isoformat(), **over}


@pytest.fixture
def db():
    fake = FakeDB({
        "gift_card": [], "sponsorship": [], "feature_flag": [{"key": "gift_cards", "status": "ready"}],
        "sponsorship_request": [{"id": "r1", "status": "sponsored", "story": "A long enough story " * 3, "story_erased_at": None}],
        "student": [{"id": "learner", "subscription_status": None}, {"id": "payer", "subscription_status": "active"}],
    })
    tiers, mail, provider, revoke = AsyncMock(), AsyncMock(return_value={"status": "sent"}), AsyncMock(), patch(
        "app.services.lesson_service.revoke_gift_months")
    with patch.object(ps, "db", fake), patch.object(rs, "db", fake), patch("app.services.commerce_service.db", fake), \
         patch("app.services.sponsorship_service.db", fake), patch("app.core.flags.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: (a, c) in {("ops", "ops"), ("fin", "finance")}), \
         patch("app.services.tier_service.change_tier", tiers), \
         patch("app.services.email_service.send_template_email", mail), \
         patch("app.services.sponsorship_service._email_of", side_effect=lambda u: f"{u}@mail.test"), \
         patch.object(ps, "provider_refund", provider), revoke as revoked:
        fake.tiers, fake.mail, fake.provider, fake.revoked = tiers, mail, provider, revoked
        yield fake


def _templates(db):
    return [c.args[1] for c in db.mail.await_args_list]


# 1
@pytest.mark.asyncio
async def test_only_finance_or_ops_with_a_reason(db):
    db.rows["gift_card"].append(_card())
    with pytest.raises(RefundRefused) as e:
        await rs.refund("someone", "gc-g1", "Bought by mistake", now=NOW)
    assert e.value.reason == "not_allowed"
    with pytest.raises(RefundRefused) as e:
        await rs.refund("ops", "gc-g1", " ", now=NOW)
    assert e.value.reason == "reason_required"
    with pytest.raises(RefundRefused):
        rs.orders("someone")
    assert (await rs.refund("fin", "gc-g1", "Bought by mistake", now=NOW))["status"] == "refunded"


# 2
@pytest.mark.asyncio
async def test_unused_card(db):
    db.rows["gift_card"].append(_card())
    out = await rs.refund("ops", "gc-g1", "Bought by mistake", now=NOW)
    row = db.rows["gift_card"][0]
    assert out["refund_state"] == "requested" and not out["access_taken_back"]
    assert row["status"] == "refunded" and row["refund_reason"] == "Bought by mistake" and row["refunded_by"] == "ops"
    db.provider.assert_awaited_once_with("paystack", "gc-g1", "Bought by mistake")
    db.tiers.assert_not_awaited()
    assert _templates(db) == ["payment_refunded"]
    assert db.mail.await_args.args[0] == "buyer@x.test"


# 3
@pytest.mark.asyncio
async def test_in_use_needs_confirming_and_a_recent_use(db):
    db.rows["gift_card"].append(_card(status="redeemed", redeemed_by="learner", redeemed_at=RECENT))
    with pytest.raises(RefundRefused) as e:
        await rs.refund("ops", "gc-g1", "Asked by the buyer", now=NOW)
    assert e.value.reason == "in_use" and db.rows["gift_card"][0]["status"] == "redeemed"
    db.rows["gift_card"][0]["redeemed_at"] = OLD
    with pytest.raises(RefundRefused) as e:
        await rs.refund("ops", "gc-g1", "Asked by the buyer", revoke_access=True, now=NOW)
    assert e.value.reason == "window_passed"
    db.provider.assert_not_awaited()
    db.rows["gift_card"][0]["redeemed_at"] = RECENT
    out = await rs.refund("ops", "gc-g1", "Asked by the buyer", revoke_access=True, now=NOW)
    assert out["access_taken_back"]
    db.revoked.assert_called_once_with("learner", "gc:g1")
    db.tiers.assert_awaited_once_with("learner", "free_tier", "self")
    assert set(_templates(db)) == {"seat_refunded", "payment_refunded"}


@pytest.mark.asyncio
async def test_a_paying_learner_keeps_their_plan(db):
    db.rows["gift_card"].append(_card(status="redeemed", redeemed_by="payer", redeemed_at=RECENT))
    await rs.refund("ops", "gc-g1", "Asked by the buyer", revoke_access=True, now=NOW)
    db.revoked.assert_called_once_with("payer", "gc:g1")
    db.tiers.assert_not_awaited()


# 4
@pytest.mark.asyncio
async def test_sponsored_seat_closes_the_request(db):
    db.rows["sponsorship"].append(_seat())
    await rs.refund("ops", "sp-s1", "Sponsor's card was stolen", revoke_access=True, now=NOW)
    req = db.rows["sponsorship_request"][0]
    assert req["status"] == "withdrawn" and req["story"] is None and req["story_erased_at"]
    db.revoked.assert_called_once_with("learner", "sp:s1")


# 5
@pytest.mark.asyncio
async def test_provider_refusal_puts_it_back(db):
    db.rows["gift_card"].append(_card())
    db.provider.side_effect = ps.PaymentFailed("No")
    with pytest.raises(RefundRefused) as e:
        await rs.refund("ops", "gc-g1", "Bought by mistake", now=NOW)
    row = db.rows["gift_card"][0]
    assert e.value.reason == "payment_failed" and row["status"] == "paid" and row["refund_state"] is None
    db.mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_refund_can_be_retried(db):
    db.rows["gift_card"].append(_card(status="refunded", refund_state="failed", refund_reason="First go"))
    assert (await rs.refund("ops", "gc-g1", "Trying again", now=NOW))["refund_state"] == "requested"
    db.mail.assert_not_awaited()                             # the buyer was told the first time
    db.rows["gift_card"][0]["refund_state"] = "processed"
    with pytest.raises(RefundRefused):
        await rs.refund("ops", "gc-g1", "Twice", now=NOW)


@pytest.mark.asyncio
async def test_never_paid_through_a_provider_is_not_refundable(db):
    db.rows["gift_card"].append(_card(payment_provider=None))
    with pytest.raises(RefundRefused) as e:
        await rs.refund("ops", "gc-g1", "Bought by mistake", now=NOW)
    assert e.value.reason == "not_refundable"
    db.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_demo_refund_finishes_at_once(db):
    db.rows["gift_card"].append(_card(payment_provider="demo"))
    assert (await rs.refund("ops", "gc-g1", "Trying it out", now=NOW))["refund_state"] == "processed"
    assert db.rows["gift_card"][0]["refund_processed_at"] == NOW.isoformat()


# 6
@pytest.mark.asyncio
async def test_refund_events(db):
    db.rows["gift_card"].append(_card(status="refunded", refund_state="requested"))
    event = {"event": "refund.processed", "data": {"transaction_reference": "gc-g1"}}
    assert await ps.handle_paystack_event(event) is True
    assert db.rows["gift_card"][0]["refund_state"] == "processed" and db.rows["gift_card"][0]["refund_processed_at"]
    db.mail.assert_not_awaited()
    await ps.handle_paystack_event({"event": "refund.failed", "data": {"transaction_reference": "gc-g1"}})
    assert db.rows["gift_card"][0]["refund_state"] == "failed" and _templates(db) == ["payment_refund_ops"]
    assert await ps.handle_paystack_event({"event": "refund.pending", "data": {"transaction_reference": "gc-g1"}}) is False
    assert await ps.handle_paystack_event({"event": "refund.processed", "data": {"transaction_reference": "T68"}}) is False


@pytest.mark.asyncio
async def test_refund_made_outside_tauka_alerts_ops(db):
    db.rows["gift_card"].append(_card())
    assert await rs.provider_update("gc-g1", "refund.processed", NOW) is False
    assert db.rows["gift_card"][0]["status"] == "paid"
    assert db.mail.await_args.args[2]["outside"] is True


# 7
@pytest.mark.asyncio
async def test_refunded_card_is_dead(db):
    db.rows["gift_card"].append(_card())
    await rs.refund("ops", "gc-g1", "Bought by mistake", now=NOW)
    with pytest.raises(cs.CommerceRefused) as e:
        await cs.redeem("learner", "ABCDEFGHJKLM", NOW)
    assert e.value.reason == "no_such_card"
    assert await cs.card_paid("g1") is None
    assert db.rows["gift_card"][0]["status"] == "refunded"


def test_orders_lists_both_kinds_and_says_what_can_be_refunded(db):
    with patch.object(rs, "_now", return_value=NOW):
        db.rows["gift_card"] += [_card(), _card(id="g2", status="redeemed", redeemed_at=OLD, payment_reference="gc-g2"),
                                 _card(id="g3", status="pending", payment_reference="gc-g3")]
        db.rows["sponsorship"].append(_seat())
        got = {o["payment_reference"]: o for o in rs.orders("ops")}
    assert set(got) == {"gc-g1", "gc-g2", "sp-s1"}                      # an unpaid checkout isn't an order
    assert got["gc-g1"]["refundable"] and not got["gc-g2"]["refundable"] and got["sp-s1"]["refundable"]
    assert got["sp-s1"]["in_use"] and got["sp-s1"]["payer_email"] == "giver@x.test" and "sponsor_email" not in got["sp-s1"]


@pytest.mark.parametrize("name,data", [
    ("payment_refunded", dict(amount="USD 150.00", months=2, tier="Tutor")),
    ("seat_refunded", dict(months=1, tier="Learner")),
    ("payment_refund_ops", dict(reference="gc-g1", state="failed", outside=False)),
    ("payment_refund_ops", dict(reference="gc-g1", outside=True)),
    ("payment_refund_ops", dict(reference="mo-p1", state="processed", outside=True, monthly=True)),
    ("payment_refund_ops", dict(reference="a1", state="refunded", outside=True, addon=True, sessions=4)),
])
def test_refund_emails_render(name, data):
    from app.services import email_service
    html = email_service._jinja_env.get_template(f"{name}.html").render(subject="s", web_base_url="https://tauka.io", **data)
    assert data.get("amount", data.get("reference", "month")) in html
