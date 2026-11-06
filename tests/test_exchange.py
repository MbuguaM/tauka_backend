"""Exchange partners (features addendum A, §142).

Pinned:
1. Off without the flag: states say disabled, joins refused, jobs don't run.
2. Joining: a new account becomes user_type 'partner'; a learner can't; 18+ required.
3. Activation needs conduct, training, a self-rated English level and availability;
   past capacity a partner is waitlisted.
4. Eligibility (X-09): content progress and not Phase 0. Allowance by tier.
5. Matching (A8, §158): open hours in the partner's zone, any band; blocks, gender
   requirements, English fit and rotation; the weekly cap; a decline passes on.
   Bookable times are every free half hour, catalogue times marked as popular.
5b. Chat now (§158): a partner's "free to chat" toggle, count-only for strangers,
   favourites by name; a no-show is settled and replaced.
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
        # §158 open hours, in Addis (no zone set): SAT is Sat 13:00 there; p3 only Wed 02:00
        # (the americas Tue 18:00 New York slot).
        "exchange_partner_hour": [{"user_id": P1, "day_of_week": 5, "hour": 13},
                                  {"user_id": P2, "day_of_week": 5, "hour": 13},
                                  {"user_id": P3, "day_of_week": 2, "hour": 2}],
        "exchange_availability": [],
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


# 2b §157: the country is stored upper-case, validated, and can be added later.
def test_join_country(db):
    st = ex.join_partner("newp", "Selam", True, "Addis Ababa", "et")
    assert st["partner"]["country_code"] == "ET" and st["partner"]["region"] == "Addis Ababa"
    with pytest.raises(ExchangeRefused, match="bad_value"):
        ex.join_partner("other", "X", True, None, "ETH")
    st = ex.set_profile("newp", "standard", None, None, [], region="Bahir Dar", country_code="")
    assert st["partner"]["country_code"] is None and st["partner"]["region"] == "Bahir Dar"
    st = ex.set_profile("newp", "standard", None, None, [], country_code="KE")
    assert st["partner"]["country_code"] == "KE" and st["partner"]["region"] == "Bahir Dar"


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
    # Hours: p3 isn't free then. Ranking: p1 then p2 by id (English fit is moot for an English speaker).
    assert ex.candidates(L, SAT, 30, NOW) == [P1, P2]
    db.rows["user_profiles"][0]["first_language"] = "he"
    assert ex.candidates(L, SAT, 30, NOW)[0] == P2          # A2 partner first for a non-English speaker
    db.rows["user_profiles"][0]["first_language"] = "en"
    db.rows["exchange_block"].append({"blocker_id": P1, "blocked_id": L})
    assert ex.candidates(L, SAT, 30, NOW) == [P2]
    db.rows["exchange_block"] = []
    db.rows["gender_preference"] = [{"user_id": L, "gender": "woman", "same_gender_exchange": "requirement"},
                                    {"user_id": P1, "gender": "woman"}, {"user_id": P2, "gender": "man"}]
    assert ex.candidates(L, SAT, 30, NOW) == [P1]
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
    with patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value="https://x.daily.co/r")) as room,          patch("app.services.calling_service.create_daily_meeting_token",
               new=AsyncMock(side_effect=lambda room, uid, name, exp, audio_only=False: f"tok-{uid}-{audio_only}")) as tok:
        me = await ex.join_call(L, s["id"], now=SAT - timedelta(minutes=5))
        them = await ex.join_call(P1, s["id"], now=SAT)
    props = room.await_args.kwargs["properties"]
    assert props["start_video_off"] and props["privacy"] == "private"       # the URL alone gets nobody in
    assert room.await_count == 1                                    # made once
    # Each person gets their own token; the low-data partner's is audio-only.
    assert me["room_url"] == "https://x.daily.co/r?t=tok-learner-False"
    assert them["room_url"] == "https://x.daily.co/r?t=tok-p1-True"
    assert tok.await_count == 2
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
    assert P1 not in ex.candidates(L, SAT, 30, NOW)
    early = _booked(db, partner=P2, at=NOW + timedelta(days=3))
    assert not ex.cancel(L, early["id"], now=NOW)["late"]


@pytest.mark.asyncio
async def test_no_token_no_call(db):
    """A private room without a token can't be entered: refuse rather than send a dead link."""
    s = _booked(db)
    with patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value="https://x.daily.co/r")),          patch("app.services.calling_service.create_daily_meeting_token", new=AsyncMock(return_value=None)):
        with pytest.raises(ExchangeRefused, match="no_room"):
            await ex.join_call(L, s["id"], now=SAT)


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


# 3b §158: open hours replace slot picks; old clients' slot ids become hours.
def test_hours(db):
    st = ex.set_hours(P1, [[0, 19], [0, 20], [5, 13]], "Europe/London", 5)
    assert st["hours"] == [[0, 19], [0, 20], [5, 13]] and st["timezone"] == "Europe/London" and st["weekly_cap"] == 5
    assert st["availability"] == []                  # sat10 is 10:00 London, not inside those hours
    assert ex.set_hours(P1, [[5, 10]], "Europe/London")["availability"] == ["sat10"]   # for §142 clients
    with pytest.raises(ExchangeRefused, match="bad_timezone"):
        ex.set_hours(P1, [], "Mars/Olympus")
    with pytest.raises(ExchangeRefused, match="bad_value"):
        ex.set_hours(P1, [[7, 0]], "Europe/London")
    with pytest.raises(ExchangeRefused, match="bad_value"):
        ex.set_hours(P1, [], "Europe/London", 20)
    st = ex.set_availability(P2, ["tue18"])          # Tue 18:00 New York = Wed 02:00 Addis
    assert st["hours"] == [[2, 2]]


