"""1:1 add-ons (enrolment plan 2.6, E-15, §129).

Pinned:
1. The pricing page's products; a product without a Stripe price is unavailable.
2. A learner with no tutor must pick a rung >= 2 active tutor; one with a tutor
   doesn't (and a supplied tutor is ignored).
3. Payment grants once (a retried webhook grants nothing) and, for a learner
   with no tutor, creates a guest link (source addon).
4. A 25-minute purchase can't pay for a 50-minute booking.
5. More than 4 1:1s in a week suggests Intensive (not for Intensive learners).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import addon_service as ads
from app.services import hierarchy_service
from app.services.addon_service import AddonRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 2, 3, 12, tzinfo=timezone.utc)   # a Wednesday


@pytest.fixture
def db():
    fake = FakeDB({
        "tutor": [{"id": "t2", "status": "active"}, {"id": "t1", "status": "active"},
                  {"id": "tx", "status": "suspended"}],
        "addon_purchase": [], "session_entitlement": [], "one_to_one_session": [],
    })
    links = {"has": {"t9"}}
    with patch("app.services.addon_service.db", fake), \
         patch.object(hierarchy_service, "linked_tutor_ids", side_effect=lambda s: links.get(s, set())), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: {"t2": 2, "t1": 1, "tx": 3}.get(t)), \
         patch("app.services.makeup_service._names", side_effect=lambda ids: {i: i.upper() for i in ids}), \
         patch("app.services.addon_service.settings.stripe_price_addon_1v1_50", "price_50"), \
         patch("app.services.addon_service.settings.stripe_price_addon_bundle4_25", "price_b25"), \
         patch("app.services.stripe_service.create_addon_checkout",
               AsyncMock(return_value={"checkout_url": "https://stripe/x", "session_id": "cs_1"})):
        yield fake


# 1 + 2
@pytest.mark.asyncio
async def test_options_and_tutor_choice(db):
    o = ads.options("new")
    assert o["needs_tutor"] is True and [t["tutor_id"] for t in o["tutors"]] == ["t2"]
    avail = {p["product"]: p["available"] for p in o["products"]}
    assert avail == {"one_to_one_50": True, "one_to_one_25": False, "bundle4_50": False, "bundle4_25": True}
    assert ads.options("has")["needs_tutor"] is False
    with pytest.raises(AddonRefused) as e:
        await ads.start_checkout("new", "one_to_one_50", None, "a@x")
    assert e.value.reason == "tutor_required"
    with pytest.raises(AddonRefused) as e:
        await ads.start_checkout("new", "one_to_one_50", "t1", "a@x")       # rung 1
    assert e.value.reason == "tutor_not_eligible"
    with pytest.raises(AddonRefused) as e:
        await ads.start_checkout("new", "one_to_one_25", "t2", "a@x")       # no price yet
    assert e.value.reason == "not_configured"
    out = await ads.start_checkout("new", "one_to_one_50", "t2", "a@x")
    assert out["checkout_url"] == "https://stripe/x"
    row = db.rows["addon_purchase"][0]
    assert (row["tutor_id"], row["stripe_session_id"], row["amount_minor"], row.get("status", "pending")) == \
        ("t2", "cs_1", 2600, "pending")
    await ads.start_checkout("has", "bundle4_25", "t2", "b@x")
    assert db.rows["addon_purchase"][1]["tutor_id"] is None           # has a tutor: ignored


# 3
@pytest.mark.asyncio
async def test_activate_grants_once_and_links(db):
    await ads.start_checkout("new", "one_to_one_50", "t2", "a@x")
    pid = db.rows["addon_purchase"][0]["id"]
    db.rows["addon_purchase"][0]["status"] = "pending"
    link = AsyncMock(return_value={"created": True})
    with patch("app.services.matching_service.create_link", link):
        out = await ads.activate(pid)
        again = await ads.activate(pid)
    assert out["granted"] == 1 and again == {"granted": 0, "reason": "already_processed"}
    g = db.rows["session_entitlement"]
    assert len(g) == 1 and (g[0]["reason"], g[0]["reference_type"], g[0]["quantity"]) == ("addon_purchase", "addon_50", 1)
    link.assert_awaited_once_with("new", "t2", "guest", source="addon")


# 4
def test_fifty_minute_units(db):
    db.rows["session_entitlement"] = [
        {"direction": "grant", "quantity": 4, "reason": "addon_purchase", "reference_type": "addon_25", "reference_id": "p1"},
        {"direction": "grant", "quantity": 1, "reason": "addon_purchase", "reference_type": "addon_50", "reference_id": "p2"},
        {"direction": "consume", "quantity": 1, "reason": "booking", "reference_type": "one_to_one_session", "reference_id": "s25"},
    ]
    db.rows["one_to_one_session"] = [{"id": "s25", "duration_minutes": 25}]
    for r in db.rows["session_entitlement"]:
        r.update(student_id="x", kind="one_to_one")
    assert ads.fifty_minute_units("x", 4) == 1          # 3 left are 25-only; 1 can be 50
    db.rows["session_entitlement"].append(
        {"direction": "consume", "quantity": 1, "reason": "booking", "reference_type": "one_to_one_session",
         "reference_id": "s50", "student_id": "x", "kind": "one_to_one"})
    db.rows["one_to_one_session"].append({"id": "s50", "duration_minutes": 50})
    assert ads.fifty_minute_units("x", 3) == 0
    db.rows["session_entitlement"] = [{"direction": "grant", "quantity": 2, "reason": "tier_cycle",
                                       "student_id": "x", "kind": "one_to_one"}]
    assert ads.fifty_minute_units("x", 2) == 2          # no 25-minute purchases


@pytest.mark.asyncio
async def test_booking_refuses_50_on_25_only(db):
    from app.services import booking_service
    db.rows["session_entitlement"] = [
        {"direction": "grant", "quantity": 4, "reason": "addon_purchase", "reference_type": "addon_25",
         "reference_id": "p1", "kind": "one_to_one", "student_id": "x", "occurred_at": "2027-01-01"}]
    with patch("app.services.booking_service.db", db):
        with pytest.raises(booking_service.BookingRefused) as e:
            await booking_service.book("x", "t9", "2027-02-10T10:00:00Z", 50)
    assert e.value.reason == "only_25_minutes"


# 5
def test_suggest_intensive(db):
    db.rows["one_to_one_session"] = [{"student_id": "x", "scheduled_at": f"2027-02-0{d}T10:00:00+00:00",
                                      "status": "scheduled"} for d in (1, 2, 3, 4, 5)]
    assert ads.suggest_intensive("x", "tutor_tier", NOW) is True
    assert ads.suggest_intensive("x", "intensive_tier", NOW) is False
    db.rows["one_to_one_session"][0]["status"] = "cancelled"
    assert ads.suggest_intensive("x", "tutor_tier", NOW) is False
