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
8. Withdrawing erases the story at once and records the consent going; daily()
   erases stories 30 days after a decline or sponsorship and deletes declined or
   withdrawn applications after 12 months, unless a sponsorship points at them.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hierarchy_service
from app.services import sponsorship_service as sp
from app.services.payment_service import Checkout
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
    }, unique={"sponsorship": [("payment_reference",), ("claim_token_hash",)]})
    tiers, mail = AsyncMock(), AsyncMock(return_value={"status": "sent"})

    async def _checkout(kind, row_id, amount_minor, currency, email, description):
        fake.last_checkout = {"kind": kind, "id": row_id, "amount_minor": amount_minor, "currency": currency}
        return Checkout(f"https://pay.test/{row_id}", "paystack", f"{kind}-{row_id}")

    with patch("app.services.sponsorship_service.db", fake), patch("app.core.flags.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch("app.services.tier_service.change_tier", tiers), \
         patch("app.services.email_service.send_template_email", mail), \
         patch.object(sp, "_email_of", side_effect=lambda u: f"{u}@mail.test"), \
         patch("app.services.payment_service.configured", return_value=True), \
         patch("app.services.payment_service.checkout", side_effect=_checkout), \
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


# 8 (§146, L-024)
def test_withdrawing_erases_the_story_and_records_it(db):
    _apply()
    sp.withdraw("s1", NOW)
    r = db.rows["sponsorship_request"][0]
    assert r["status"] == "withdrawn" and r["story"] is None and r["story_erased_at"] == NOW.isoformat()
    consents = db.rows["consent_record"]
    assert [c["granted"] for c in consents] == [True, False] and consents[1]["purpose"] == "sponsorship_listing"


@pytest.mark.asyncio
async def test_stories_and_old_applications_are_removed(db):
    old, recent = (NOW - timedelta(days=31)).isoformat(), (NOW - timedelta(days=29)).isoformat()
    long_ago = (NOW - timedelta(days=366)).isoformat()
    db.rows["sponsorship_request"] += [
        {"id": "keep-recent", "status": "declined", "story": STORY, "reviewed_at": recent, "story_erased_at": None},
        {"id": "keep-listed", "status": "listed", "story": STORY, "listed_at": long_ago, "held_until": None, "story_erased_at": None},
        {"id": "erase-declined", "status": "declined", "story": STORY, "reviewed_at": old, "story_erased_at": None},
        {"id": "erase-sponsored", "status": "sponsored", "story": STORY, "sponsored_at": old, "story_erased_at": None},
        {"id": "delete-me", "status": "withdrawn", "story": None, "story_erased_at": long_ago},
        {"id": "linked", "status": "withdrawn", "story": None, "story_erased_at": long_ago},
        {"id": "not-yet", "status": "withdrawn", "story": None, "story_erased_at": recent},
    ]
    db.rows["sponsorship"].append({"id": "x", "request_id": "linked", "status": "refunded", "mode": "waitlist"})
    out = await sp.daily(NOW)
    rows = {r["id"]: r for r in db.rows["sponsorship_request"]}
    assert (out["stories_erased"], out["requests_deleted"]) == (2, 1)
    assert rows["erase-declined"]["story"] is None and rows["erase-sponsored"]["story"] is None
    assert rows["keep-recent"]["story"] == STORY and rows["keep-listed"]["story"] == STORY
    assert "delete-me" not in rows and {"linked", "not-yet"} <= set(rows)


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
    assert out["checkout_url"].startswith("https://pay.test/")
    assert db.last_checkout["kind"] == "sp" and db.last_checkout["amount_minor"] == 45000 and db.last_checkout["currency"] == "USD"
    assert db.rows["sponsorship"][0]["payment_reference"] == f"sp-{db.rows['sponsorship'][0]['id']}"
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
