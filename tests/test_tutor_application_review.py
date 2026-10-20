"""Tutor application review and approval (enrolment plan §2j J3; CONFLICT-057).

Pinned:
1. Only ops reviews; the detail shows whether the email has an account and
   whether a subscription is live.
2. Reject needs a code, sets the 12-month delete date, emails the code's
   wording (never the admin note), and is final.
3. Approve with no account invites (redirect to /set-password), creates a
   tutor profile and an ONBOARDING app.tutor row; with a learner account it
   promotes the same account (J1) unless a subscription is live; an admin
   account is refused; approval is idempotent.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import email_service, hierarchy_service
from app.services import tutor_application_service as apps
from app.services.tutor_application_service import ApplicationRefused

NOW = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            rows.append(dict(p))
            return SimpleNamespace(data=[p])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.rows = {
            "tutor_application": [
                {"id": "new", "email": "new@x.test", "full_name": "Hana Bekele", "status": "submitted",
                 "created_at": "2027-01-01", "withdraw_token_hash": "h", "admin_note": None},
                {"id": "learner", "email": "learner@x.test", "full_name": "Abel G", "status": "under_review",
                 "created_at": "2027-01-02", "admin_note": None},
                {"id": "payer", "email": "payer@x.test", "full_name": "Sara T", "status": "submitted",
                 "created_at": "2027-01-03", "admin_note": None},
                {"id": "boss", "email": "boss@x.test", "full_name": "Admin A", "status": "submitted",
                 "created_at": "2027-01-04", "admin_note": None},
            ],
            "user_profiles": [{"id": "u-learner", "user_type": "student"}, {"id": "u-payer", "user_type": "student"},
                              {"id": "u-boss", "user_type": "student"}],
            "student": [{"id": "u-learner", "tier": "free_tier", "subscription_status": "cancelled"},
                        {"id": "u-payer", "tier": "tutor_tier", "subscription_status": "active"}],
            "admin_users": [{"id": "u-boss"}],
            "tutor": [],
        }
        self.accounts = {"learner@x.test": "u-learner", "payer@x.test": "u-payer", "boss@x.test": "u-boss"}
        self.auth = SimpleNamespace(admin=MagicMock())
        self.auth.admin.invite_user_by_email.return_value = SimpleNamespace(user=SimpleNamespace(id="u-new"))

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)

    def rpc(self, name, args):
        assert name == "user_id_by_email"
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.accounts.get(args["p_email"])))


@pytest.fixture
def db():
    fake = _DB()
    mail = AsyncMock(return_value={"status": "sent"})
    with patch("app.services.tutor_application_service.db", fake), \
         patch("app.services.tutor_application_service._now", return_value=NOW), \
         patch.object(email_service, "send_template_email", mail), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops" and c == "ops"):
        fake.mail = mail
        yield fake


# 1
def test_ops_only_and_detail_shows_the_account(db):
    with pytest.raises(ApplicationRefused):
        apps.admin_list("stranger")
    assert [a["id"] for a in apps.admin_list("ops")] == ["boss", "payer", "learner", "new"]
    assert apps.admin_list("ops", "under_review")[0]["id"] == "learner"
    d = apps.admin_detail("ops", "payer")
    assert d["account"] == {"exists": True, "user_id": "u-payer", "user_type": "student", "live_subscription": True}
    assert "withdraw_token_hash" not in apps.admin_detail("ops", "new")
    assert apps.admin_detail("ops", "new")["account"] == {"exists": False}
    apps.mark_under_review("ops", "new")
    with pytest.raises(ApplicationRefused):
        apps.mark_under_review("ops", "new")


# 2
@pytest.mark.asyncio
async def test_reject(db):
    with pytest.raises(ApplicationRefused):
        await apps.reject("ops", "new", "bad code")
    row = await apps.reject("ops", "new", "capacity", "Strong, but no seats in EA")
    assert row["status"] == "rejected" and row["delete_after"] and row["withdraw_token_hash"] is None
    args = db.mail.await_args
    assert args.args[0] == "new@x.test" and args.args[2]["code"] == "capacity"
    assert "Strong" not in str(args.args[2])                    # the note stays internal
    with pytest.raises(ApplicationRefused) as e:
        await apps.reject("ops", "new", "other")
    assert e.value.reason == "decided"


# 3
@pytest.mark.asyncio
async def test_approve_invites_a_new_person(db):
    row = await apps.approve("ops", "new")
    assert row["status"] == "approved" and row["tutor_id"] == "u-new"
    email, opts = db.auth.admin.invite_user_by_email.call_args.args
    assert email == "new@x.test" and opts["redirect_to"].endswith("/set-password")
    assert {"id": "u-new", "user_type": "tutor", "first_name": "Hana"} in db.rows["user_profiles"]
    assert db.rows["tutor"] == [{"id": "u-new", "status": "onboarding"}]
    assert db.mail.await_args.args[2]["invited"] is True
    again = await apps.approve("ops", "new")                    # idempotent
    assert again["status"] == "approved" and db.auth.admin.invite_user_by_email.call_count == 1


@pytest.mark.asyncio
async def test_approve_promotes_a_learner_but_not_while_paying_and_never_an_admin(db):
    row = await apps.approve("ops", "learner")
    assert row["tutor_id"] == "u-learner"
    assert next(p for p in db.rows["user_profiles"] if p["id"] == "u-learner")["user_type"] == "tutor"
    assert {"id": "u-learner", "status": "onboarding"} in db.rows["tutor"]
    assert db.auth.admin.invite_user_by_email.call_count == 0
    with pytest.raises(ApplicationRefused) as e:
        await apps.approve("ops", "payer")
    assert e.value.reason == "live_subscription"
    with pytest.raises(ApplicationRefused) as e:
        await apps.approve("ops", "boss")
    assert e.value.reason == "admin_account"
    with pytest.raises(ApplicationRefused):
        await apps.approve("stranger", "payer")


def test_decision_email_renders():
    html = email_service._jinja_env.get_template("tutor_application_decision.html").render(
        subject="s", kind="rejected", code="capacity", name="<b>Hana</b>", web_base_url="https://tauka.io")
    assert "next intake" in html and "<b>Hana</b>" not in html
    html = email_service._jinja_env.get_template("tutor_application_decision.html").render(
        subject="s", kind="approved", invited=True, name="Hana", web_base_url="https://tauka.io")
    assert "set your password" in html and "onboarding" in html
