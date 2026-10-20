"""Account provisioning — the server half of "signup happens on the web".

Signup moved to tauka-react-web on 2026-08-28. This route is now the only path
by which a Tauka account comes into existence, so the things worth asserting are
the ones whose failure produces an account that is *wrong* rather than absent:

1. **The role is not a parameter.** `user_type` is always 'student'. Tutors are
   admin-created; an endpoint any signed-in user can reach must not accept its
   own role, and `user_type = 'tutor'` is read by RLS, not just by the UI.
2. **`app.student.tier` is never written.** The column default is the only thing
   that sets an initial tier, and tauka-python's billing path owns it after
   that. A literal here is how CONFLICT-045 happened in the other direction.
3. **Idempotence.** Email confirmation is on, so the web calls this after signup
   AND again on first sign-in. The second call must not duplicate or overwrite.
4. **The caller comes from the JWT.** There is no id or email in the body; this
   pins that so nobody adds one.
5. **A referral failure does not cost the student their account.** Attribution
   is immutable and losing it costs a tutor their share — but failing the whole
   provision costs the student the account, which is worse.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import make_token, TEST_USER_ID


class _Tbl:
    """Enough PostgREST surface for provisioning_service, and no more."""

    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filter = None

    def select(self, *a, **k):
        return self

    def eq(self, _col, val):
        self._filter = val
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, payload):
        self.store.setdefault("inserts", []).append((self.name, payload))
        self.store[self.name] = self.store.get(self.name, []) + [payload]
        return self

    def update(self, payload):
        self.store.setdefault("updates", []).append((self.name, payload))
        return self

    def execute(self):
        rows = self.store.get(self.name, [])
        if self._filter is not None:
            rows = [r for r in rows if r.get("id") == self._filter]
        return SimpleNamespace(data=rows)


class _DB:
    def __init__(self, store):
        self.store = store

    def schema(self, _n):
        return self

    def table(self, name):
        return _Tbl(self.store, name)


def _inserted(store, table):
    """The payload of the first insert into `table`."""
    return next(p for (t, p) in store.get("inserts", []) if t == table)


@pytest.fixture
def headers():
    return {"Authorization": f"Bearer {make_token()}"}


def _run(client, headers, store, body, referral=None):
    db = _DB(store)
    with patch("app.services.provisioning_service.db", db):
        if referral is not None:
            with patch("app.services.tutor_referral_service.attribute_signup", referral):
                return client.post("/auth/provision", json=body, headers=headers)
        return client.post("/auth/provision", json=body, headers=headers)


# ── the role is not a parameter ──────────────────────────────────────────────

def test_user_type_is_always_student(client, headers):
    store = {}
    resp = _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": True})
    assert resp.status_code == 200
    assert resp.json()["user_type"] == "student"
    assert _inserted(store, "user_profiles")["user_type"] == "student"


def test_a_supplied_user_type_is_ignored(client, headers):
    """Self-promotion guard. `user_type` is not on the request model, so an
    extra key in the body must not reach the insert."""
    store = {}
    resp = _run(client, headers, store,
                {"first_name": "Hanna", "age_confirmed": True, "user_type": "tutor"})
    assert resp.status_code == 200
    assert resp.json()["user_type"] == "student"
    assert _inserted(store, "user_profiles")["user_type"] == "student"


# ── tier is never written ────────────────────────────────────────────────────

def test_student_insert_carries_no_tier(client, headers):
    store = {}
    _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": True})
    student = _inserted(store, "student")
    assert student == {"id": TEST_USER_ID}
    assert "tier" not in student


# ── the age attestation (L-005) ──────────────────────────────────────────────

def test_age_confirmed_is_recorded(client, headers):
    store = {}
    _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": True})
    assert _inserted(store, "user_profiles").get("age_confirmed_at")


def test_unticked_box_is_not_an_attestation(client, headers):
    """False leaves the column null. The server does not pretend an unticked box
    is an attestation, even though the client should refuse to submit."""
    store = {}
    _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": False})
    assert "age_confirmed_at" not in _inserted(store, "user_profiles")


# ── idempotence ──────────────────────────────────────────────────────────────

def test_second_call_is_a_no_op(client, headers):
    store = {}
    first = _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": True})
    second = _run(client, headers, store, {"first_name": "Someone Else", "age_confirmed": True})

    assert first.json()["profile_created"] is True
    assert second.json()["profile_created"] is False
    assert second.json()["student_created"] is False
    assert second.json()["already_provisioned"] is True

    # And the second call did not overwrite the name.
    profiles = [p for (t, p) in store["inserts"] if t == "user_profiles"]
    assert len(profiles) == 1
    assert profiles[0]["first_name"] == "Hanna"


# ── the caller is the JWT ────────────────────────────────────────────────────

def test_body_cannot_choose_the_account(client, headers):
    """Regression guard: no id/email field may be added to this route."""
    store = {}
    other = "00000000-0000-0000-0000-0000000000ff"
    _run(client, headers, store,
         {"first_name": "Hanna", "age_confirmed": True, "user_id": other, "id": other})
    assert _inserted(store, "user_profiles")["id"] == TEST_USER_ID


def test_unauthenticated_is_refused(client):
    resp = client.post("/auth/provision", json={"age_confirmed": True})
    assert resp.status_code == 401


# ── a referral failure must not cost the account ─────────────────────────────

def test_referral_failure_still_provisions(client, headers):
    store = {}

    async def boom(**_kwargs):
        raise RuntimeError("referral service down")

    resp = _run(client, headers, store,
                {"first_name": "Hanna", "age_confirmed": True, "referral_code": "ABCD1234"},
                referral=boom)
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile_created"] is True
    assert body["student_created"] is True
    assert body["referral"]["attributed"] is False
    assert body["referral"]["reason"] == "attribution_failed"


def test_referral_is_passed_through(client, headers):
    store = {}
    seen = {}

    async def ok(**kwargs):
        seen.update(kwargs)
        return {"attributed": True, "reason": "ok"}

    resp = _run(client, headers, store,
                {"first_name": "Hanna", "age_confirmed": True,
                 "referral_code": "ABCD1234", "source_platform": "tiktok"},
                referral=ok)
    assert resp.status_code == 200
    assert seen["student_id"] == TEST_USER_ID
    assert seen["code"] == "ABCD1234"
    assert seen["source_platform"] == "tiktok"


# ── the placement result's goal (enrolment plan 1.7) ─────────────────────────

def test_goal_is_written_on_creation(client, headers):
    store = {}
    resp = _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": True, "learning_goal": "heritage"})
    assert resp.status_code == 200
    assert _inserted(store, "user_profiles")["learning_goal"] == "heritage"


def test_goal_outside_the_vocabulary_is_refused(client, headers):
    resp = _run(client, headers, {}, {"first_name": "Hanna", "age_confirmed": True, "learning_goal": "<script>"})
    assert resp.status_code == 422


def test_goal_fills_an_empty_one_but_never_overwrites(client, headers):
    store = {"user_profiles": [{"id": TEST_USER_ID, "user_type": "student", "learning_goal": None}],
             "student": [{"id": TEST_USER_ID}]}
    _run(client, headers, store, {"age_confirmed": True, "learning_goal": "travel"})
    assert ("user_profiles", {"learning_goal": "travel"}) in store.get("updates", [])
    store = {"user_profiles": [{"id": TEST_USER_ID, "user_type": "student", "learning_goal": "family"}],
             "student": [{"id": TEST_USER_ID}]}
    _run(client, headers, store, {"age_confirmed": True, "learning_goal": "travel"})
    assert not [u for u in store.get("updates", []) if "learning_goal" in u[1]]


# ── the banner arm (plan 7.10, §136) ─────────────────────────────────────────

def test_banner_arm_recorded_once(client, headers):
    store = {}
    _run(client, headers, store, {"first_name": "Hanna", "age_confirmed": True, "banner_arm": "dated"})
    assert _inserted(store, "acquisition_arm") == {"user_id": TEST_USER_ID, "arm": "dated"}
    resp = _run(client, headers, {}, {"age_confirmed": True, "banner_arm": "loud"})
    assert resp.status_code == 422
