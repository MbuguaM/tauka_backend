"""Institutions, community partners, gift cards (Track 10, §139).

Pinned:
1. Institutions: ops only and behind the flag; a contract's code joins staff to
   its tier within its dates and seats; the employer report lists ONLY members
   who granted employer_reporting (latest record wins).
2. Partners: attributed once at provision; the statement is the partner's share
   of those learners' payments in the period, for 12 months, per currency.
3. Gift cards: paid -> a code (once); redeem gives the tier and the lessons;
   a second redemption fails; after the months, access ends unless paying.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import commerce_service as cs
from app.services import hierarchy_service
from app.services.commerce_service import CommerceRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 3, 1, tzinfo=timezone.utc)


@pytest.fixture
def db():
    fake = FakeDB({
        "feature_flag": [{"key": "institutions", "status": "ready"}, {"key": "gift_cards", "status": "ready"}],
        "organization": [], "org_contract": [], "org_member": [], "org_invoice": [], "consent_record": [],
        "partner": [], "partner_signup": [], "student_payment": [], "gift_card": [], "cohort_session_attendance": [],
        "student": [{"id": "s1", "subscription_status": None}, {"id": "s2", "subscription_status": "active"}],
    }, unique={"partner_signup": [("student_id",)], "gift_card": [("code",)]})
    tiers = AsyncMock()
    with patch("app.services.commerce_service.db", fake), patch("app.core.flags.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"), \
         patch("app.services.tier_service.change_tier", tiers), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: f"Name {i}" for i in ids}), \
         patch("app.services.tracker_service.for_student", side_effect=lambda s: {"lessons_done": 7, "status": "on_track"}):
        fake.tiers = tiers
        yield fake


# 1
@pytest.mark.asyncio
async def test_institution(db):
    with pytest.raises(CommerceRefused):
        cs.create_org("someone", "Embassy", "hr@emb.example")
    org = cs.create_org("ops", "Embassy", "HR@emb.example", "ke")
    c = cs.create_contract("ops", org["id"], "tutor_tier", 2, date(2027, 1, 1), date(2027, 12, 31), 150000, "usd")
    assert c["status"] == "draft" and len(c["join_code"]) == 8
    with patch.object(cs.settings, "stripe_secret_key", ""):
        await cs.activate_contract("ops", c["id"])
    assert db.rows["org_contract"][0]["status"] == "active" and db.rows["org_invoice"][0]["amount_minor"] == 150000
    assert (await cs.join("s1", c["join_code"].lower(), date(2027, 3, 1)))["tier"] == "tutor_tier"
    db.tiers.assert_awaited_with("s1", "tutor_tier", "admin")
    await cs.join("s2", c["join_code"], date(2027, 3, 1))
    with pytest.raises(CommerceRefused) as e:
        await cs.join("s3", c["join_code"], date(2027, 3, 1))
    assert e.value.reason == "no_seats"
    with pytest.raises(CommerceRefused):
        await cs.join("s1", c["join_code"], date(2028, 1, 5))           # contract over
    db.rows["consent_record"] = [
        {"subject_id": "s1", "purpose": "employer_reporting", "granted": True, "at": "2027-02-01"},
        {"subject_id": "s2", "purpose": "employer_reporting", "granted": True, "at": "2027-02-01"},
        {"subject_id": "s2", "purpose": "employer_reporting", "granted": False, "at": "2027-02-10"}]
    rep = cs.employer_report("ops", c["id"])
    assert cs.set_report_consent("s3", True) == {"employer_reporting": True}
    assert (rep["members"], rep["consented"]) == (2, 1) and rep["rows"][0]["name"] == "Name s1"


# 2
def test_partners(db):
    p = cs.create_partner("ops", "Habesha Society", "HABSOC", "a@x", 1000)
    assert cs.attribute("s1", "HABSOC") is True and cs.attribute("s1", "HABSOC") is False
    assert cs.attribute("s2", "NOPE") is False
    db.rows["partner_signup"][0]["signed_up_at"] = "2026-06-01T00:00:00+00:00"
    db.rows["student_payment"] = [
        {"student_id": "s1", "amount_minor": 7500, "currency": "USD", "paid_at": "2027-02-05T00:00:00+00:00"},
        {"student_id": "s1", "amount_minor": 970000, "currency": "KES", "paid_at": "2027-02-06T00:00:00+00:00"},
        {"student_id": "s1", "amount_minor": 7500, "currency": "USD", "paid_at": "2027-07-05T00:00:00+00:00"}]
    st = cs.statement("ops", p["id"], date(2027, 2, 1), date(2027, 2, 28))
    assert st["commission"] == {"USD": 750, "KES": 97000}
    assert cs.statement("ops", p["id"], date(2027, 7, 1), date(2027, 7, 31))["commission"] == {}   # past 12 months


# 3
@pytest.mark.asyncio
async def test_gift_cards(db):
    db.rows["gift_card"].append({"id": "g1", "tier": "tutor_tier", "months": 2, "amount_minor": 15000,
                                 "purchaser_email": "buyer@x", "status": "pending", "created_at": NOW.isoformat()})
    mail = AsyncMock(return_value={"status": "sent"})
    with patch("app.services.email_service.send_template_email", mail):
        card = await cs.card_paid("g1")
        assert await cs.card_paid("g1") is None
    assert card["status"] == "paid" and len(card["code"]) == 12 and mail.await_args.args[2]["code"] == card["code"]
    with patch("app.services.lesson_service.grant_gift_months") as grant:
        out = await cs.redeem("s1", card["code"].lower(), NOW)
        with pytest.raises(CommerceRefused):
            await cs.redeem("s2", card["code"], NOW)
    assert out["tier"] == "tutor_tier"
    grant.assert_called_once_with("s1", "gc:g1", "tutor_tier", 2, NOW)
    db.tiers.reset_mock()
    await cs.daily(NOW + timedelta(days=61))
    db.tiers.assert_awaited_once_with("s1", "free_tier", "self")
    assert db.rows["gift_card"][0]["status"] == "expired"


def test_gift_card_email_renders():
    from app.services import email_service
    html = email_service._jinja_env.get_template("gift_card.html").render(
        subject="s", code="ABCDEFGHJKLM", months=2, tier="Tutor", web_base_url="https://tauka.io")
    assert "ABCDEFGHJKLM" in html and "2 months" in html
