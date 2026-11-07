"""LiveKit as a full provider.

Pinned:
1. A room comes from the active provider, and LiveKit needs all three settings.
2. A token is for one person in one room: their name, expiry and what they may send.
3. Only someone in the session gets one, and only near its time.
5. Exchange and peer calls carry a token, and a low-data partner can't send video.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
import pytest

from app.services import calling_service as cs
from app.services import session_join_service as js
from tests.conftest import TEST_USER_ID
from tests.fakedb import FakeDB

SECRET = "lk-secret-long-enough-for-hs256-signing-000"
START = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
VS = {"id": "vs1", "tutor_id": "tutor1", "title": "Pod", "scheduled_at": START.isoformat(), "duration_minutes": 50,
      "video_call_url": "livekit://pod-1"}


@pytest.fixture
def lk():
    with patch.object(cs.settings, "livekit_url", "wss://lk.test"), patch.object(cs.settings, "livekit_api_key", "key"), \
         patch.object(cs.settings, "livekit_api_secret", SECRET):
        yield


@pytest.fixture
def db(lk):
    fake = FakeDB({"video_session": [dict(VS)],
                   "video_session_participant": [{"session_id": "vs1", "student_id": "learner1"}],
                   "cohort_session": [{"video_session_id": "vs1", "cohort_id": "c1", "lead_tutor_id": "cover1"}],
                   "cohort_memberships": [{"cohort_id": "c1", "user_id": "member1", "status": "active"},
                                          {"cohort_id": "c1", "user_id": "left1", "status": "left"}],
                   "one_to_one_session": [], "user_profiles": [{"id": "learner1", "first_name": "Hanna"}]})
    with patch.object(js, "db", fake):
        yield fake


def _claims(token):
    return jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})


# 1
@pytest.mark.asyncio
async def test_a_livekit_room_needs_no_call_to_anyone(lk):
    daily = AsyncMock()
    with patch.object(cs, "get_active_vendor", return_value="livekit"), patch.object(cs, "create_daily_room", daily):
        assert await cs.create_room("pod-1", 123) == "livekit://pod-1"
    daily.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_stays_the_default_path():
    daily = AsyncMock(return_value="https://x.daily.co/pod-1")
    with patch.object(cs, "get_active_vendor", return_value="daily"), patch.object(cs, "create_daily_room", daily):
        assert await cs.create_room("pod-1", 123, {"a": 1}) == "https://x.daily.co/pod-1"
    daily.assert_awaited_once_with("pod-1", 123, properties={"a": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["livekit_url", "livekit_api_key", "livekit_api_secret"])
async def test_no_room_without_all_three_settings(lk, missing):
    with patch.object(cs, "get_active_vendor", return_value="livekit"), patch.object(cs.settings, missing, ""):
        assert await cs.create_room("pod-1", 123) is None
        assert cs.can_make_rooms() is False
        assert cs.livekit_access("livekit://pod-1", "u", None, 123) is None


def test_stored_addresses_are_told_apart():
    assert cs.is_livekit_room("livekit://x") is True
    assert cs.is_livekit_room("https://x.daily.co/y") is False
    assert cs.is_livekit_room(None) is False


# 2
def test_a_token_is_for_one_person_in_one_room(lk):
    a = cs.livekit_access("livekit://pod-1", "u1", "Hanna", 2_000_000_000)
    c = _claims(a["token"])
    assert a["server_url"] == "wss://lk.test" and a["room"] == "pod-1"
    assert c["sub"] == "u1" and c["name"] == "Hanna" and c["exp"] == 2_000_000_000 and c["iss"] == "key"
    assert c["video"]["room"] == "pod-1" and c["video"]["roomJoin"] is True
    assert c["video"]["canPublishSources"] == ["microphone", "camera"]
    assert c["video"]["canPublishData"] is False


def test_audio_only_cannot_send_video(lk):
    a = cs.livekit_access("livekit://ex-1", "u1", None, 2_000_000_000, audio_only=True)
    assert _claims(a["token"])["video"]["canPublishSources"] == ["microphone"]
    assert a["audio_only"] is True and a["start_video_off"] is True


# 3
AT = START - timedelta(minutes=5)


@pytest.mark.parametrize("user", ["tutor1", "learner1", "member1", "cover1"])
def test_people_in_the_session_get_a_token(db, user):
    assert js.join(user, "vs1", AT)["room"] == "pod-1"


@pytest.mark.parametrize("user", ["stranger", "left1"])
def test_nobody_else_does(db, user):
    with pytest.raises(js.JoinRefused) as exc:
        js.join(user, "vs1", AT)
    assert exc.value.reason == "not_in_session"


def test_a_one_to_one_student_gets_one(db):
    db.rows["one_to_one_session"].append({"video_session_id": "vs1", "student_id": "s9", "tutor_id": "t9"})
    assert js.join("s9", "vs1", AT)["room"] == "pod-1"


# 2026-11-07: tutor-led sessions allow typed questions and a raised hand; the tutor may share their screen.
@pytest.mark.parametrize("user,host", [("tutor1", True), ("cover1", True), ("learner1", False), ("member1", False)])
def test_tutor_led_sessions_allow_chat_and_only_the_tutor_shares(db, user, host):
    a = js.join(user, "vs1", AT)
    v = _claims(a["token"])["video"]
    assert a["can_chat"] is True and a["host"] is host
    assert v["canPublishData"] is True and v["canUpdateOwnMetadata"] is True
    assert ("screen_share" in v["canPublishSources"]) is host


def test_a_one_to_one_tutor_is_the_host(db):
    db.rows["one_to_one_session"].append({"video_session_id": "vs1", "student_id": "s9", "tutor_id": "t9"})
    assert js.join("t9", "vs1", AT)["host"] is True


def test_exchanges_and_peers_keep_chat_and_sharing_off(lk):
    v = _claims(cs.livekit_access("livekit://ex-1", "u1", None, 2_000_000_000)["token"])["video"]
    assert v["canPublishData"] is False and v["canUpdateOwnMetadata"] is False
    assert "screen_share" not in v["canPublishSources"]


def test_the_display_name_comes_from_the_profile(db):
    assert _claims(js.join("learner1", "vs1", AT)["token"])["name"] == "Hanna"


@pytest.mark.parametrize("at,reason", [
    (START - timedelta(minutes=16), "not_open"),
    (START + timedelta(minutes=50 + 61), "closed"),
])
def test_only_near_its_time(db, at, reason):
    with pytest.raises(js.JoinRefused) as exc:
        js.join("learner1", "vs1", at)
    assert exc.value.reason == reason


def test_a_token_ends_an_hour_after_the_session(db):
    expected = int((START + timedelta(minutes=110)).timestamp())
    assert _claims(js.join("learner1", "vs1", AT)["token"])["exp"] == expected


def test_a_daily_session_is_refused_here(db):
    db.rows["video_session"][0]["video_call_url"] = "https://x.daily.co/y"
    with pytest.raises(js.JoinRefused) as exc:
        js.join("learner1", "vs1", AT)
    assert exc.value.reason == "not_livekit"


def test_no_keys_is_reported(db):
    with patch.object(cs.settings, "livekit_url", ""), pytest.raises(js.JoinRefused) as exc:
        js.join("learner1", "vs1", AT)
    assert exc.value.reason == "not_configured"


def test_the_route_needs_a_signed_in_person(client):
    assert client.post("/calls/sessions/vs1/join").status_code == 401


def test_the_route_refuses_a_stranger(client, auth_headers, db):
    with patch("app.services.grace_service.is_lapsed", return_value=False):
        res = client.post("/calls/sessions/vs1/join", headers=auth_headers)
    assert res.status_code == 403 and res.json()["detail"]["reason"] == "not_in_session"


def test_the_route_refuses_a_lapsed_learner(client, auth_headers, db):
    with patch("app.services.grace_service.is_lapsed", return_value=True):
        assert client.post("/calls/sessions/vs1/join", headers=auth_headers).status_code == 402


