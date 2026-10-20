"""Enrolment domain, first slice (§95): the waitlist and signup consents.

What is worth pinning:

1. **The waitlist tells nobody anything.** `POST /waitlist` answers `{ok: true}`
   for a new address, a known one, and a failed write alike.
2. **One row per address, merged, never weakened.** A re-submission keeps old
   answers it leaves blank, and an email opt-in once given stays given.
3. **Email only.** No phone or WhatsApp field is accepted (L-011), and goal /
   band / pace are closed vocabularies matching the table's CHECKs.
4. **Consents: one row per purpose, once per wording.** An unticked or missing
   purpose is recorded as not granted; a retried provision appends nothing.
5. **A consent failure never costs the account**, and an absent `consents`
   (an older page, or a plain sign-in) records nothing at all.

The CHECKs, RLS, grants and append-only trigger themselves were exercised
against production in a rolled-back transaction before §95 was applied.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import enrolment_service
from tests.conftest import make_token, TEST_USER_ID


class _Tbl:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.filters = []
        self.op = ("select", None)

    def select(self, *a, **k):
        self.op = ("select", None)
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def lt(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r[col] < val)
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, payload):
        self.op = ("insert", payload)
        return self

    def update(self, payload):
        self.op = ("update", payload)
        return self

    def delete(self):
        self.op = ("delete", None)
        return self

    def execute(self):
        if self.db.fail:
            raise RuntimeError("db down")
        rows = self.db.rows.setdefault(self.name, [])
        kind, payload = self.op
        if kind == "insert":
            new = payload if isinstance(payload, list) else [payload]
            for row in new:
                row = {"id": f"{self.name}-{len(rows) + 1}", **row}
                rows.append(row)
            return SimpleNamespace(data=new)
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if kind == "update":
            for r in hit:
                r.update(payload)
        elif kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        return SimpleNamespace(data=hit)


class _DB:
    def __init__(self, fail=False):
        self.rows: dict[str, list[dict]] = {}
        self.fail = fail
        self.auth = SimpleNamespace(admin=SimpleNamespace(
            get_user_by_id=lambda uid: SimpleNamespace(user=SimpleNamespace(email="Hanna@Example.com"))))

    def schema(self, _n):
        return self

    def table(self, name):
        return _Tbl(self, name)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.enrolment_service.db", fake):
        yield fake


@pytest.fixture
def sent():
    """Confirmation emails the route scheduled, as (email, token)."""
    calls = []

    async def fake(email, token):
        calls.append((email, token))

    with patch("app.services.enrolment_service.send_confirmation", fake):
        yield calls


# ── waitlist: the route ──────────────────────────────────────────────────────

def test_join_creates_one_lowercased_unconfirmed_row_and_mails_a_link(client, db, sent):
    resp = client.post("/waitlist", json={
        "email": "Hanna@Example.com", "goal": "heritage", "band": "europe",
        "pace_hours": 10, "email_opt_in": True, "source": "home_hero",
    })
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    [row] = db.rows["waitlist_entry"]
    assert row["email"] == "hanna@example.com"
    assert row["goal"] == "heritage" and row["pace_hours"] == 10
    assert row["email_opt_in"] is True
    assert row.get("confirmed_at") is None
    [(to, token)] = sent
    assert to.lower() == "hanna@example.com"
    # Only the hash is stored, never the token.
    assert row["confirm_token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(row)


def test_unconfirmed_entry_expires_in_a_week(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co"})
    expires = datetime.fromisoformat(db.rows["waitlist_entry"][0]["delete_after"])
    assert expires < datetime.now(timezone.utc) + timedelta(days=8)


def test_known_address_answers_exactly_like_a_new_one(client, db, sent):
    first = client.post("/waitlist", json={"email": "a@b.co"})
    second = client.post("/waitlist", json={"email": "A@B.co"})
    assert first.json() == second.json() == {"ok": True}
    assert len(db.rows["waitlist_entry"]) == 1


def test_failed_write_still_answers_ok_and_mails_nothing(client, sent):
    with patch("app.services.enrolment_service.db", _DB(fail=True)):
        resp = client.post("/waitlist", json={"email": "a@b.co"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert sent == []


def test_needs_no_auth(client, db, sent):
    assert client.post("/waitlist", json={"email": "a@b.co"}).status_code == 200


@pytest.mark.parametrize("body", [
    {"email": "not-an-email"},
    {"email": "a@b.co", "pace_hours": 7},
    {"email": "a@b.co", "goal": "romance"},
    {"email": "a@b.co", "band": "africa"},
    {"email": "a@b.co", "source": "tiktok"},
])
def test_rejects_values_outside_the_table_checks(client, db, sent, body):
    assert client.post("/waitlist", json=body).status_code == 422
    assert not db.rows.get("waitlist_entry")


def test_phone_is_not_stored(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co", "phone": "+254700000000", "whatsapp": True})
    [row] = db.rows["waitlist_entry"]
    assert "phone" not in row and "whatsapp" not in row


def test_rate_limited(client, db, sent):
    with patch("app.routes.waitlist.check_anonymous_rate_limit", return_value=(False, "Too many")):
        assert client.post("/waitlist", json={"email": "a@b.co"}).status_code == 429


# ── waitlist: double opt-in ──────────────────────────────────────────────────

def test_confirm_with_the_mailed_token(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co"})
    [(_, token)] = sent
    resp = client.post("/waitlist/confirm", json={"token": token})
    assert resp.json() == {"ok": True}
    row = db.rows["waitlist_entry"][0]
    assert row["confirmed_at"]
    assert datetime.fromisoformat(row["delete_after"]) > datetime.now(timezone.utc) + timedelta(days=360)
    # Idempotent: a second click (or a scanner that got past the page) is fine.
    assert client.post("/waitlist/confirm", json={"token": token}).json() == {"ok": True}


def test_wrong_token_confirms_nothing(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co"})
    assert client.post("/waitlist/confirm", json={"token": "x" * 43}).json() == {"ok": False}
    assert db.rows["waitlist_entry"][0].get("confirmed_at") is None


def test_malformed_token_is_rejected(client, db):
    assert client.post("/waitlist/confirm", json={"token": "short"}).status_code == 422
    assert client.post("/waitlist/remove", json={"token": "has spaces in it, twenty+"}).status_code == 422


def test_remove_deletes_the_entry_even_after_confirmation(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co"})
    [(_, token)] = sent
    client.post("/waitlist/confirm", json={"token": token})
    assert client.post("/waitlist/remove", json={"token": token}).json() == {"ok": True}
    assert db.rows["waitlist_entry"] == []


def test_confirmed_entry_is_untouched_by_the_public_form(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co", "goal": "travel", "email_opt_in": True})
    [(_, token)] = sent
    client.post("/waitlist/confirm", json={"token": token})
    before = dict(db.rows["waitlist_entry"][0])
    resp = client.post("/waitlist", json={"email": "a@b.co", "goal": "exam", "email_opt_in": False})
    assert resp.json() == {"ok": True}
    assert db.rows["waitlist_entry"][0] == before
    assert len(sent) == 1  # no second email to a confirmed inbox


def test_resend_is_throttled_to_once_an_hour(client, db, sent):
    client.post("/waitlist", json={"email": "a@b.co"})
    client.post("/waitlist", json={"email": "a@b.co"})
    assert len(sent) == 1
    db.rows["waitlist_entry"][0]["confirm_sent_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    client.post("/waitlist", json={"email": "a@b.co"})
    assert len(sent) == 2
    # The new link replaces the old one.
    old_token, new_token = sent[0][1], sent[1][1]
    assert client.post("/waitlist/confirm", json={"token": old_token}).json() == {"ok": False}
    assert client.post("/waitlist/confirm", json={"token": new_token}).json() == {"ok": True}


def test_confirmation_email_links_to_website_pages_not_api_gets():
    from app.services.email_service import _jinja_env
    html = _jinja_env.get_template("waitlist_confirm.html").render(
        token="TOKEN123", subject="s", web_base_url="https://tauka.io", api_base_url="https://api.tauka.io")
    assert "https://tauka.io/waitlist/confirm?token=TOKEN123" in html
    assert "https://tauka.io/waitlist/remove?token=TOKEN123" in html
    assert "api.tauka.io/waitlist" not in html


# ── waitlist: merge rules (unconfirmed entries) ──────────────────────────────

def test_resubmission_keeps_blank_answers_and_never_withdraws_opt_in(db):
    enrolment_service.join_waitlist(email="a@b.co", goal="travel", pace_hours=5, email_opt_in=True)
    enrolment_service.join_waitlist(email="a@b.co", band="asia", email_opt_in=False)
    [row] = db.rows["waitlist_entry"]
    assert row["goal"] == "travel" and row["pace_hours"] == 5 and row["band"] == "asia"
    assert row["email_opt_in"] is True


def test_sweep_deletes_only_expired_unconverted(db):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db.rows["waitlist_entry"] = [
        {"id": "old", "delete_after": past, "converted_student_id": None},
        {"id": "kept", "delete_after": future, "converted_student_id": None},
        {"id": "converted", "delete_after": past, "converted_student_id": "s1"},
    ]
    assert enrolment_service.sweep_waitlist() == 1
    assert {r["id"] for r in db.rows["waitlist_entry"]} == {"kept", "converted"}


def test_conversion_links_by_lowercased_account_email(db):
    db.rows["waitlist_entry"] = [{"id": "w1", "email": "hanna@example.com", "converted_student_id": None}]
    assert enrolment_service.link_waitlist_conversion("s1") is True
    assert db.rows["waitlist_entry"][0]["converted_student_id"] == "s1"


def test_conversion_failure_is_not_raised():
    broken = _DB()
    broken.auth.admin.get_user_by_id = lambda uid: (_ for _ in ()).throw(RuntimeError("auth down"))
    with patch("app.services.enrolment_service.db", broken):
        assert enrolment_service.link_waitlist_conversion("s1") is False


# ── consents ─────────────────────────────────────────────────────────────────

def test_one_row_per_purpose_and_unticked_is_recorded_not_granted(db):
    n = enrolment_service.record_signup_consents("u1", {"model_training": True})
    assert n == 3
    rows = {r["purpose"]: r for r in db.rows["consent_record"]}
    assert set(rows) == set(enrolment_service.CONSENT_PURPOSES)
    assert rows["model_training"]["granted"] is True
    assert rows["session_recording"]["granted"] is False
    assert rows["pronunciation_profile"]["granted"] is False
    assert all(r["source"] == "signup" for r in rows.values())
    assert all(r["policy_version"] == enrolment_service.CONSENT_POLICY_VERSION for r in rows.values())


def test_retried_provision_appends_nothing(db):
    enrolment_service.record_signup_consents("u1", {"session_recording": True})
    assert enrolment_service.record_signup_consents("u1", {"session_recording": False}) == 0
    assert len(db.rows["consent_record"]) == 3


# ── consents through /auth/provision ─────────────────────────────────────────

@pytest.fixture
def headers():
    return {"Authorization": f"Bearer {make_token()}"}


def _provision(client, headers, body, enrol_db):
    from tests.test_provisioning import _DB as ProvDB
    store = {"user_profiles": [], "student": []}
    with patch("app.services.provisioning_service.db", ProvDB(store)), \
         patch("app.services.enrolment_service.db", enrol_db):
        return client.post("/auth/provision", json=body, headers=headers)


def test_provision_records_consents(client, headers, db):
    resp = _provision(client, headers, {
        "age_confirmed": True,
        "consents": {"session_recording": True, "pronunciation_profile": False, "model_training": False},
    }, db)
    assert resp.status_code == 200
    assert resp.json()["consents_recorded"] == 3
    assert {r["subject_id"] for r in db.rows["consent_record"]} == {TEST_USER_ID}


def test_provision_without_consents_records_nothing(client, headers, db):
    resp = _provision(client, headers, {"age_confirmed": True}, db)
    assert resp.status_code == 200
    assert resp.json()["consents_recorded"] == 0
    assert not db.rows.get("consent_record")


def test_unknown_purpose_is_rejected(client, headers, db):
    resp = _provision(client, headers, {"consents": {"marketing": True}}, db)
    assert resp.status_code == 422


def test_consent_failure_does_not_cost_the_account(client, headers):
    resp = _provision(client, headers, {"consents": {"model_training": True}}, _DB(fail=True))
    assert resp.status_code == 200
    assert resp.json()["student_created"] is True
    assert resp.json()["consents_recorded"] == 0


# ── The family waitlist (plan 1.8, L-012), behind the family_waitlist flag ─────

def test_family_entry_refused_until_the_flag_is_on(client):
    with patch("app.core.flags.is_ready", return_value=False):
        resp = client.post("/waitlist", json={"email": "parent@x.co", "child_age_band": "16"})
    assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "family_waitlist_closed"


def test_family_entry_takes_the_age_band_only(client):
    with patch("app.core.flags.is_ready", return_value=True), \
         patch("app.services.enrolment_service.join_waitlist", return_value=None) as join:
        resp = client.post("/waitlist", json={"email": "parent@x.co", "child_age_band": "16",
                                              "child_name": "Abebe"})
    assert resp.status_code == 200
    kwargs = join.call_args.kwargs
    assert kwargs["child_age_band"] == "16" and "child_name" not in kwargs


def test_flags_fail_closed():
    from app.core import flags
    from tests.fakedb import FakeDB
    with patch("app.core.flags.db", FakeDB({"feature_flag": [{"key": "x", "status": "ready"},
                                                              {"key": "y", "status": "coming"}]})):
        assert flags.is_ready("x") is True and flags.is_ready("y") is False and flags.is_ready("z") is False
