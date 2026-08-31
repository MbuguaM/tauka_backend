"""O-08 — the role gates, tested through the routes.

`require_admin` and `require_tutor` both checked `app_metadata.role`, a key no
account in `auth.users` carries. Every one of the 22 admin-gated endpoints and
20 tutor-gated endpoints returned 403 to every caller, while the database
recognised one admin (`app.admin_users`) and one tutor (`app.tutor`).

It survived because the existing suites for these features test the *service*
layer — `payout_service`, `handoff_service`, `feedback_service` — and never
route a request through the dependency. So these tests go through TestClient on
purpose: a gate is only tested where the request meets it.

Three cases per gate, and the third is the one worth keeping: a database that
cannot be reached must produce a refusal, never a grant.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import make_token, TEST_USER_ID


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data, count=len(self._data))


class _FakeDB:
    """Dispatches by table name. A value of Exception means that read fails."""

    def __init__(self, **tables):
        self._tables = tables

    def schema(self, _name):
        return self

    def table(self, name):
        val = self._tables.get(name, [])
        if isinstance(val, Exception):
            raise val
        return _FakeQuery(val)


ADMIN_ROW = [{"id": TEST_USER_ID}]
TUTOR_ROW = [{"id": TEST_USER_ID}]
STATS = [{"day": "2026-08-28", "pattern_class": "payment_handle", "shown": 3, "proceeded": 1}]


@pytest.fixture
def headers():
    return {"Authorization": f"Bearer {make_token()}"}


# ── require_admin ─────────────────────────────────────────────────────────────

def test_admin_in_admin_users_is_admitted(client, headers):
    """The regression. A row in app.admin_users must reach the handler."""
    db = _FakeDB(admin_users=ADMIN_ROW, trust_warning_stat=STATS)
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.routes.trust.db", db),
    ):
        resp = client.get("/trust/warnings", headers=headers)
    assert resp.status_code == 200


def test_non_admin_is_refused(client, headers):
    db = _FakeDB(admin_users=[], trust_warning_stat=STATS)
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.routes.trust.db", db),
    ):
        resp = client.get("/trust/warnings", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin access required"


def test_unreadable_admin_table_fails_closed(client, headers):
    """An unreachable database is not a grant."""
    db = _FakeDB(admin_users=RuntimeError("connection refused"), trust_warning_stat=STATS)
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.routes.trust.db", db),
    ):
        resp = client.get("/trust/warnings", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Could not verify admin role"


def test_soft_deleted_admin_is_refused(client, headers):
    """deleted_at IS NULL is applied in the query; a filtered-out admin is no
    admin. The fake returns what the filter would have returned — empty."""
    db = _FakeDB(admin_users=[], trust_warning_stat=STATS)
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.routes.trust.db", db),
    ):
        resp = client.get("/trust/warnings", headers=headers)
    assert resp.status_code == 403


# ── require_tutor ─────────────────────────────────────────────────────────────

def test_tutor_in_tutor_table_is_admitted(client, headers):
    db = _FakeDB(tutor=TUTOR_ROW)
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.routes.tutor_portal.tutor_service.get_payout_settings", return_value=None),
    ):
        resp = client.get("/tutor/payout-settings", headers=headers)
    assert resp.status_code == 200


def test_non_tutor_is_refused(client, headers):
    db = _FakeDB(tutor=[])
    with patch("app.services.supabase_client.supabase_admin", db):
        resp = client.get("/tutor/payout-settings", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Tutor access required"


def test_unreadable_tutor_table_fails_closed(client, headers):
    db = _FakeDB(tutor=RuntimeError("connection refused"))
    with patch("app.services.supabase_client.supabase_admin", db):
        resp = client.get("/tutor/payout-settings", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Could not verify tutor role"


# ── the shape that produced the outage ────────────────────────────────────────

def test_gates_do_not_read_auth_metadata(client, headers):
    """Guard against the old implementation coming back.

    Every real account's app_metadata is {"provider":"email","providers":[...]}
    — no `role` key. If either gate goes back to reading it, the admin row below
    stops being enough and this fails.
    """
    db = _FakeDB(admin_users=ADMIN_ROW, trust_warning_stat=STATS)
    db.auth = SimpleNamespace(
        admin=SimpleNamespace(
            get_user_by_id=lambda _uid: (_ for _ in ()).throw(
                AssertionError("role gate must not call auth.admin.get_user_by_id")
            )
        )
    )
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.routes.trust.db", db),
    ):
        resp = client.get("/trust/warnings", headers=headers)
    assert resp.status_code == 200
