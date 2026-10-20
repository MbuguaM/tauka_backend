"""Exchange partners (features addendum A, §142).

Pinned:
1. Off without the flag: states say disabled, joins refused, jobs don't run.
2. Joining: a new account becomes user_type 'partner'; a learner can't; 18+ required.
3. Activation needs conduct, training, a self-rated English level and availability;
   past capacity a partner is waitlisted.
4. Eligibility (X-09): content progress and not Phase 0. Allowance by tier.
5. Matching (A8): the slot in the learner's band, blocks, gender requirements,
   English fit and rotation; a decline passes the request to the next partner.
6. The call: participants only, from 10 minutes before; an information gap shows
   each side its half; the partner's card is at their English level; the timer.
7. Ratings: both -> completed; "didn't come" -> missed; phrases to deck / list.
8. Rewards (A9, X-08): every 4 counting sessions, held without verification or
   while a report is open, at most 2 a month.
9. Safety (A10, X-11): a report goes to safeguarding; two pause a partner; a
   pair's chat hides contact details for four sessions; keyword flags only with
   their own flag; blocks stop matching.
10. Reliability: two late cancellations pause a partner 14 days.
11. Jobs: a lapsed request passes on; unrated sessions settle; inactive partners
    return to the waitlist and the waitlist fills free places.
12. attended() counts completed exchanges (X-10).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import exchange_service as ex
from app.services.exchange_service import ExchangeRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 2, 1, 6, tzinfo=timezone.utc)          # a Monday
SAT = datetime(2027, 2, 6, 10, tzinfo=timezone.utc)         # the slot's next occurrence (London = UTC in Feb)
L, P1, P2, P3 = "learner", "p1", "p2", "p3"


def _partner(uid, **kw):
    row = {"user_id": uid, "status": "active", "conduct_agreed_at": "2027-01-01", "trained_at": "2027-01-01",
           "english_self": "B2", "english_level": "B2", "activated_at": "2027-01-01T00:00:00+00:00",
           "low_data": True, "created_at": "2027-01-01"}
    row.update(kw)
    return row


def _db(flag="ready", **extra):
    rows = {
        "feature_flag": [{"key": "exchange_partners", "status": flag}, {"key": "gender_matching", "status": "ready"}],
        "config": [{"key": "exchange.partner_floor", "value": "30"}],
        "user_profiles": [{"id": L, "first_name": "Lemma", "user_type": "student", "first_language": "en"},
                          {"id": P1, "first_name": "Abeba", "user_type": "partner"},
                          {"id": P2, "first_name": "Bekele", "user_type": "partner"},
                          {"id": P3, "first_name": "Chaltu", "user_type": "partner"}],
        "student": [{"id": L, "tier": "learner_tier"}],
        "enrolment_state": [{"student_id": L, "state": "in_pod", "band": "europe"}],
        "unit": [{"id": "u0", "is_preliminary": True}, {"id": "u1", "is_preliminary": False}],
        "unit_progress": [{"user_id": L, "unit_id": "u1", "status": "completed"}],
        "slot_catalogue": [{"id": "sat10", "band": "europe", "anchor_timezone": "Europe/London", "day_of_week": 5,
                            "start_time": "10:00", "active": True},
                           {"id": "tue18", "band": "americas", "anchor_timezone": "America/New_York",
                            "day_of_week": 1, "start_time": "18:00", "active": True}],
        "exchange_partner": [_partner(P1), _partner(P2, english_level="A2"), _partner(P3)],
        "exchange_availability": [{"user_id": P1, "slot_id": "sat10"}, {"user_id": P2, "slot_id": "sat10"},
                                  {"user_id": P3, "slot_id": "tue18"}],
        "exchange_session": [], "exchange_rating": [], "exchange_reward": [], "exchange_block": [],
        "exchange_favourite": [], "exchange_queue": [], "exchange_phrase": [], "exchange_message_flag": [],
        "peer_block": [], "gender_preference": [], "concern_report": [], "consent_record": [],
        "conversations": [], "conversation_participants": [], "anki_deck": [], "peer_optin": [],
        "peer_task": [
            {"id": "t1", "side": "learner", "lesson_number": 1, "kind": "information_gap", "title": "Market",
             "prompt_a": "You have prices", "prompt_b": "You have items", "corrections": "Gender of verbs",
             "active": True, "created_at": "2027-01-01"},
            {"id": "e1", "side": "partner", "english_level": "B2", "kind": "question_set", "title": "Advice",
             "prompt_a": "Ask for advice", "active": True},
            {"id": "e2", "side": "partner", "english_level": "A2", "kind": "question_set", "title": "Family",
             "prompt_a": "Describe family", "active": True}],
    }
    rows.update(extra)
    return FakeDB(rows, unique={"exchange_rating": [("session_id", "rater_id")],
                                "exchange_block": [("blocker_id", "blocked_id")],
                                "exchange_favourite": [("learner_id", "partner_id")],
                                "exchange_queue": [("user_id",)], "exchange_partner": [("user_id",)]})


@pytest.fixture
def db():
    fake = _db()
    with patch("app.services.exchange_service.db", fake), patch("app.core.flags.db", fake), \
         patch("app.services.gender_service.db", fake), \
         patch("app.services.exchange_service._lesson", return_value=1), \
         patch("app.services.exchange_service._notify", new=AsyncMock()):
        yield fake


def _booked(db, partner=P1, at=SAT, **kw):
    row = {"id": f"s{len(db.rows['exchange_session']) + 1}", "learner_id": L, "partner_id": partner, "kind": "scheduled",
           "slot_id": "sat10", "scheduled_at": at.isoformat(), "length_min": 30, "status": "booked",
           "lesson_number": 1, "connect_failures": 0}
    row.update(kw)
    db.rows["exchange_session"].append(row)
    return row


# 1
def test_off_without_flag():
    fake = _db("coming")
    with patch("app.services.exchange_service.db", fake), patch("app.core.flags.db", fake):
        assert ex.learner_state(L) == {"enabled": False}
        assert ex.partner_state(P1) == {"enabled": False}
        with pytest.raises(ExchangeRefused):
            ex.join_partner("new", "N", True, None)
        assert ex.daily(NOW) == {"enabled": False}


# 2
def test_join(db):
    st = ex.join_partner("newp", "Selam", True, "Addis Ababa")
    assert st["partner"]["status"] == "onboarding"
    prof = next(p for p in db.rows["user_profiles"] if p["id"] == "newp")
    assert prof["user_type"] == "partner" and prof["age_confirmed_at"]
    with pytest.raises(ExchangeRefused, match="learner_cannot_partner"):
        ex.join_partner(L, "Lemma", True, None)
    with pytest.raises(ExchangeRefused, match="age_required"):
        ex.join_partner("other", "X", False, None)


# 3
def test_activation_and_waitlist(db):
    ex.join_partner("newp", "Selam", True, None)
    with pytest.raises(ExchangeRefused, match="incomplete"):
        ex.activate("newp", NOW)
    ex.set_profile("newp", "standard", None, "B1", ["music"])
    ex.set_availability("newp", ["sat10"])
    ex.agree_conduct("newp")
    out = ex.finish_training("newp", {"halves": 0})
    assert out["training_wrong"]
    ex.finish_training("newp", {q["id"]: q["answer"] for q in ex.TRAINING})
    assert ex.activate("newp", NOW)["partner"]["status"] == "active"
    assert any(c["purpose"] == "exchange_conduct" for c in db.rows["consent_record"])
    # Capacity of 4 (floor) is now full: the next partner waits.
    db.rows["config"] = [{"key": "exchange.partner_floor", "value": "4"}]
    ex.join_partner("late", "Late", True, None)
    ex.set_profile("late", "standard", None, "B1", [])
    ex.set_availability("late", ["sat10"])
    ex.agree_conduct("late")
    ex.finish_training("late", {q["id"]: q["answer"] for q in ex.TRAINING})
    st = ex.activate("late", NOW)
    assert st["partner"]["status"] == "waitlisted" and st["waitlist"]["position"] == 1


# 4
def test_eligibility_and_allowance(db):
    assert ex.eligible(L)
    db.rows["enrolment_state"][0]["state"] = "phase0"
    assert not ex.eligible(L)
    db.rows["enrolment_state"][0]["state"] = "in_pod"
    db.rows["unit_progress"] = [{"user_id": L, "unit_id": "u0", "status": "completed"}]
    assert not ex.eligible(L)                         # only a preliminary lesson
    db.rows["unit_progress"] = [{"user_id": L, "unit_id": "u1", "status": "in_progress"}]
    assert ex.allowance(L, NOW) == {"period": "month", "limit": 4, "used": 0, "left": 4}
    db.rows["student"][0]["tier"] = "intensive_tier"
    assert ex.allowance(L, NOW)["period"] == "week" and ex.allowance(L, NOW)["limit"] == 5


# 5
@pytest.mark.asyncio
async def test_matching_and_decline_passes_on(db):
    # Band: p3 offers only an americas slot. Ranking: p1 then p2 by id (English fit is moot for an English speaker).
    assert ex.candidates(L, "sat10", SAT, 30, NOW) == [P1, P2]
    db.rows["user_profiles"][0]["first_language"] = "he"
    assert ex.candidates(L, "sat10", SAT, 30, NOW)[0] == P2          # A2 partner first for a non-English speaker
    db.rows["user_profiles"][0]["first_language"] = "en"
    db.rows["exchange_block"].append({"blocker_id": P1, "blocked_id": L})
    assert ex.candidates(L, "sat10", SAT, 30, NOW) == [P2]
    db.rows["exchange_block"] = []
    db.rows["gender_preference"] = [{"user_id": L, "gender": "woman", "same_gender_exchange": "requirement"},
                                    {"user_id": P1, "gender": "woman"}, {"user_id": P2, "gender": "man"}]
    assert ex.candidates(L, "sat10", SAT, 30, NOW) == [P1]
    db.rows["gender_preference"] = []
    out = ex.book(L, "sat10", SAT.isoformat(), 30, now=NOW)
    first = db.rows["exchange_session"][0]
    assert out["status"] == "requested" and first["partner_id"] == P1
    res = await ex.respond(P1, first["id"], False, now=NOW)
    assert res["passed_on"] and db.rows["exchange_session"][1]["partner_id"] == P2
    res = await ex.respond(P2, db.rows["exchange_session"][1]["id"], True, now=NOW)
    assert res["status"] == "booked" and db.rows["conversations"][0]["type"] == "exchange"
    assert ex.allowance(L, NOW)["used"] == 1                          # the declined one doesn't count


def test_booking_refusals(db):
    with pytest.raises(ExchangeRefused, match="bad_time"):
        ex.book(L, "sat10", (NOW + timedelta(minutes=30)).isoformat(), 30, now=NOW)
    with pytest.raises(ExchangeRefused, match="bad_length"):
        ex.book(L, "sat10", SAT.isoformat(), 25, now=NOW)
    db.rows["student"][0]["tier"] = "free_tier"
    for i in range(2):
        _booked(db, at=SAT + timedelta(days=i))
    with pytest.raises(ExchangeRefused, match="allowance_used"):
        ex.book(L, "sat10", SAT.isoformat(), 30, now=NOW)


# 6
@pytest.mark.asyncio
async def test_join_cards_and_plan(db):
    s = _booked(db)
    with pytest.raises(ExchangeRefused, match="not_now"):
        await ex.join_call(L, s["id"], now=SAT - timedelta(minutes=20))
    with pytest.raises(ExchangeRefused, match="not_your_session"):
        await ex.join_call(P2, s["id"], now=SAT)
    with patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value="https://x.daily.co/r")) as room:
        me = await ex.join_call(L, s["id"], now=SAT - timedelta(minutes=5))
        them = await ex.join_call(P1, s["id"], now=SAT)
    assert room.await_args.kwargs["properties"] == {"start_video_off": True}
    assert room.await_count == 1                                    # made once
    assert me["cards"]["amharic"]["prompt"] == "You have prices"
    assert them["cards"]["amharic"]["prompt"] == "You have items"
    assert them["cards"]["amharic"]["prompt_extra"] == "Correct one thing kindly."
    assert them["cards"]["english"]["title"] == "Advice"            # the B2 partner's card
    assert [p["minutes"] for p in me["plan"]] == [1, 14, 14, 1]
    assert them["low_data"] and not me["low_data"]
    assert ex.plan(20) == [{"step": "hello", "minutes": 1}, {"step": "amharic", "minutes": 9},
                           {"step": "english", "minutes": 9}, {"step": "wrap_up", "minutes": 1}]


# 7
@pytest.mark.asyncio
async def test_ratings(db):
    s = _booked(db)
    after = SAT + timedelta(minutes=35)
    await ex.rate(L, s["id"], 5, True, True, phrase="ጤና ይስጥልኝ", meaning="Hello", now=after)
    assert db.rows["anki_deck"][0]["title"] == "Exchange phrases"
    assert db.rows["anki_deck"][0]["deck"]["cards"][0]["reveal"] == "ጤና ይስጥልኝ"
    out = await ex.rate(P1, s["id"], 4, True, True, phrase="hold up", now=after)
    assert out["status"] == "completed" and db.rows["exchange_phrase"][0]["phrase"] == "hold up"
    with pytest.raises(ExchangeRefused, match="already_rated"):
        await ex.rate(L, s["id"], 5, True, now=after)
    s2 = _booked(db, at=SAT + timedelta(days=7))
    out = await ex.rate(L, s2["id"], 1, False, other_came=False, now=SAT + timedelta(days=7, minutes=40))
    assert out["status"] == "missed" and db.rows["exchange_session"][1]["missed_by"] == P1


# 8
def test_rewards(db):
    for i in range(9):
        _booked(db, at=SAT - timedelta(days=30 - i), status="completed", actual_duration_s=1700)
        db.rows["exchange_rating"].append({"session_id": db.rows["exchange_session"][-1]["id"], "rater_id": L,
                                           "helpfulness": 4})
    made = ex.rewards_due(P1, NOW)
    assert len(made) == 2 and all(r["status"] == "held" and r["held_reason"] == "verification" for r in made)
    assert ex.rewards_due(P1, NOW) == []                              # capped at 2 this month, 1 left over
    with patch("app.services.hierarchy_service.admin_can", return_value=True):
        ex.verify("lead", P1, True)
    assert {r["status"] for r in db.rows["exchange_reward"]} == {"due"}
    # A short session doesn't count; a learner rating under 3 doesn't either.
    db.rows["exchange_session"][0]["actual_duration_s"] = 600
    assert len(ex._reward_ready_sessions(P1)) == 8


# 9
def test_report_and_pause(db):
    s = _booked(db)
    ex.report(L, s["id"], "contact_details", "asked for my number")
    r = db.rows["concern_report"][0]
    assert r["about_kind"] == "partner" and r["about_user_id"] == P1 and r["session_type"] == "exchange_session"
    assert next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)["status"] == "active"
    ex.report(L, s["id"], "harassment", None)
    assert next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)["status"] == "paused"


def test_chat_filter(db):
    db.rows["conversations"] = [{"id": "c1", "type": "exchange"}, {"id": "c2", "type": "student_to_student"}]
    db.rows["conversation_participants"] = [{"conversation_id": "c1", "user_id": L},
                                            {"conversation_id": "c1", "user_id": P1}]
    text, info = ex.filter_message("c1", P1, "call me +251 911 234 567 or abeba@mail.com, @abeba_x")
    assert info["masked"] and "911" not in text and "@mail" not in text and "abeba_x" not in text
    assert ex.filter_message("c2", P1, "+251 911 234 567")[0] == "+251 911 234 567"   # not an exchange chat
    assert ex.filter_message("c1", P1, "send me money")[1]["flags"] == []              # flags are off
    db.rows["feature_flag"].append({"key": "exchange_message_flags", "status": "ready"})
    assert ex.filter_message("c1", P1, "send me money, my love")[1]["flags"] == ["money", "romance"]
    for i in range(4):
        _booked(db, at=SAT - timedelta(days=10 + i), status="completed")
    assert ex.filter_message("c1", P1, "+251 911 234 567")[0] == "+251 911 234 567"   # after four sessions


# 10
def test_late_cancellations_pause(db):
    a = _booked(db, at=NOW + timedelta(hours=5))
    b = _booked(db, at=NOW + timedelta(hours=8))
    assert ex.cancel(P1, a["id"], now=NOW)["late"]
    ex.cancel(P1, b["id"], now=NOW)
    p = next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)
    assert p["paused_until"]
    assert P1 not in ex.candidates(L, "sat10", SAT, 30, NOW)
    early = _booked(db, partner=P2, at=NOW + timedelta(days=3))
    assert not ex.cancel(L, early["id"], now=NOW)["late"]


@pytest.mark.asyncio
async def test_connection_failures(db):
    s = _booked(db)
    assert (await ex.connect_failed(L, s["id"], now=SAT))["tries"] == 1
    out = await ex.connect_failed(P1, s["id"], now=SAT)
    assert out == {"status": "cancelled", "reschedule": True}
    assert not db.rows["exchange_session"][0]["late_cancel"]


# 11
@pytest.mark.asyncio
async def test_hourly(db):
    req = _booked(db, status="requested", respond_by=(NOW - timedelta(minutes=1)).isoformat())
    gone = _booked(db, at=NOW - timedelta(days=2), learner_joined_at="x")            # partner never came
    both = _booked(db, partner=P2, at=NOW - timedelta(days=3), learner_joined_at="x", partner_joined_at="y")
    with patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value=None)):
        out = await ex.hourly(NOW)
    assert out["expired"] == 1 and out["settled"] == 2
    rows = {r["id"]: r for r in db.rows["exchange_session"]}
    assert rows[req["id"]]["status"] == "expired"
    assert any(r["partner_id"] == P2 and r["status"] == "requested" for r in db.rows["exchange_session"])
    assert rows[gone["id"]]["status"] == "missed" and rows[gone["id"]]["missed_by"] == P1
    assert rows[both["id"]]["status"] == "completed"


def test_daily_waitlist(db):
    db.rows["config"] = [{"key": "exchange.partner_floor", "value": "3"}]
    db.rows["exchange_partner"].append(_partner("w1", status="waitlisted", waitlisted_at="2027-01-20"))
    db.rows["exchange_partner"][0]["activated_at"] = (NOW - timedelta(days=40)).isoformat()   # p1 inactive
    out = ex.daily(NOW)
    st = {p["user_id"]: p["status"] for p in db.rows["exchange_partner"]}
    assert out["to_waitlist"] >= 1 and st[P1] == "waitlisted" and st["w1"] == "active"


# 12
def test_attended(db):
    _booked(db, at=NOW - timedelta(days=3), status="completed")
    _booked(db, at=NOW - timedelta(days=4), status="missed")
    assert ex.attended(L, NOW - timedelta(days=30)) == 1


def test_mask_contacts():
    assert ex.mask_contacts("my wa.me/251911 please")[1]
    assert not ex.mask_contacts("I studied for 3 hours on 12 May")[1]
