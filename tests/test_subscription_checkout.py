"""`POST /subscriptions/checkout` — enrolment plan H4 / 2.1.

Stripe Checkout always creates a NEW subscription, so a learner who already has
a live one must be sent to the billing portal instead of being billed twice.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _db_with_student(row):
    db = MagicMock()
    db.schema.return_value.table.return_value.select.return_value.eq.return_value \
        .limit.return_value.execute.return_value = SimpleNamespace(data=[row] if row else [])
    db.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=SimpleNamespace(email="a@b.c"))
    return db


def test_a_live_subscriber_is_refused_a_second_subscription(client, auth_headers):
    db = _db_with_student({"stripe_subscription_id": "sub_1", "subscription_status": "active"})
    with patch("app.routes.subscriptions.db", db), \
         patch("app.routes.subscriptions.create_subscription_checkout", AsyncMock()) as create:
        res = client.post("/subscriptions/checkout", json={"tier": "learner_tier"}, headers=auth_headers)
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "already_subscribed"
    create.assert_not_called()


def test_a_free_learner_gets_a_checkout_url(client, auth_headers):
    db = _db_with_student({"stripe_subscription_id": None, "subscription_status": "none"})
    with patch("app.routes.subscriptions.db", db), \
         patch("app.routes.subscriptions.create_subscription_checkout",
               AsyncMock(return_value={"checkout_url": "https://checkout.stripe.test/x", "session_id": "cs_1"})):
        res = client.post("/subscriptions/checkout", json={"tier": "learner_tier"}, headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == {"checkout_url": "https://checkout.stripe.test/x"}


def test_a_cancelled_subscriber_can_subscribe_again(client, auth_headers):
    db = _db_with_student({"stripe_subscription_id": "sub_old", "subscription_status": "cancelled"})
    with patch("app.routes.subscriptions.db", db), \
         patch("app.routes.subscriptions.create_subscription_checkout",
               AsyncMock(return_value={"checkout_url": "https://checkout.stripe.test/y", "session_id": "cs_2"})):
        res = client.post("/subscriptions/checkout", json={"tier": "learner_tier"}, headers=auth_headers)
    assert res.status_code == 200
