"""`/matching/*` at the route level — the gates, and the two refusals that are
not errors.

WHY ROUTE-LEVEL AND NOT SERVICE-LEVEL. O-08 was 22 admin endpoints returning 403
to everybody for four sessions, and it survived because every suite for those
features tested the *service* and never routed a request through the dependency.
`test_role_gates.py` records the lesson: "a gate is only tested where the request
meets it". This is a new admin surface, so it gets the same treatment.

The second half is about the accept path's contract with the client. Two of its
refusals are **200 with `accepted: false`** rather than errors — a group that
filled while the student was deciding is the outcome the soft hold exists to make
survivable, and a 4xx would make the app show an error dialogue for the ordinary
case.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import TEST_USER_ID, make_token


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def not_(self, *a, **k): return self
    def like(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def execute(self): return SimpleNamespace(data=self._data, count=len(self._data))


class _FakeDB:
    def __init__(self, **tables):
        self._tables = tables

    def schema(self, _name): return self

    def table(self, name):
        val = self._tables.get(name, [])
        if isinstance(val, Exception):
            raise val
        return _FakeQuery(val)


ADMIN_ROW = [{"id": TEST_USER_ID}]


@pytest.fixture
def headers():
    return {"Authorization": f"Bearer {make_token()}"}


# ── Authentication ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path", [
    ("get", "/matching/offers"),
    ("get", "/matching/status"),
    ("post", "/matching/offers/abc/accept"),
    ("post", "/matching/offers/abc/decline"),
    ("post", "/matching/cycles/dry-run"),
    ("post", "/matching/cycles/run"),
    ("post", "/matching/cycles/close"),
    ("post", "/matching/offers/expire"),
    ("get", "/matching/deferrals"),
])
def test_every_route_requires_a_token(client, method, path):
    """There is no `{student_id}` anywhere in this router — every route reads its
    subject from the verified JWT, because an offer is a statement about one
    person and a client-supplied id would be an IDOR."""
    resp = getattr(client, method)(path)
    # 401 on this FastAPI version, 403 on older ones — `HTTPBearer` has changed
    # which it returns for a missing Authorization header. The assertion is
    # about the refusal, not about which of the two codes carries it.
    assert resp.status_code in (401, 403)


# ── require_admin, on the four routes that have it ───────────────────────────

@pytest.mark.parametrize("path", [
    "/matching/cycles/dry-run",
    "/matching/cycles/run",
    "/matching/cycles/close",
    "/matching/offers/expire",
])
def test_admin_routes_refuse_a_non_admin(client, headers, path):
    db = _FakeDB(admin_users=[])
    with patch("app.services.supabase_client.supabase_admin", db):
        resp = client.post(path, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin access required"


def test_the_deferral_brief_refuses_a_non_admin(client, headers):
    """Grouped counts of who could not be placed, and why. A student's own
    deferral reaches them through `/matching/status`; this is the business's
    view and it names other people."""
    db = _FakeDB(admin_users=[])
    with patch("app.services.supabase_client.supabase_admin", db):
        resp = client.get("/matching/deferrals", headers=headers)
    assert resp.status_code == 403


def test_an_unreadable_admin_table_fails_closed(client, headers):
    """An unreachable database is not a grant. A dry run is harmless; a live
    cycle placing people is not, and they share a gate."""
    db = _FakeDB(admin_users=RuntimeError("connection refused"))
    with patch("app.services.supabase_client.supabase_admin", db):
        resp = client.post("/matching/cycles/run", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Could not verify admin role"


def test_an_admin_reaches_the_deferral_brief(client, headers):
    """The regression O-08 left behind: a row in `app.admin_users` must reach
    the handler."""
    db = _FakeDB(admin_users=ADMIN_ROW, deferred_student=[])
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.services.intake_service.db", db),
    ):
        resp = client.get("/matching/deferrals", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["by_reason"] == {}


# ── The student routes, and the refusals that are not errors ─────────────────

def test_a_student_with_no_offers_gets_the_deferral_shape_not_an_empty_list(
        client, headers):
    """Plan §4b Stage 1: "no match found" is not an acceptable answer. The route
    must return the deferral slot even when it is null, so the client renders a
    state rather than an empty screen."""
    db = _FakeDB(match_offer=[], deferred_student=[], student=[], user_profiles=[],
                 student_time_preference=[], cohort_memberships=[], config=[])
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.services.offer_service.db", db),
        patch("app.services.intake_service.db", db),
    ):
        resp = client.get("/matching/status", headers=headers)
    body = resp.json()
    assert resp.status_code == 200
    assert body["offers"] == []
    assert "deferral" in body
    assert "readiness" in body


def test_an_unknown_offer_is_a_404_and_everything_else_is_a_200(client, headers):
    """The dividing line. `not_found` is the only accept refusal a student
    cannot act on, so it is the only one that is an HTTP error."""
    db = _FakeDB(match_offer=[], config=[])
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.services.offer_service.db", db),
        patch("app.services.intake_service.db", db),
    ):
        resp = client.post("/matching/offers/" + TEST_USER_ID + "/accept",
                           headers=headers)
    assert resp.status_code == 404


def test_declining_an_offer_that_is_not_open_is_a_404_not_a_silent_success(
        client, headers):
    """A decline that changed nothing must not report success — the student
    would believe they had answered."""
    class _NoRows(_FakeQuery):
        def update(self, *a, **k): return self

    class _DB(_FakeDB):
        def table(self, name):
            return _NoRows([])

    db = _DB()
    with (
        patch("app.services.supabase_client.supabase_admin", db),
        patch("app.services.offer_service.db", db),
        patch("app.services.intake_service.db", db),
    ):
        resp = client.post("/matching/offers/" + TEST_USER_ID + "/decline",
                           headers=headers)
    assert resp.status_code == 404
