"""AI token packs (§154, ALL-18).

Pinned:
1. Past the allowance, bought tokens pay for a call: held first, then settled to the real count.
2. A failed AI call gives everything back.
3. No tokens means the same 429 as before, and the per-minute limit is never bypassed.
4. A paid order grants through settle(), only for the right amount and currency.
5. confirm() grants only for the caller's own order and only when Paystack says success.
6. Checkout needs the flag, a learner and a working provider.
7. Refunds: unused any time, used within 7 days with revoke, otherwise refused, and a
   provider refusal puts everything back.
8. A paid plan's allowance is larger than Free's; other limits don't scale.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import ai_token_service as svc
from app.services import payment_service as ps
from app.services import rate_limit_service as rl
from app.services import refund_service as rf
from tests.fakedb import FakeDB

USER = "00000000-0000-0000-0000-000000000001"
ORDER = "11111111-1111-1111-1111-111111111111"
URL = "/ai/generate"


def _balance(db, args):
    return sum(r["delta"] for r in db.rows.get("ai_token_ledger", []) if r["student_id"] == args["p_student_id"])


def _spend(db, args):
    if _balance(db, args) < args["p_tokens"]:
        return False
    db.rows["ai_token_ledger"].append({"student_id": args["p_student_id"], "delta": -args["p_tokens"], "reason": "spend"})
    return True


def _settle(db, args):
    for r in db.rows["ai_token_purchase"]:
        if r["id"] == args["p_purchase_id"] and r["status"] == "pending":
            r["status"] = "paid"
            r["paid_at"] = datetime.now(timezone.utc).isoformat()
            db.rows["ai_token_ledger"].append({"student_id": r["student_id"], "delta": r["tokens"], "reason": "purchase",
                                               "purchase_id": r["id"]})
            return True
    return False


def _take_back(db, args):
    r = next(o for o in db.rows["ai_token_purchase"] if o["id"] == args["p_purchase_id"])
    removed = min(_balance(db, {"p_student_id": r["student_id"]}), r["tokens"])
    if removed:
        db.rows["ai_token_ledger"].append({"student_id": r["student_id"], "delta": -removed, "reason": "refund"})
    return removed


def _order(**over):
    return {"id": ORDER, "student_id": USER, "tokens": 100_000, "amount_minor": 300, "currency": "USD", "email": "a@x.test",
            "status": "pending", "payment_provider": "paystack", "payment_reference": f"tk-{ORDER}",
            "created_at": "2026-11-04T00:00:00+00:00", **over}


@pytest.fixture
def db():
    fake = FakeDB({"ai_token_ledger": [], "ai_token_purchase": [_order()], "student": [{"id": USER, "tier": "free_tier"}],
                   "feature_flag": [{"key": "ai_tokens", "status": "ready"}]},
                  rpcs={"ai_token_balance": _balance, "spend_ai_tokens": _spend, "settle_ai_token_purchase": _settle,
                        "take_back_ai_tokens": _take_back})
    with patch.object(svc, "db", fake), patch.object(ps, "db", fake), patch.object(rf, "db", fake), \
         patch("app.core.flags.db", fake), patch.object(ps.settings, "payment_provider", "paystack"), \
         patch.object(ps.settings, "paystack_secret_key", "sk_test_x"), \
         patch.object(ps.settings, "api_base_url", "https://api.tauka.io"):
        yield fake


def _total(db):
    return sum(r["delta"] for r in db.rows["ai_token_ledger"])


def _credit(db, tokens=100_000):
    db.rows["ai_token_ledger"].append({"student_id": USER, "delta": tokens, "reason": "purchase"})


# 1
def test_past_the_allowance_bought_tokens_pay_for_the_call(db, client, auth_headers):
    _credit(db)
    with patch("app.routes.ai.check_rate_limit", return_value=(False, "Daily token limit exceeded")), \
         patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="selam"), \
         patch("app.routes.ai.log_usage", new_callable=AsyncMock), patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock), \
         patch("app.routes.ai.count_tokens", return_value=10):
        res = client.post(URL, json={"prompt": "hi"}, headers=auth_headers)
    assert res.status_code == 200
    assert _total(db) == 100_000 - 20     # the real count: 10 for the prompt and 10 for the reply


def test_within_the_allowance_nothing_is_spent(db, client, auth_headers):
    _credit(db)
    with patch("app.routes.ai.check_rate_limit", return_value=(True, "OK")), \
         patch("app.routes.ai.call_ai", new_callable=AsyncMock, return_value="selam"), \
         patch("app.routes.ai.log_usage", new_callable=AsyncMock), patch("app.routes.ai.log_ai_usage", new_callable=AsyncMock):
        assert client.post(URL, json={"prompt": "hi"}, headers=auth_headers).status_code == 200
    assert _total(db) == 100_000


# 2
def test_a_failed_call_gives_everything_back(db, client, auth_headers):
    _credit(db)
    with patch("app.routes.ai.check_rate_limit", return_value=(False, "Daily token limit exceeded")), \
         patch("app.routes.ai.call_ai", new_callable=AsyncMock, side_effect=ValueError("no")):
        assert client.post(URL, json={"prompt": "hi"}, headers=auth_headers).status_code == 400
    assert _total(db) == 100_000


# 3
def test_no_tokens_is_the_same_429(db, client, auth_headers):
    call = AsyncMock()
    with patch("app.routes.ai.check_rate_limit", return_value=(False, "Monthly token limit exceeded")), \
         patch("app.routes.ai.call_ai", call):
        res = client.post(URL, json={"prompt": "hi"}, headers=auth_headers)
    assert res.status_code == 429 and res.json()["detail"] == "Monthly token limit exceeded"
    call.assert_not_awaited()


def test_tokens_do_not_bypass_the_per_minute_limit(db, client, auth_headers):
    _credit(db)
    with patch("app.routes.ai.check_rate_limit", return_value=(False, "Too many requests")):
        assert client.post(URL, json={"prompt": "hi"}, headers=auth_headers).status_code == 429
    assert _total(db) == 100_000


def test_balance_reports_bought_tokens(db, client, auth_headers):
    _credit(db, 500)
    with patch("app.services.rate_limit_service.get_redis", return_value=None):
        data = client.get("/ai/balance", headers=auth_headers).json()
    assert data["purchased"] == 500 and data["tokens_on_sale"] is True


# 4
@pytest.mark.asyncio
async def test_settle_grants_once(db):
    assert await ps.settle(f"tk-{ORDER}", 300, "USD") is True
    assert await ps.settle(f"tk-{ORDER}", 300, "USD") is True
    assert _total(db) == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,currency", [(100, "USD"), (300, "KES")])
async def test_wrong_payment_grants_nothing(db, amount, currency):
    assert await ps.settle(f"tk-{ORDER}", amount, currency) is False
    assert _total(db) == 0


# 5
@pytest.mark.asyncio
async def test_confirm_grants_when_paystack_says_paid(db):
    with patch.object(ps, "paystack_call", AsyncMock(return_value={"status": "success", "amount": 300, "currency": "USD"})):
        res = await svc.confirm(USER, f"tk-{ORDER}")
    assert res["status"] == "paid" and res["balance"] == 100_000


@pytest.mark.asyncio
async def test_confirm_grants_nothing_while_abandoned(db):
    with patch.object(ps, "paystack_call", AsyncMock(return_value={"status": "abandoned", "amount": 300, "currency": "USD"})):
        res = await svc.confirm(USER, f"tk-{ORDER}")
    assert res["status"] == "pending" and res["balance"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("user,reference", [
    ("someone-else", f"tk-{ORDER}"), (USER, "tk-not-a-uuid"), (USER, f"gc-{ORDER}"), (USER, ""),
])
async def test_confirm_refuses_other_orders(db, user, reference):
    with pytest.raises(svc.TokensRefused) as exc:
        await svc.confirm(user, reference)
    assert exc.value.reason == "no_such_order"


# 6
@pytest.mark.asyncio
async def test_checkout_opens_a_priced_order(db):
    seen = AsyncMock(return_value=ps.Checkout("https://checkout.paystack.com/x", "paystack", "tk-new"))
    with patch.object(ps, "checkout", seen), patch.object(svc, "_email_of", return_value="a@x.test"):
        res = await svc.checkout(USER)
    assert res["checkout_url"].startswith("https://checkout.paystack.com")
    assert seen.await_args.args[0] == "tk" and seen.await_args.args[2:4] == (300, "USD")
    assert db.rows["ai_token_purchase"][-1]["payment_reference"] == "tk-new"


@pytest.mark.asyncio
async def test_checkout_refuses_while_the_flag_is_off(db):
    db.rows["feature_flag"][0]["status"] = "coming"
    with pytest.raises(svc.TokensRefused) as exc:
        await svc.checkout(USER)
    assert exc.value.reason == "not_enabled"


@pytest.mark.asyncio
async def test_checkout_is_for_learners(db):
    with pytest.raises(svc.TokensRefused) as exc:
        await svc.checkout("a-tutor")
    assert exc.value.reason == "learners_only"


@pytest.mark.asyncio
async def test_a_refused_checkout_marks_the_order_failed(db):
    with patch.object(ps, "checkout", AsyncMock(side_effect=ps.PaymentFailed("no"))), \
         patch.object(svc, "_email_of", return_value="a@x.test"), pytest.raises(svc.TokensRefused):
        await svc.checkout(USER)
    assert db.rows["ai_token_purchase"][-1]["status"] == "failed"


# 7
def _paid(db, tokens_left=100_000, days_ago=1):
    r = db.rows["ai_token_purchase"][0]
    r["status"] = "paid"
    r["paid_at"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    _credit(db, tokens_left)


@pytest.fixture
def can_refund():
    with patch.object(rf.hs, "admin_can", return_value=True), patch.object(rf, "_mail", new_callable=AsyncMock):
        yield


@pytest.mark.asyncio
async def test_unused_pack_refunds_and_removes_tokens(db, can_refund):
    _paid(db, days_ago=60)
    with patch.object(ps, "provider_refund", new_callable=AsyncMock) as money:
        res = await rf.refund("admin", f"tk-{ORDER}", "asked")
    assert res["status"] == "refunded"
    money.assert_awaited_once()
    assert _total(db) == 0


@pytest.mark.asyncio
async def test_used_pack_needs_revoke_inside_the_window(db, can_refund):
    _paid(db, tokens_left=40_000)
    with pytest.raises(rf.RefundRefused) as exc:
        await rf.refund("admin", f"tk-{ORDER}", "asked")
    assert exc.value.reason == "in_use"
    with patch.object(ps, "provider_refund", new_callable=AsyncMock):
        res = await rf.refund("admin", f"tk-{ORDER}", "asked", revoke_access=True)
    assert res["access_taken_back"] is True and _total(db) == 0


@pytest.mark.asyncio
async def test_used_pack_after_the_window_is_refused(db, can_refund):
    _paid(db, tokens_left=40_000, days_ago=8)
    with pytest.raises(rf.RefundRefused) as exc:
        await rf.refund("admin", f"tk-{ORDER}", "asked", revoke_access=True)
    assert exc.value.reason == "window_passed"
    assert _total(db) == 40_000


@pytest.mark.asyncio
async def test_a_refused_refund_puts_everything_back(db, can_refund):
    _paid(db)
    with patch.object(ps, "provider_refund", AsyncMock(side_effect=ps.PaymentFailed("no"))), pytest.raises(rf.RefundRefused):
        await rf.refund("admin", f"tk-{ORDER}", "asked")
    assert db.rows["ai_token_purchase"][0]["status"] == "paid" and _total(db) == 100_000


def test_orders_list_shows_token_packs(db, can_refund):
    _paid(db)
    orders = rf._token_orders(10)
    assert orders[0]["kind"] == "tk" and orders[0]["refundable"] is True and orders[0]["in_use"] is False


# 8
def _redis(day, month=0):
    r = MagicMock()
    r.incr.return_value = 1
    r.incrby.side_effect = [day, month]
    return r


def test_free_plan_stops_at_the_base_allowance():
    with patch.object(rl, "get_redis", return_value=_redis(8_001)), patch.object(rl, "_on_paid_plan", return_value=False):
        assert rl.check_rate_limit("u", 1, provider="deepseek") == (False, "Daily token limit exceeded")


def test_paid_plan_goes_past_it_up_to_its_own_limit():
    with patch.object(rl, "get_redis", return_value=_redis(8_001, 1)), patch.object(rl, "_on_paid_plan", return_value=True):
        assert rl.check_rate_limit("u", 1, provider="deepseek")[0] is True
    with patch.object(rl, "get_redis", return_value=_redis(40_001)), patch.object(rl, "_on_paid_plan", return_value=True):
        assert rl.check_rate_limit("u", 1, provider="deepseek")[0] is False


def test_only_chat_providers_scale():
    assert rl._on_paid_plan("u", "messaging") is False
    assert rl._on_paid_plan("u", "lyric_annotation") is False


def test_balance_shows_the_paid_allowance_once_past_the_base():
    r = MagicMock()
    r.mget.return_value = ["9000", "9000"]
    with patch.object(rl, "get_redis", return_value=r), patch.object(rl, "_on_paid_plan", return_value=True):
        data = rl.get_rate_limit_status("u", "deepseek")
    assert data["limit"] == 40_000 and data["remaining"] == 31_000
