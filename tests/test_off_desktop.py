"""A tutor teaching from a phone (D16).

Pinned:
1. The tutor of a session, or its cover tutor, is recorded once per session.
2. Learners, strangers, bad ids and unknown platforms aren't recorded.
3. The people told are the owners and top-rung tutors, and an email failure stays out of the way.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services import off_desktop_service as svc
from tests.fakedb import FakeDB

VS_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db():
    fake = FakeDB({
        "video_session": [{"id": VS_ID, "tutor_id": "tutor1", "title": "Pod", "scheduled_at": "2026-11-07T10:00:00+00:00"}],
        "cohort_session": [{"video_session_id": VS_ID, "lead_tutor_id": "cover1"}],
        "one_to_one_session": [],
        "off_desktop_session": [],
        "admin_users": [{"id": "owner1", "deleted_at": None}, {"id": "gone1", "deleted_at": "2026-01-01"},
                        {"id": "ops1", "deleted_at": None}],
        "user_profiles": [{"id": "tutor1", "first_name": "Selam"}],
        "tutor_rung": [{"tutor_id": "head1", "rung": 4, "effective_to": None},
                       {"tutor_id": "former1", "rung": 4, "effective_to": "2026-10-01T00:00:00+00:00"},
                       {"tutor_id": "pod1", "rung": 3, "effective_to": None},
                       {"tutor_id": "cover1", "rung": 4, "effective_to": None}],
    })
    with patch.object(svc, "db", fake):
        yield fake


# 1
@pytest.mark.parametrize("tutor", ["tutor1", "cover1"])
def test_a_tutor_is_recorded_once(db, tutor):
    assert svc.record(tutor, VS_ID, "android")["platform"] == "android"
    assert svc.record(tutor, VS_ID, "android") is None
    assert len(db.rows["off_desktop_session"]) == 1


# 2
def test_someone_else_is_refused(db):
    with pytest.raises(svc.OffDesktopRefused, match="not_the_tutor"):
        svc.record("learner1", VS_ID, "ios")
    assert db.rows["off_desktop_session"] == []


@pytest.mark.parametrize("session, platform, reason", [
    ("not-a-uuid", "ios", "no_such_session"),
    ("22222222-2222-2222-2222-222222222222", "ios", "no_such_session"),
    (VS_ID, "windows", "invalid_platform"),
])
def test_bad_reports_are_refused(db, session, platform, reason):
    with pytest.raises(svc.OffDesktopRefused, match=reason):
        svc.record("tutor1", session, platform)


# 3
@pytest.mark.asyncio
async def test_the_owner_and_current_top_rung_tutors_are_told(db):
    row = svc.record("tutor1", VS_ID, "ios")
    send = AsyncMock(return_value={"status": "sent"})
    with patch.object(svc.email_service, "send_email", send), \
         patch.object(svc.hs, "admin_can", side_effect=lambda a, c: a == "owner1" and c == "owner"), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda a: f"{a}@tauka.test"):
        await svc.notify(row)
    args = send.await_args.args
    assert args[0] == ["cover1@tauka.test", "head1@tauka.test", "owner1@tauka.test"]
    assert "Selam" in args[1]
    assert "an iPhone" in args[2]
    assert db.rows["off_desktop_session"][0]["notified_at"]


@pytest.mark.asyncio
async def test_a_head_on_a_phone_is_not_told_about_themselves(db):
    with patch.object(svc.hs, "admin_can", side_effect=lambda a, c: a == "owner1"), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda a: f"{a}@tauka.test"):
        assert svc._recipients("head1") == ["cover1@tauka.test", "owner1@tauka.test"]


@pytest.mark.asyncio
async def test_a_failed_email_is_not_marked_and_does_not_raise(db):
    row = svc.record("tutor1", VS_ID, "android")
    with patch.object(svc.email_service, "send_email", AsyncMock(side_effect=RuntimeError("down"))), \
         patch.object(svc.hs, "admin_can", return_value=True), \
         patch("app.services.makeup_delivery._email_of", return_value="a@tauka.test"):
        await svc.notify(row)
    assert not db.rows["off_desktop_session"][0].get("notified_at")
