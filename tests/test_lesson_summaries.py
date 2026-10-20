"""Lesson summaries (features addendum B, §143), Fidel transliteration, pod rooms.

Pinned:
1. Off without the flag: no panel.
2. The panel shows only for a non-English summary language in the launch list.
3. X-16: the model never writes Fidel. Its {{n}} references become the lesson's
   own Fidel with a transliteration; a reply with Ethiopic is retried, then refused.
4. Cached per lesson and language; a newer content_version regenerates.
5. A flag hides the summary at once and the next view regenerates it.
6. Questions: behind their own flag, 20 a day, Fidel only from the lesson.
7. Review: approve resolves flags; an edit with Fidel is refused.
8. Transliteration is deterministic.
9. Pod rooms: made for sessions in the next 24 h, never without a URL.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core.fidel_translit import has_fidel, latin
from app.services import cohort_room_service as rooms
from app.services import summary_service as ss
from app.services.summary_service import SummaryRefused
from tests.fakedb import FakeDB

NOW = datetime(2027, 2, 1, 6, tzinfo=timezone.utc)
LESSON = {"title": "Unit 1", "sections": [
    {"title": "Basic Sentences", "type": "reference", "con": {"con": [{"voc": [
        {"E": "health", "A": "ጤና"}, {"E": "Hello. (general greeting)", "A": "ጤና ይስጥልኝ"}]}]}},
    {"title": "Grammatical Notes", "type": "reference", "con": [
        {"paragraph": "Verbs agree with the subject in person and gender."}]}]}
GOOD = json.dumps({"bullets": ["Greet with {{2}}.", "{{1}} means health.", "Verbs agree with the subject."],
                   "glosses": {"1": "בריאות", "2": "שלום"}})
BAD = json.dumps({"bullets": ["Say ሰላም", "two", "three"], "glosses": {}})


def _db(flags=("lesson_summaries",)):
    return FakeDB({
        "feature_flag": [{"key": k, "status": "ready"} for k in flags],
        "config": [],
        "user_profiles": [{"id": "he1", "first_language": "he", "summaries_enabled": True, "user_type": "student"},
                          {"id": "en1", "first_language": "en", "user_type": "student"}],
        "unit": [{"id": "u1", "title": "Unit 1", "json_file": "courses/amharic/U_1.json", "content_version": 1,
                  "is_preliminary": False, "unit_order": 1}],
        "lesson_summary": [], "summary_event": [], "lesson_summary_flag": [],
    })


class _Storage:
    def from_(self, _bucket):
        return self

    def download(self, _path):
        return json.dumps(LESSON).encode()


@pytest.fixture
def db():
    fake = _db()
    fake.storage = _Storage()
    with patch("app.services.summary_service.db", fake), patch("app.core.flags.db", fake), \
         patch("app.services.summary_service._ledger", new=AsyncMock()):
        yield fake


# 1
@pytest.mark.asyncio
async def test_off_without_flag():
    fake = _db(flags=())
    with patch("app.services.summary_service.db", fake), patch("app.core.flags.db", fake):
        assert (await ss.for_learner("he1", "u1"))["show"] is False


# 2, 3
@pytest.mark.asyncio
async def test_panel_and_fidel_rule(db):
    assert (await ss.for_learner("en1", "u1"))["show"] is False
    with patch("app.services.ai_services.call_ai", new=AsyncMock(side_effect=[BAD, GOOD])) as ai:
        out = await ss.for_learner("he1", "u1")
    assert ai.await_count == 2                                       # the Fidel reply was retried
    s = out["summary"]
    assert s["rtl"] and s["label"].startswith("AI summary")
    first = s["content"]["bullets"][0]
    assert first[0] == {"text": "Greet with "} and first[1]["fidel"] == "ጤና ይስጥልኝ"
    assert first[1]["latin"] == latin("ጤና ይስጥልኝ")
    vocab = s["content"]["vocabulary"]
    assert vocab[0]["fidel"] == "ጤና" and vocab[0]["gloss"] == "בריאות"
    # Every Fidel string in the stored summary is one of the lesson's own.
    lesson_fidel = {"ጤና", "ጤና ይስጥልኝ"}
    for b in s["content"]["bullets"]:
        for seg in b:
            assert "fidel" not in seg or seg["fidel"] in lesson_fidel
            assert not has_fidel(seg.get("text", ""))


@pytest.mark.asyncio
async def test_rejected_twice(db):
    with patch("app.services.ai_services.call_ai", new=AsyncMock(return_value=BAD)):
        out = await ss.for_learner("he1", "u1")
    assert out["pending"] and out["reason"] == "generation_rejected"
    assert db.rows["lesson_summary"] == []


# 4, 5
@pytest.mark.asyncio
async def test_cache_version_and_flag(db):
    with patch("app.services.ai_services.call_ai", new=AsyncMock(return_value=GOOD)) as ai:
        a = await ss.for_learner("he1", "u1")
        await ss.for_learner("he1", "u1")
        assert ai.await_count == 1                                   # cached
        db.rows["unit"][0]["content_version"] = 2
        await ss.for_learner("he1", "u1")
        assert ai.await_count == 2                                   # the lesson changed
        live = [r for r in db.rows["lesson_summary"] if r["status"] == "live"]
        assert len(live) == 1 and live[0]["content_version"] == 2
        ss.flag("he1", live[0]["id"], "wrong gloss")
        assert live[0]["id"] not in {r["id"] for r in db.rows["lesson_summary"] if r["status"] == "live"}
        await ss.for_learner("he1", "u1")
        assert ai.await_count == 3                                   # regenerated on the next view
    assert a["summary"]["content_version"] == 1


# 6
@pytest.mark.asyncio
async def test_questions(db):
    with pytest.raises(SummaryRefused, match="not_enabled"):
        await ss.ask("he1", "u1", "why?", now=NOW)
    db.rows["feature_flag"].append({"key": "summary_questions", "status": "ready"})
    with patch("app.services.ai_services.call_ai", new=AsyncMock(side_effect=["Use ሰላም", "Use {{2}} in the morning."])):
        out = await ss.ask("he1", "u1", "how do I greet?", now=NOW)
    assert out["answer"][1]["fidel"] == "ጤና ይስጥልኝ" and out["left"] == 19
    db.rows["summary_event"] += [{"user_id": "he1", "event": "question", "created_at": NOW.isoformat()}] * 20
    with pytest.raises(SummaryRefused, match="limit_reached"):
        await ss.ask("he1", "u1", "again?", now=NOW)


# 7
@pytest.mark.asyncio
async def test_review(db):
    with patch("app.services.ai_services.call_ai", new=AsyncMock(return_value=GOOD)):
        await ss.for_learner("he1", "u1")
    sid = db.rows["lesson_summary"][0]["id"]
    ss.flag("he1", sid, None)
    with patch("app.services.hierarchy_service.admin_can", return_value=True):
        assert ss.queue("ops")["items"][0]["flags"]
        with pytest.raises(SummaryRefused, match="bad_value"):
            ss.review("ops", sid, "edit", bullets=["Say ሰላም"])
        ss.review("ops", sid, "approve")
    row = db.rows["lesson_summary"][0]
    assert row["status"] == "live" and row["reviewed_by"] == "ops"
    assert all(f["outcome"] == "kept" for f in db.rows["lesson_summary_flag"])


def test_prefs(db):
    p = ss.prefs("he1")
    assert p["show"] and p["summary_language"] == "he" and p["rtl"]
    db.rows["user_profiles"][0]["summaries_enabled"] = False          # the app writes its own row
    assert ss.prefs("he1")["show"] is False
    db.rows["user_profiles"][0].update({"summaries_enabled": True, "summary_language": "en"})
    assert ss.prefs("he1")["show"] is False                            # English is never "summarised"


# 8
def test_transliteration():
    assert latin("ኢትዮጵያ") == "ityop'ya"
    assert latin("ሰላም") == "selam"
    assert latin("እንደምን") == "əndemən"
    assert latin("ቋንቋ") == "qwanqwa"
    assert latin("ልክ፟ ነው") == "lək new"
    assert latin("hello") == "hello" and not has_fidel("hello") and has_fidel("ሀ")


def test_extract_takes_the_lessons_own_words():
    out = ss.extract(LESSON)
    assert [v["fidel"] for v in out["vocab"]] == ["ጤና", "ጤና ይስጥልኝ"]
    assert "Verbs agree" in out["english"]


# 9
@pytest.mark.asyncio
async def test_pod_rooms():
    fake = FakeDB({
        "cohort_session": [
            {"id": "cs-1", "cohort_id": "c1", "scheduled_at": (NOW + timedelta(hours=3)).isoformat(),
             "duration_minutes": 50, "status": "scheduled", "video_session_id": None, "lead_tutor_id": None},
            {"id": "cs-2", "cohort_id": "c1", "scheduled_at": (NOW + timedelta(days=3)).isoformat(),
             "duration_minutes": 50, "status": "scheduled", "video_session_id": None}],
        "cohorts": [{"id": "c1", "name": "Tir Saturday", "tutor_id": "t1", "status": "active"}],
        "cohort_memberships": [{"cohort_id": "c1", "user_id": "a", "status": "active"},
                               {"cohort_id": "c1", "user_id": "b", "status": "active"}],
        "video_session": [], "video_session_participant": [],
    })
    with patch("app.services.cohort_room_service.db", fake), \
         patch("app.config.settings.daily_api_key", ""):
        out = await rooms.hourly(NOW)
    assert out["skipped"] == "no_daily_key" and fake.rows["video_session"] == []
    with patch("app.services.cohort_room_service.db", fake), \
         patch("app.config.settings.daily_api_key", "k"), \
         patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value="https://x.daily.co/p")):
        out = await rooms.hourly(NOW)
    assert out == {"due": 1, "made": 1}
    vs = fake.rows["video_session"][0]
    assert vs["tutor_id"] == "t1" and vs["video_call_url"] == "https://x.daily.co/p" and vs["title"] == "Tir Saturday"
    assert fake.rows["cohort_session"][0]["video_session_id"] == vs["id"]
    assert fake.rows["cohort_session"][1]["video_session_id"] is None          # three days away: not yet
    assert len(fake.rows["video_session_participant"]) == 2
    with patch("app.services.cohort_room_service.db", fake), \
         patch("app.config.settings.daily_api_key", "k"), \
         patch("app.services.calling_service.create_daily_room", new=AsyncMock(return_value=None)):
        fake.rows["cohort_session"][1]["scheduled_at"] = (NOW + timedelta(hours=5)).isoformat()
        out = await rooms.hourly(NOW)
    assert out["made"] == 0 and len(fake.rows["video_session"]) == 1            # no URL, no row
