"""Communications (enrolment plan Track 6, §133).

Pinned:
1. notify: once per (user, kind, ref); nothing switched on -> nothing sent and
   the claim released; push only with PUSH_ENABLED.
2. WhatsApp: off without the flag; opt-in stores the number encrypted; a send
   needs an opt-in; STOP opts out by phone hash; Meta's signature is checked.
3. Session reminders pick sessions 24 h and 1 h ahead, learners and the tutor.
4. DST: the first session whose local time moves is found; a member warned once.
5. Season: one welcome per live pod; one season post per formed (non-rehearsal) intake.
6. Telegram is off without its settings.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

from app.services import (dst_service, notify_service, push_service, reminder_service, season_service,
                          telegram_service, whatsapp_service)
from tests.fakedb import FakeDB

NOW = datetime(2027, 3, 20, 12, tzinfo=timezone.utc)


def _db(**rows):
    return FakeDB({"notice_log": [], "feature_flag": [], **rows},
                  unique={"notice_log": [("user_id", "kind", "ref")], "whatsapp_contact": [("user_id",)]})


# 1
@pytest.mark.asyncio
async def test_notify_once_and_release_when_nothing_on():
    db = _db()
    with patch("app.services.notify_service.db", db), patch("app.core.flags.db", db), \
         patch.object(push_service.settings, "push_enabled", False):
        assert await notify_service.notify("u", "session_1h", "s1", "t", "b") == []
        assert db.rows["notice_log"] == []                               # released
    with patch("app.services.notify_service.db", db), \
         patch("app.services.push_service.send", AsyncMock(return_value=True)):
        assert await notify_service.notify("u", "session_1h", "s1", "t", "b") == ["push"]
        assert await notify_service.notify("u", "session_1h", "s1", "t", "b") == []   # once
    assert db.rows["notice_log"][0]["channels"] == ["push"]


@pytest.mark.asyncio
async def test_push_off_by_default():
    with patch.object(push_service.settings, "push_enabled", False):
        assert await push_service.send("u", "t", "b") is False


# 2
@pytest.mark.asyncio
async def test_whatsapp_flag_optin_send_and_stop():
    db = _db(whatsapp_contact=[])
    key = Fernet.generate_key().decode()
    with patch("app.services.whatsapp_service.db", db), patch("app.core.flags.db", db), \
         patch.object(whatsapp_service.settings, "encryption_key", key), \
         patch.object(whatsapp_service.settings, "whatsapp_provider", "console"):
        assert whatsapp_service.mine("u") == {"enabled": False}
        with pytest.raises(whatsapp_service.WhatsappRefused):
            whatsapp_service.opt_in("u", "+254712345678")
        db.rows["feature_flag"].append({"key": "whatsapp_reminders", "status": "ready"})
        assert await whatsapp_service.send_template("u", "seat_offer", ["P"]) is False      # not opted in
        out = whatsapp_service.opt_in("u", "+254 712 345 678")
        assert out == {"enabled": True, "opted_in": True, "phone_last4": "5678"}
        assert "254712345678" not in db.rows["whatsapp_contact"][0]["phone_encrypted"]
        assert await whatsapp_service.send_template("u", "seat_offer", ["P"]) is True
        payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "254712345678", "text": {"body": " stop "}}]}}]}]}
        assert whatsapp_service.handle_inbound(payload) == 1
        assert await whatsapp_service.send_template("u", "seat_offer", ["P"]) is False      # opted out
    with patch.object(whatsapp_service.settings, "whatsapp_app_secret", "s3"):
        raw = b'{"a":1}'
        good = "sha256=" + hmac.new(b"s3", raw, hashlib.sha256).hexdigest()
        assert whatsapp_service.verify_signature(raw, good) and not whatsapp_service.verify_signature(raw, "sha256=x")


# 3
@pytest.mark.asyncio
async def test_session_reminders():
    db = _db(
        cohort_session=[{"id": "cs1", "cohort_id": "p1", "status": "scheduled", "lead_tutor_id": "t1",
                         "scheduled_at": (NOW + timedelta(hours=24)).isoformat()},
                        {"id": "cs2", "cohort_id": "p1", "status": "scheduled", "lead_tutor_id": "t1",
                         "scheduled_at": (NOW + timedelta(hours=5)).isoformat()}],
        cohort_memberships=[{"cohort_id": "p1", "user_id": "a", "status": "active"}],
        makeup_session=[{"id": "m1", "status": "scheduled", "tutor_id": "t2",
                         "scheduled_at": (NOW + timedelta(minutes=60)).isoformat()}],
        makeup_session_participant=[{"makeup_session_id": "m1", "student_id": "b", "status": "pending"}],
        clinic=[], clinic_booking=[], match_offer=[])
    sent = []

    async def fake_notify(uid, kind, ref, *a, **k):
        sent.append((uid, kind, ref))
        return ["push"]
    with patch("app.services.reminder_service.db", db), \
         patch("app.services.makeup_delivery._zones", side_effect=lambda ids: {}), \
         patch("app.services.notify_service.notify", fake_notify):
        out = await reminder_service.hourly(NOW)
    assert sorted(sent) == [("a", "session_24h", "cs1"), ("b", "session_1h", "m1"),
                            ("t1", "session_24h", "cs1"), ("t2", "session_1h", "m1")]
    assert out["session_24h"] == 2 and out["session_1h"] == 2


# 4
def test_first_change_finds_the_dst_shift():
    # A pod at 17:00 UTC; London moves to BST on Sun 28 Mar 2027.
    starts = [datetime(2027, 3, 21, 17, tzinfo=timezone.utc), datetime(2027, 3, 28, 17, tzinfo=timezone.utc),
              datetime(2027, 4, 4, 17, tzinfo=timezone.utc)]
    at, was, now = dst_service.first_change(starts, "Europe/London")
    assert at.day == 28 and (was, now) == ("17:00", "18:00")
    assert dst_service.first_change(starts, "Africa/Nairobi") is None


@pytest.mark.asyncio
async def test_dst_daily_warns_once():
    db = _db(cohorts=[{"id": "p1", "name": "Pod", "status": "active", "tutor_id": None}],
             cohort_session=[{"id": f"s{d}", "cohort_id": "p1", "status": "scheduled",
                              "scheduled_at": datetime(2027, 3, d, 17, tzinfo=timezone.utc).isoformat()}
                             for d in (21, 28)],
             cohort_memberships=[{"cohort_id": "p1", "user_id": "a", "status": "active"}])
    mail = AsyncMock(return_value={"status": "sent"})
    with patch("app.services.dst_service.db", db), patch("app.services.notify_service.db", db), \
         patch("app.services.makeup_delivery._zones", side_effect=lambda ids: {"a": "Europe/London"}), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch("app.services.dst_service.email_service.send_template_email", mail), \
         patch("app.services.push_service.send", AsyncMock(return_value=False)):
        soon = datetime(2027, 3, 21, 9, tzinfo=timezone.utc)
        assert (await dst_service.daily(soon))["warned"] == 1
        assert (await dst_service.daily(soon))["warned"] == 0
    assert mail.await_count == 1 and mail.await_args.args[2]["now"] == "18:00"


# 5
def test_season_welcomes_and_posts():
    db = _db(cohorts=[{"id": "p1", "name": "Tir, Saturdays", "status": "active", "start_date": "2027-01-09"},
                      {"id": "p2", "name": "F", "status": "forming", "start_date": "2027-01-09"}],
             intake=[{"id": "i1", "season_id": "bega", "formation_date": "2027-01-09", "status": "formed",
                      "mode": "live", "season": {"display_name": "Bega 2019"}},
                     {"id": "i0", "season_id": "tsedey", "formation_date": "2026-11-10", "status": "formed",
                      "mode": "rehearsal", "season": {"display_name": "Tsedey 2019"}}],
             broadcast=[])
    with patch("app.services.season_service.db", db):
        assert season_service.daily() == {"pod_welcomes": 1, "season_posts": 1}
        assert season_service.daily() == {"pod_welcomes": 0, "season_posts": 0}
    posts = {(b["target_type"], b["target_id"]): b["content"] for b in db.rows["broadcast"]}
    assert "part of the Bega 2019 cohort" in posts[("cohort", "p1")]
    assert posts[("season", "bega")].startswith("Bega 2019: 1 new pod formed")


# 6
@pytest.mark.asyncio
async def test_telegram_off_without_settings():
    with patch.object(telegram_service.settings, "telegram_bot_token", ""):
        assert await telegram_service.post("x") is False
