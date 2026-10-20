"""Peer sessions (enrolment plan Track 5, §135).

Pinned:
1. Off without the flag: mine says disabled, opt-in refused, pairing doesn't run.
2. Pairing: across pods only, same season/level/band, a shared slot not
   clashing with either pod, never blocked or last fortnight's pair; a task at
   or below the pair's lesson.
3. Join: only participants, from 10 min before; video off; an information gap
   shows each side its own half.
4. Marking: once each; two "didn't come" in 30 days pauses the absentee 14 days;
   both marked -> completed.
5. Report goes to safeguarding as a concern about the partner; block stops pairing.
6. Practise now: pilot band only; the second waiting learner is paired at once.
7. attended() counts sessions the partner confirmed (for the guarantee).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hierarchy_service
from app.services import peer_service as ps
from app.services.peer_service import PeerRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 2, 1, 6, tzinfo=timezone.utc)          # a Monday


def _db(flag="ready"):
    learners = ["a", "b", "c", "d"]
    pods = {"a": "p1", "b": "p2", "c": "p1", "d": "p3"}
    return FakeDB({
        "feature_flag": [{"key": "peer_sessions", "status": flag}],
        "peer_optin": [{"student_id": s, "opted_in": True} for s in learners],
        "cohort_memberships": [{"user_id": s, "cohort_id": p, "status": "active"} for s, p in pods.items()],
        "cohorts": [{"id": p, "status": "active", "start_date": "2027-01-09", "level": 1} for p in ("p1", "p2", "p3")],
        "intake": [{"formation_date": "2027-01-09", "season_id": "bega"}],
        "student": [{"id": s, "assessed_level": "A1"} for s in learners],
        "enrolment_state": [{"student_id": s, "band": "ea_gulf"} for s in learners],
        "student_slot_choice": [{"student_id": s, "slot_id": "sat10"} for s in learners],
        "slot_catalogue": [{"id": "sat10", "band": "ea_gulf", "anchor_timezone": "Africa/Nairobi", "day_of_week": 5,
                            "start_time": "10:00", "active": True}],
        "cohort_session": [], "peer_session": [], "peer_session_participant": [], "peer_block": [],
        "peer_queue": [], "payment_grace": [], "gender_preference": [], "concern_report": [], "config": [],
        "peer_task": [
            {"id": "t0", "lesson_number": 1, "kind": "question_set", "title": "Greetings", "prompt_a": "Ask", "active": True},
            {"id": "t5", "lesson_number": 5, "kind": "information_gap", "title": "Market", "prompt_a": "You have prices",
             "prompt_b": "You have items", "active": True},
            {"id": "t9", "lesson_number": 9, "kind": "role_play", "title": "Doctor", "prompt_a": "Play", "active": True}],
    }, unique={"peer_optin": [("student_id",)], "peer_block": [("blocker_id", "blocked_id")],
               "peer_queue": [("student_id",)]})


@pytest.fixture
def db():
    fake = _db()
    with patch("app.services.peer_service.db", fake), patch("app.core.flags.db", fake), \
         patch("app.services.gender_service.db", fake), patch("app.services.grace_service.db", fake), \
         patch("app.services.peer_service._lessons", side_effect=lambda ids: {i: 6 for i in ids}):
        yield fake


# 1
def test_off_without_flag():
    fake = _db("coming")
    with patch("app.services.peer_service.db", fake), patch("app.core.flags.db", fake):
        assert ps.mine("a") == {"enabled": False}
        with pytest.raises(PeerRefused):
            ps.set_optin("a", True)
        assert ps.pair_week(NOW) == {"paired": 0, "enabled": False}


# 2
def test_pairing(db):
    out = ps.pair_week(NOW)
    pairs = {}
    for p in db.rows["peer_session_participant"]:
        pairs.setdefault(p["session_id"], set()).add(p["student_id"])
    assert sorted(sorted(v) for v in pairs.values()) == [["a", "b"], ["c", "d"]]       # never p1 with p1
    assert out == {"paired": 2, "unpaired": 0, "enabled": True}
    s = db.rows["peer_session"][0]
    assert (s["task_id"], s["minutes"], s["season_id"]) == ("t5", 20, "bega")         # lesson 6 -> the lesson-5 card
    assert s["starts_at"].startswith("2027-02-06T07:00")                                # Sat 10:00 Nairobi
    # Next week: not the same pairs again, and a block is respected.
    db.rows["peer_block"].append({"blocker_id": "a", "blocked_id": "d"})
    later = NOW + timedelta(days=7)
    db.rows["peer_session"][0]["starts_at"] = (later - timedelta(days=1)).isoformat()
    db.rows["peer_session"][1]["starts_at"] = (later - timedelta(days=1)).isoformat()
    # a-b were paired last week, a-d is blocked, a-c share a pod: a waits; b-c is new.
    assert ps.pair_week(later)["paired"] == 1
    newest = db.rows["peer_session"][-1]["id"]
    assert {p["student_id"] for p in db.rows["peer_session_participant"] if p["session_id"] == newest} == {"b", "c"}


def test_pod_clash_skips(db):
    db.rows["cohort_session"].append({"cohort_id": "p2", "status": "scheduled", "scheduled_at": "2027-02-06T07:30:00+00:00"})
    ps.pair_week(NOW)
    pairs = {frozenset(x["student_id"] for x in db.rows["peer_session_participant"] if x["session_id"] == s["id"])
             for s in db.rows["peer_session"]}
    assert frozenset({"a", "b"}) not in pairs


# 3
@pytest.mark.asyncio
async def test_join_halves(db):
    ps.pair_week(NOW)
    sid = db.rows["peer_session"][0]["id"]
    room = AsyncMock(return_value="https://tauka.daily.co/peer-x")
    with patch("app.services.calling_service.create_daily_room", room):
        with pytest.raises(PeerRefused) as e:
            await ps.join("a", sid, NOW)
        assert e.value.reason == "not_now"
        at = datetime(2027, 2, 6, 6, 55, tzinfo=timezone.utc)
        with pytest.raises(PeerRefused):
            await ps.join("c", sid, at)
        ja, jb = await ps.join("a", sid, at), await ps.join("b", sid, at)
    assert ja["task"]["prompt"] == "You have prices" and jb["task"]["prompt"] == "You have items"
    assert room.await_args.kwargs["properties"] == {"start_video_off": True} and room.await_count == 1


# 4
def test_marking_and_reliability(db):
    ps.pair_week(NOW)
    sid = db.rows["peer_session"][0]["id"]
    after = datetime(2027, 2, 6, 8, tzinfo=timezone.utc)
    with pytest.raises(PeerRefused):
        ps.mark("a", sid, False, NOW)
    ps.mark("a", sid, False, after)
    with pytest.raises(PeerRefused):
        ps.mark("a", sid, False, after)
    assert ps.noshows("b", after) == 1
    # A second no-show in another session pauses b.
    s2 = db.rows["peer_session"][1]["id"]
    db.rows["peer_session_participant"] += [{"session_id": "x2", "student_id": "b", "role": "a"},
                                            {"session_id": "x2", "student_id": "z", "role": "b"}]
    db.rows["peer_session"].append({"id": "x2", "starts_at": "2027-02-05T07:00:00+00:00", "status": "scheduled", "minutes": 20})
    ps.mark("z", "x2", False, after)
    paused = [o for o in db.rows["peer_optin"] if o["student_id"] == "b"][0]
    assert paused["paused_until"].startswith("2027-02-20")
    assert "b" not in ps.pool(after)
    ps.mark("b", sid, True, after)
    assert db.rows["peer_session"][0]["status"] == "completed" and s2


# 5
def test_report_and_block(db):
    ps.pair_week(NOW)
    sid = db.rows["peer_session"][0]["id"]
    r = ps.report("a", sid, "contact_details", "asked for my number")
    c = db.rows["concern_report"][0]
    assert (c["about_kind"], c["about_user_id"], c["session_type"], r["status"]) == ("learner", "b", "peer_session", "open")
    ps.block("a", sid)
    assert db.rows["peer_block"] == [{"blocker_id": "a", "blocked_id": "b", "id": "peer_block-1"}]


# 6
def test_practise_now(db):
    assert ps.practise_now("a", NOW) == {"status": "waiting"}
    assert ps.now_status("a") == {"status": "waiting"}
    out = ps.practise_now("b", NOW + timedelta(minutes=2))
    assert out["status"] == "paired"
    assert ps.now_status("a") == {"status": "paired", "session_id": out["session_id"]}
    db.rows["enrolment_state"][2]["band"] = "europe"
    with pytest.raises(PeerRefused) as e:
        ps.practise_now("c", NOW)
    assert e.value.reason == "not_in_pilot"


# 7
def test_attended(db):
    ps.pair_week(NOW)
    sid = db.rows["peer_session"][0]["id"]
    after = datetime(2027, 2, 6, 8, tzinfo=timezone.utc)
    ps.mark("b", sid, True, after)
    assert ps.attended("a", NOW) == 1 and ps.attended("b", NOW) == 0