@pytest.mark.asyncio
async def test_open_hours_matching(db):
    # Any half hour inside marked hours books, not only catalogue times.
    assert ex.candidates(L, SAT + timedelta(minutes=30), 30, NOW) == [P1, P2]   # 13:30-14:00, inside 13:00
    off = SAT + timedelta(hours=1)                   # 14:00 Addis: nobody marked that hour yet
    assert ex.candidates(L, off, 30, NOW) == []
    db.rows["exchange_partner_hour"].append({"user_id": P1, "day_of_week": 5, "hour": 14})
    assert ex.candidates(L, off, 30, NOW) == [P1]
    out = ex.book(L, None, off.isoformat(), 30, now=NOW)
    assert out["status"] == "requested" and db.rows["exchange_session"][0]["slot_id"] is None
    # A popular (catalogue) time records its slot.
    ex.book(L, None, SAT.isoformat(), 30, now=NOW)
    assert db.rows["exchange_session"][1]["slot_id"] == "sat10"
    with pytest.raises(ExchangeRefused, match="bad_time"):
        ex.book(L, None, (SAT + timedelta(minutes=10)).isoformat(), 30, now=NOW)
    # Bookable: every free half hour, the catalogue time marked.
    slots = ex.bookable(L, NOW)["slots"]
    sat = next(x for x in slots if x["at"] == SAT.isoformat())
    assert sat["suggested"] and sat["slot_id"] == "sat10"


def test_weekly_cap(db):
    next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)["weekly_cap"] = 1
    _booked(db, partner=P1, at=SAT - timedelta(days=1), status="booked")     # same week, Friday
    assert ex.candidates(L, SAT, 30, NOW) == [P2]
    # A chat-now session doesn't count toward it.
    db.rows["exchange_session"][0]["kind"] = "instant"
    assert P1 in ex.candidates(L, SAT, 30, NOW)


def test_chat_now(db):
    db.rows["feature_flag"].append({"key": "exchange_practise_now", "status": "ready"})
    assert ex.free_now(L, NOW) == {"enabled": True, "free": 0, "favourites": []}
    out = ex.practise_now(P1, "partner", NOW, minutes=60)
    assert out["status"] == "waiting" and ex.partner_state(P1, NOW)["free_until"]
    with pytest.raises(ExchangeRefused, match="bad_value"):
        ex.practise_now(P2, "partner", NOW, minutes=45)
    assert ex.free_now(L, NOW + timedelta(minutes=30))["free"] == 1          # still free half an hour later
    assert ex.free_now(L, NOW + timedelta(minutes=61))["free"] == 0          # the switch ran out
    # Strangers are a count; a favourite who is free is named.
    db.rows["exchange_favourite"].append({"learner_id": L, "partner_id": P1})
    assert ex.free_now(L, NOW)["favourites"] == [{"id": P1, "name": "Abeba"}]
    # Asking a free favourite pairs at once; asking a stranger is refused.
    with pytest.raises(ExchangeRefused, match="not_a_favourite"):
        ex.practise_now(L, "learner", NOW, partner_id=P2)
    out = ex.practise_now(L, "learner", NOW, partner_id=P1)
    assert out["status"] == "paired"
    s = next(r for r in db.rows["exchange_session"] if r["id"] == out["session_id"])
    assert s["kind"] == "instant" and s["length_min"] == 20 and s["partner_id"] == P1
    assert ex.now_status(P1, NOW)["status"] == "paired"
    # A favourite who isn't free: nothing queued.
    assert ex.practise_now(L, "learner", NOW, partner_id=P1)["status"] == "not_free"


@pytest.mark.asyncio
async def test_no_show_replacement(db):
    db.rows["feature_flag"].append({"key": "exchange_practise_now", "status": "ready"})
    s = _booked(db, at=SAT, learner_joined_at=SAT.isoformat())
    with pytest.raises(ExchangeRefused, match="not_a_no_show"):
        ex.practise_now(L, "learner", SAT + timedelta(minutes=2), replaces=s["id"])   # too early
    ex.practise_now(P2, "partner", SAT, minutes=30)
    out = ex.practise_now(L, "learner", SAT + timedelta(minutes=6), replaces=s["id"])
    assert out["status"] == "paired"
    rows = {r["id"]: r for r in db.rows["exchange_session"]}
    assert rows[s["id"]]["status"] == "missed" and rows[s["id"]]["missed_by"] == P1
    new = rows[out["session_id"]]
    assert new["replaces_id"] == s["id"] and new["partner_id"] == P2
    # The missed one stops counting, the replacement counts: one exchange used.
    assert ex.allowance(L, SAT + timedelta(minutes=6))["used"] == 1
    # A chat-now partner who never joins isn't penalised.
    with patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value=None)):
        await ex.hourly(SAT + timedelta(days=2))
    assert next(r for r in db.rows["exchange_session"] if r["id"] == new["id"])["status"] == "cancelled"


def test_hour_hints(db):
    out = ex.hour_hints("Africa/Addis_Ababa", NOW)
    assert out["timezone"] == "Africa/Addis_Ababa" and len(out["cells"]) == 168
    sat13 = next(c for c in out["cells"] if c["day"] == 5 and c["hour"] == 13)
    europe = next(r for r in sat13["regions"] if r["band"] == "europe")
    assert europe["time"] == "10:00" and europe["likely"] and sat13["popular"] == ["europe"]
    # A zone that isn't one falls back to Addis Ababa rather than failing.
    assert ex.hour_hints("Nowhere/Else", NOW)["timezone"] == "Africa/Addis_Ababa"
