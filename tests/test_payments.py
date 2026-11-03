"""One-off payments (§145).

Pinned:
1. A Paystack webhook only counts with a correct signature over the raw body.
2. settle() grants once, and grants nothing for a wrong amount, a wrong currency,
   an unknown reference or someone else's reference.
3. Only charge.success with status success does anything.
4. checkout() sends Paystack the row's reference and minor units, and a refusal
   raises PaymentFailed rather than returning a dead link.
5. The demo provider only exists on a local API_BASE_URL.
6. A failed checkout releases a held sponsorship request, and is reported as
   payment_failed.
"""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import payments as routes
from app.services import commerce_service as cs
from app.services import payment_service as ps
from app.services import sponsorship_service as sp
from tests.fakedb import FakeDB

KEY = "sk_test_x"
CARD = {"id": "g1", "amount_minor": 15000, "currency": "USD", "status": "pending", "months": 2, "tier": "tutor_tier",
        "purchaser_email": "b@x.test"}


@pytest.fixture
def db():
    fake = FakeDB({"gift_card": [dict(CARD)], "sponsorship": [
        {"id": "s1", "amount_minor": 22500, "currency": "USD", "status": "pending", "months": 3, "tier": "tutor_tier"}],
        "sponsorship_request": [], "feature_flag": [{"key": "gift_cards", "status": "ready"},
                                                    {"key": "sponsorship", "status": "ready"}]})
    card, seat = AsyncMock(), AsyncMock()
    with patch.object(ps, "db", fake), patch("app.services.commerce_service.db", fake), \
         patch("app.services.sponsorship_service.db", fake), patch("app.core.flags.db", fake), \
         patch.object(ps.settings, "paystack_secret_key", KEY), patch.object(ps.settings, "payment_provider", "paystack"), \
         patch.object(ps.settings, "api_base_url", "https://api.tauka.io"), \
         patch("app.services.commerce_service.card_paid", card), patch("app.services.sponsorship_service.paid", seat):
        fake.card, fake.seat = card, seat
        yield fake


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router, prefix="/payments")
    return TestClient(app, follow_redirects=False)


def _sign(body: bytes) -> str:
    return hmac.new(KEY.encode(), body, hashlib.sha512).hexdigest()


def _charge(reference="gc-g1", amount=15000, currency="USD", status="success", event="charge.success") -> bytes:
    return json.dumps({"event": event, "data": {"status": status, "reference": reference, "amount": amount,
                                                "currency": currency}}).encode()


# 1
def test_webhook_needs_a_good_signature(db, client):
    body = _charge()
    assert client.post("/payments/paystack/webhook", content=body).status_code == 401
    assert client.post("/payments/paystack/webhook", content=body,
                       headers={"x-paystack-signature": "0" * 128}).status_code == 401
    db.card.assert_not_awaited()
    assert client.post("/payments/paystack/webhook", content=body,
                       headers={"x-paystack-signature": _sign(body)}).status_code == 200
    db.card.assert_awaited_once_with("g1")


def test_no_key_means_no_signature_passes():
    with patch.object(ps.settings, "paystack_secret_key", ""):
        assert ps.paystack_signature_ok(b"{}", hmac.new(b"", b"{}", hashlib.sha512).hexdigest()) is False


def test_failed_grant_asks_paystack_to_retry(db, client):
    db.card.side_effect = RuntimeError("db down")
    body = _charge()
    assert client.post("/payments/paystack/webhook", content=body,
                       headers={"x-paystack-signature": _sign(body)}).status_code == 500


# 2
@pytest.mark.asyncio
@pytest.mark.parametrize("reference,amount,currency", [
    ("gc-g1", 100, "USD"), ("gc-g1", 15000, "KES"), ("gc-nope", 15000, "USD"), ("xx-g1", 15000, "USD"), ("", 15000, "USD"),
])
async def test_settle_refuses_what_does_not_match(db, reference, amount, currency):
    assert await ps.settle(reference, amount, currency) is False
    db.card.assert_not_awaited()
    db.seat.assert_not_awaited()


@pytest.mark.asyncio
async def test_settle_routes_by_kind(db):
    assert await ps.settle("gc-g1", 15000, "usd") is True
    assert await ps.settle("sp-s1", 22500, "USD") is True
    db.card.assert_awaited_once_with("g1")
    db.seat.assert_awaited_once_with("s1")


