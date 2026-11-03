"""ADM-1 and ADM-2: the rules behind the payout and handoff screens.

Who may reach them, and that a payment can't be recorded on a line nobody approved,
or recorded twice.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, require_admin
from app.main import app
from app.services import hierarchy_service, payout_service


@pytest.fixture
def client_as():
    """A client signed in as an admin holding exactly these capabilities."""
    def make(*caps):
        app.dependency_overrides[get_current_user] = lambda: "admin-1"
        app.dependency_overrides[require_admin] = lambda: "admin-1"
        patcher = patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: c in caps)
        patcher.start()
        patchers.append(patcher)
        return TestClient(app)
    patchers = []
    yield make
    for p in patchers:
        p.stop()
    app.dependency_overrides.clear()


PAYOUT_ROUTES = [("get", "/payouts/runs"), ("get", "/payouts/runs/r1"), ("get", "/payouts/float"),
                 ("post", "/payouts/runs/compute"), ("post", "/payouts/runs/approve"),
                 ("post", "/payouts/lines/disburse")]
HANDOFF_ROUTES = [("get", "/tutoring/handoff/pending"), ("get", "/tutoring/links/lookup?email=a@b.co"),
                  ("get", "/tutoring/handoff/candidates/s1"), ("get", "/tutoring/links/student/s1"),
                  ("get", "/tutoring/links/evaluate?student_id=s&tutor_id=t"),
                  ("post", "/tutoring/links"), ("post", "/tutoring/handoff/begin"),
                  ("post", "/tutoring/handoff/complete"), ("post", "/tutoring/handoff/cancel")]


@pytest.mark.parametrize("method,path", PAYOUT_ROUTES)
def test_money_routes_refuse_an_admin_without_finance_or_ops(client_as, method, path):
    assert getattr(client_as("content"), method)(path).status_code == 403


@pytest.mark.parametrize("method,path", HANDOFF_ROUTES)
def test_handoff_routes_refuse_an_admin_without_ops(client_as, method, path):
    assert getattr(client_as("finance"), method)(path).status_code == 403


def _db(update_rows, line_status="approved", run_lines=None):
    """A database whose payout_line update matches `update_rows`."""
    db = MagicMock()
    tables = {}

    def table(name):
        t = tables.setdefault(name, MagicMock(name=name))
        chain = t
        for m in ("update", "select", "eq", "limit"):
            getattr(chain, m).return_value = chain
        return t

    db.schema.return_value.table.side_effect = table
    line = table("payout_line")
    results = [MagicMock(data=update_rows)]
    if update_rows:
        results.append(MagicMock(data=run_lines or []))
    else:
        results.append(MagicMock(data=[{"status": line_status}] if line_status else []))
    line.execute.side_effect = results
    table("payout_run").execute.return_value = MagicMock(data=[])
    return db, tables


@pytest.mark.asyncio
async def test_a_line_that_isnt_approved_cannot_be_recorded_as_paid():
    db, tables = _db([], line_status="pending")
    with patch.object(payout_service, "db", db):
        result = await payout_service.record_disbursement("l1", "MPESA123")
    assert result == {"settled": False, "reason": "line is pending"}
    tables["payout_line"].eq.assert_any_call("status", "approved")
    db.schema.return_value.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_a_second_tap_keeps_the_first_reference():
    db, _ = _db([], line_status="settled")
    with patch.object(payout_service, "db", db):
        result = await payout_service.record_disbursement("l1", "SECOND")
    assert result == {"settled": False, "reason": "line is settled"}
    db.schema.return_value.rpc.assert_not_called()


@pytest.mark.parametrize("others,status", [
    ([{"status": "settled"}, {"status": "approved"}], "disbursing"),
    ([{"status": "settled"}, {"status": "settled"}, {"status": "cancelled"}], "settled"),
])
@pytest.mark.asyncio
async def test_the_run_follows_its_lines(others, status):
    row = {"id": "l1", "run_id": "r1", "currency": "KES", "amount_minor": 500}
    db, tables = _db([row], run_lines=others)
    with patch.object(payout_service, "db", db):
        result = await payout_service.record_disbursement("l1", "MPESA123")
    assert result["settled"] is True
    assert tables["payout_run"].update.call_args[0][0]["status"] == status
    assert db.schema.return_value.rpc.call_args[0][1]["p_amount"] == 500


@pytest.mark.asyncio
async def test_opening_the_float_page_sends_no_mail():
    state = {"currencies": {"KES": {"fold_minor": 1, "agrees": False}}}
    with patch.object(payout_service, "reconcile", return_value=state), \
         patch.object(payout_service, "_config", return_value={"payout.float_low_balance_minor": '{"KES": 100}'}), \
         patch("app.services.email_service.send_template_email") as send:
        result = await payout_service.check_float_balance(alert=False)
    send.assert_not_called()
    assert result["mismatches"] == ["KES"] and result["low"][0]["currency"] == "KES"


def test_run_detail_never_selects_the_encrypted_details():
    db = MagicMock()
    tables = {}

    def table(name):
        t = tables.setdefault(name, MagicMock(name=name))
        for m in ("select", "eq", "in_", "limit"):
            getattr(t, m).return_value = t
        return t

    db.schema.return_value.table.side_effect = table
    table("payout_run").execute.return_value = MagicMock(data=[{"id": "r1"}])
    table("payout_line").execute.return_value = MagicMock(data=[{"id": "l1", "tutor_id": "t1"}])
    table("user_profiles").execute.return_value = MagicMock(data=[{"id": "t1", "first_name": "Sara"}])
    table("tutor_payout_settings").execute.return_value = MagicMock(
        data=[{"tutor_id": "t1", "method": "mpesa", "details_last4": "1234", "verified": True}])
    with patch.object(payout_service, "db", db):
        result = payout_service.run_detail("r1")
    assert "encrypted_details" not in tables["tutor_payout_settings"].select.call_args[0][0]
    line = result["lines"][0]
    assert (line["tutor_name"], line["pay_method"], line["pay_last4"]) == ("Sara", "mpesa", "1234")
