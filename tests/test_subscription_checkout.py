"""`POST /subscriptions/checkout`: enrolment plan H4 / 2.1, on billing_service since §151.

A checkout always starts a NEW subscription, so a learner who already has a live plan must
be refused rather than billed twice. The rules are pinned in test_billing.py; this pins the route.
"""
from unittest.mock import AsyncMock, patch

from app.services.billing_service import BillingRefused


def test_a_live_subscriber_is_refused_a_second_subscription(client, auth_headers):
    with patch("app.routes.subscriptions.billing_service.checkout",
               AsyncMock(side_effect=BillingRefused("already_subscribed"))):
        res = client.post("/subscriptions/checkout", json={"tier": "learner_tier"}, headers=auth_headers)
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "already_subscribed"


def test_a_free_learner_gets_a_checkout_url(client, auth_headers):
    with patch("app.routes.subscriptions.billing_service.checkout",
               AsyncMock(return_value={"checkout_url": "https://pay.test/x"})) as create:
        res = client.post("/subscriptions/checkout", json={"tier": "learner_tier"}, headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == {"checkout_url": "https://pay.test/x"}
    assert create.await_args.args[1:] == ("learner_tier", "card")


def test_an_unconfigured_server_says_so(client, auth_headers):
    with patch("app.routes.subscriptions.billing_service.checkout",
               AsyncMock(side_effect=BillingRefused("not_configured"))):
        res = client.post("/subscriptions/checkout", json={"tier": "learner_tier"}, headers=auth_headers)
    assert res.status_code == 503
