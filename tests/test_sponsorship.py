"""Sponsorship (§144).

Pinned:
1. Applying needs the flag, a real story and consent; one open application per
   learner; consent 'sponsorship_listing' is recorded.
2. Only listed, unheld requests are public, and only the agreed fields.
3. Ops review: list or decline, ops only, pending only.
4. Waitlist sponsoring: checkout holds the request (a second sponsor is refused);
   paid -> sponsored, the seat granted (tier + lessons), learner and sponsor mailed;
   a second webhook is a no-op.
5. Named sponsoring: paid -> a claim link mailed, token stored hashed; claim grants
   once; a wrong token fails.
6. A sponsored learner who sponsors is paying it forward (forward_of).
7. daily(): releases lapsed holds; ends access after the months unless paying,
   and invites the learner to pay it forward.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hierarchy_service
from app.services import sponsorship_service as sp
from app.services.sponsorship_service import SponsorshipRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 3, 1, tzinfo=timezone.utc)
STORY = "My grandmother only speaks Amharic and I want to talk with her before it is too late."


@pytest.fixture
def db():
    fake = FakeDB({
        "feature_flag": [{"key": "sponsorship", "status": "ready"}],
        "sponsorship_request": [], "sponsorship": [], "consent_record": [],
        "student": [{"id": "s1", "subscription_status": None}, {"id": "s2", "subscription_status": None},
                    {"id": "s3", "subscription_status": "active"}],
    }, unique={"sponsorship": [("stripe_session_id",), ("claim_token_hash",)]})
    tiers, mail = AsyncMock(), AsyncMock(return_value={"status": "sent"})
    sessions = iter(range(100))

    def _session(**kw):
        n = next(sessions)
        fake.last_session = kw
        return SimpleNamespace(id=f"cs_{n}", url=f"https://stripe.test/{n}")

    with patch("app.services.sponsorship_service.db", fake), patch("app.core.flags.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch("app.services.tier_service.change_tier", tiers), \
         patch("app.services.email_service.send_template_email", mail), \
         patch.object(sp, "_email_of", side_effect=lambda u: f"{u}@mail.test"), \
         patch.object(sp.settings, "stripe_secret_key", "sk_test"), \
         patch("stripe.checkout.Session.create", side_effect=_session), \
         patch("app.services.lesson_service.grant_gift_months") as grant:
        fake.tiers, fake.mail, fake.grant = tiers, mail, grant
        yield fake


def _apply(student="s1"):
    return sp.apply(student, "Selam", "ke", "heritage", STORY)


def _templates(db):
    return [c.args[1] for c in db.mail.await_args_list]


# 1
def test_apply(db):
    with pytest.raises(SponsorshipRefused):
        sp.apply("s1", "Selam", None, "heritage", "too short")
    r = _apply()
    assert r["status"] == "pending" and r["country_code"] == "KE" and r["tier_wanted"] == "tutor_tier"
    assert db.rows["consent_record"][0]["purpose"] == "sponsorship_listing"
    with pytest.raises(SponsorshipRefused) as e:
        _apply()
    assert e.value.reason == "already_open"
    db.rows["feature_flag"][0]["status"] = "coming"
    with pytest.raises(SponsorshipRefused) as e:
        _apply("s2")
    assert e.value.reason == "not_enabled"


# 2, 3
@pytest.mark.asyncio
async def test_review_and_public_list(db):
    r = _apply()
    assert sp.public_list(NOW) == []
    with pytest.raises(SponsorshipRefused):
        await sp.review("someone", r["id"], "list")
    await sp.review("ops", r["id"], "list")
    listed = sp.public_list(NOW)
    assert len(listed) == 1 and listed[0]["price_minor"] == 7500 * 12
    assert "student_id" not in listed[0] and set(listed[0]) <= set(sp.PUBLIC_FIELDS.split(", ")) | {"price_minor"}
    with pytest.raises(SponsorshipRefused):
        await sp.review("ops", r["id"], "decline")          # no longer pending
    assert "sponsorship_review" in _templates(db)


# 4
@pytest.mark.asyncio
async def test_waitlist_sponsoring(db):
    r = _apply()
    await sp.review("ops", r["id"], "list")
    out = await sp.checkout("waitlist", "Giver@x.test", request_id=r["id"], months=6, sponsor_name="Abeba", now=NOW)
    assert out["checkout_url"].startswith("https://stripe.test/")
    assert db.last_session["metadata"]["sponsorship_id"] and db.last_session["line_items"][0]["price_data"]["unit_amount"] == 45000
    assert sp.public_list(NOW) == []                         # held
    with pytest.raises(SponsorshipRefused) as e:
        await sp.checkout("waitlist", "other@x.test", request_id=r["id"], now=NOW)
    assert e.value.reason == "taken"
    sid = db.rows["sponsorship"][0]["id"]
    seat = await sp.paid(sid, NOW)
    assert seat["status"] == "claimed" and seat["recipient_id"] == "s1"
    assert db.rows["sponsorship_request"][0]["status"] == "sponsored"
    db.tiers.assert_awaited_once_with("s1", "tutor_tier", "gifted")
    db.grant.assert_called_once_with("s1", f"sp:{sid}", "tutor_tier", 6, NOW)
    assert {"sponsorship_sponsored", "sponsorship_receipt"} <= set(_templates(db))
    assert await sp.paid(sid, NOW) is None
    assert sp.mine("s1")["seats"][0]["from"] == "Abeba"


# 5
@pytest.mark.asyncio
async def test_named_sponsoring(db):
    await sp.checkout("named", "giver@x.test", recipient_email="Friend@x.test", recipient_name="Lulit",
                      tier="learner_tier", months=3, anonymous=True, now=NOW)
    sid = db.rows["sponsorship"][0]["id"]
    await sp.paid(sid, NOW)
    claim_mail = next(c for c in db.mail.await_args_list if c.args[1] == "sponsorship_claim")
    assert claim_mail.args[0] == "friend@x.test" and claim_mail.args[2]["sponsor"] == "An anonymous sponsor"
    token = claim_mail.args[2]["claim_url"].split("token=")[1]
    assert db.rows["sponsorship"][0]["claim_token_hash"] == sp._hash(token) != token
    with pytest.raises(SponsorshipRefused):
        await sp.claim("s2", "not-the-token-at-all", NOW)
    out = await sp.claim("s2", token, NOW)
    assert out["tier"] == "learner_tier"
    with pytest.raises(SponsorshipRefused):
        await sp.claim("s3", token, NOW)
    db.tiers.assert_awaited_once_with("s2", "learner_tier", "gifted")


# 6
@pytest.mark.asyncio
async def test_pay_it_forward(db):
    await sp.checkout("named", "g@x.test", recipient_email="s2@mail.test", tier="tutor_tier", months=1, now=NOW)
    first = db.rows["sponsorship"][0]["id"]
    await sp.paid(first, NOW)
    token = next(c for c in db.mail.await_args_list if c.args[1] == "sponsorship_claim").args[2]["claim_url"].split("=")[1]
    await sp.claim("s2", token, NOW)
    r = _apply("s1")
    await sp.review("ops", r["id"], "list")
    await sp.checkout("waitlist", "s2@mail.test", request_id=r["id"], months=3, sponsor_user_id="s2", now=NOW)
    assert db.rows["sponsorship"][1]["forward_of"] == first
    with pytest.raises(SponsorshipRefused):                 # nobody sponsors themselves
        r3 = _apply("s3")
        await sp.review("ops", r3["id"], "list")
        await sp.checkout("waitlist", "s3@mail.test", request_id=r3["id"], sponsor_user_id="s3", now=NOW)


# 7
@pytest.mark.asyncio
async def test_daily(db):
    r = _apply()
    await sp.review("ops", r["id"], "list")
    await sp.checkout("waitlist", "g@x.test", request_id=r["id"], months=3, now=NOW)
    out = await sp.daily(NOW + timedelta(hours=1))
    assert out["released"] == 1 and len(sp.public_list(NOW + timedelta(hours=1))) == 1
    await sp.paid(db.rows["sponsorship"][0]["id"], NOW)
    db.tiers.reset_mock()
    out = await sp.daily(NOW + timedelta(days=91))
    assert out["ended"] == 1 and db.rows["sponsorship"][0]["status"] == "expired"
    db.tiers.assert_awaited_once_with("s1", "free_tier", "self")
    assert "sponsorship_forward" in _templates(db)


def test_constraints_documented():
    """The CHECKs the service relies on exist in the migration."""
    from pathlib import Path
    sql = (Path(__file__).parent.parent / "migrations" / "2026-10-21_s144_sponsorship.sql").read_text(encoding="utf-8")
    for needle in ("sponsorship_request_one_open", "BETWEEN 40 AND 600", "sponsorship_mode_target",
                   "'sponsorship_listing'", "claim_token_hash  text UNIQUE"):
        assert needle in sql


@pytest.mark.parametrize("name", ["sponsorship_review", "sponsorship_sponsored", "sponsorship_claim",
                                  "sponsorship_receipt", "sponsorship_forward"])
def test_emails_render(name):
    from app.services import email_service
    html = email_service._jinja_env.get_template(f"{name}.html").render(
        subject="s", web_base_url="https://tauka.io", listed=True, first_name="Selam", sponsor="Abeba", months=3,
        tier="Tutor", message="Go well", claim_url="https://tauka.io/sponsorship/claim?token=t", who="Selam",
        named=False, forward=True, name="Lulit", reason=None)
    assert "3 month" in html or "Selam" in html