# 3
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_charge(status="failed"), _charge(event="refund.processed")])
async def test_only_a_successful_charge_counts(db, body):
    assert await ps.handle_paystack_event(json.loads(body)) is False
    db.card.assert_not_awaited()


# 4
def _paystack(status_code, payload, seen=None):
    def handler(request):
        if seen is not None:
            seen.update(json.loads(request.content), auth=request.headers["authorization"])
        return httpx.Response(status_code, json=payload)
    real = httpx.AsyncClient
    return patch("app.services.payment_service.httpx.AsyncClient",
                 lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.asyncio
async def test_checkout_sends_reference_and_minor_units(db):
    seen = {}
    ok = {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc", "reference": "gc-g1"}}
    with _paystack(200, ok, seen):
        pay = await ps.checkout("gc", "g1", 15000, "usd", "b@x.test", "A gift")
    assert pay.url == "https://checkout.paystack.com/abc" and pay.reference == "gc-g1" and pay.provider == "paystack"
    assert seen["amount"] == "15000" and seen["currency"] == "USD" and seen["reference"] == "gc-g1"
    assert seen["auth"] == f"Bearer {KEY}" and json.loads(seen["metadata"])["description"] == "A gift"
    assert seen["callback_url"].endswith("/gift-card/thanks")


@pytest.mark.asyncio
async def test_checkout_refusal_raises(db):
    with _paystack(400, {"status": False, "message": "Currency not supported"}):
        with pytest.raises(ps.PaymentFailed, match="Currency not supported"):
            await ps.checkout("gc", "g1", 15000, "USD", "b@x.test", "A gift")


@pytest.mark.asyncio
async def test_gift_card_failure_is_reported(db):
    with _paystack(400, {"status": False, "message": "No"}):
        with pytest.raises(cs.CommerceRefused) as e:
            await cs.buy_card("tutor_tier", 1, "b@x.test", None)
    assert e.value.reason == "payment_failed"


# 5
@pytest.mark.parametrize("base,expected", [("http://127.0.0.1:8000", True), ("https://api.tauka.io", False)])
def test_demo_only_runs_locally(base, expected):
    with patch.object(ps.settings, "payment_provider", "demo"), patch.object(ps.settings, "api_base_url", base):
        assert ps.configured() is expected


def test_demo_routes_are_gone_unless_on(db, client):
    assert client.get("/payments/demo/gc-g1").status_code == 404
    assert client.post("/payments/demo/gc-g1").status_code == 404
    with patch.object(ps.settings, "payment_provider", "demo"):                 # demo, but a public API_BASE_URL
        assert client.post("/payments/demo/gc-g1").status_code == 404
    db.card.assert_not_awaited()


def test_demo_checkout_pays_and_returns(db, client):
    with patch.object(ps.settings, "payment_provider", "demo"), \
         patch.object(ps.settings, "api_base_url", "http://127.0.0.1:8000"), \
         patch.object(ps.settings, "web_base_url", "http://localhost:5173"):
        page = client.get("/payments/demo/gc-g1")
        assert page.status_code == 200 and "USD 150.00" in page.text and "No real money" in page.text
        res = client.post("/payments/demo/gc-g1")
    assert res.status_code == 303 and res.headers["location"] == "http://localhost:5173/gift-card/thanks"
    db.card.assert_awaited_once_with("g1")


# 6
@pytest.mark.asyncio
async def test_failed_checkout_releases_the_hold(db):
    db.rows["sponsorship_request"].append({"id": "r1", "status": "listed", "held_until": None, "first_name": "Selam",
                                           "tier_wanted": "tutor_tier", "months_wanted": 6, "student_id": "x"})
    with _paystack(500, {"status": False, "message": "Down"}):
        with pytest.raises(sp.SponsorshipRefused) as e:
            await sp.checkout("waitlist", "g@x.test", request_id="r1", months=6)
    assert e.value.reason == "payment_failed"
    assert db.rows["sponsorship_request"][0]["held_until"] is None
    assert db.rows["sponsorship"][-1]["status"] == "cancelled"
