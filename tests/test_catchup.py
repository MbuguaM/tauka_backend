"""Catch-up for a learner who missed a pod session (enrolment plan 4.8, §114).

Pinned (developer decisions 2026-10-18):
1. Ready once the session has ended and the tutor wrote notes, or 48 h after
   it either way; never a cancelled session; only the last 7 days.
2. Absent and excused learners still in the pod get one email with the notes
   and the lesson's practice; attendees, leavers and the already-sent don't.
3. A failed send releases the claim; the template escapes the notes.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import catchup_service as cs
from app.services import email_service

NOW = datetime(2027, 2, 20, 12, tzinfo=timezone.utc)


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def test_ready_rules():
    base = {"status": "completed", "duration_minutes": 50, "tutor_notes": None}
    assert not cs.ready({**base, "scheduled_at": _at(0.5)}, NOW)                       # still running
    assert not cs.ready({**base, "scheduled_at": _at(10)}, NOW)                        # no notes, < 48 h
    assert cs.ready({**base, "scheduled_at": _at(10), "tutor_notes": "We did verbs"}, NOW)
    assert cs.ready({**base, "scheduled_at": _at(50)}, NOW)                            # 48 h passed
    assert not cs.ready({**base, "scheduled_at": _at(50), "status": "cancelled"}, NOW)
    assert not cs.ready({**base, "scheduled_at": _at(24 * 8), "tutor_notes": "x"}, NOW)  # too old


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if any((r["cohort_session_id"], r["student_id"]) == (p["cohort_session_id"], p["student_id"])
                   for r in rows):
                raise Exception("duplicate key catchup_notice_pkey")
            rows.append(dict(p))
            return SimpleNamespace(data=[p])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.rows = {
            "cohort_session": [
                {"id": "s1", "cohort_id": "pod1", "scheduled_at": _at(10), "duration_minutes": 50,
                 "status": "completed", "lesson_ref": 4, "tutor_notes": "Past tense <b>verbs</b>",
                 "recording_path": "r/s1.mp4", "recording_expires_at": (NOW + timedelta(days=80)).isoformat()},
                {"id": "s-fresh", "cohort_id": "pod1", "scheduled_at": _at(5), "duration_minutes": 50,
                 "status": "completed", "lesson_ref": 5, "tutor_notes": None},
            ],
            "cohort_session_attendance": [
                {"cohort_session_id": "s1", "user_id": "absent", "status": "absent"},
                {"cohort_session_id": "s1", "user_id": "excused", "status": "excused"},
                {"cohort_session_id": "s1", "user_id": "came", "status": "attended"},
                {"cohort_session_id": "s1", "user_id": "left", "status": "absent"},
                {"cohort_session_id": "s-fresh", "user_id": "absent", "status": "absent"},
            ],
            "catchup_notice": [],
            "cohorts": [{"id": "pod1", "name": "Tir A", "course_id": "am"}],
            "cohort_memberships": [{"cohort_id": "pod1", "user_id": u, "status": "active"}
                                   for u in ("absent", "excused", "came")],
            "unit": [{"course_id": "am", "unit_order": 4, "title": "At the market", "level": 1},
                     {"course_id": "am", "unit_order": 4, "title": "Fidel 4", "level": None}],
            "user_profiles": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    sent = []

    async def send(to, template, data, tags=None, **k):
        sent.append((to, data))
        return {"status": "failed" if to in fake.failing else "sent"}

    fake.failing = set()
    with patch("app.services.catchup_service.db", fake), patch("app.services.makeup_delivery.db", fake), \
         patch("app.services.makeup_delivery._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch.object(email_service, "send_template_email", side_effect=send):
        fake.sent = sent
        yield fake


@pytest.mark.asyncio
async def test_sends_once_to_absent_and_excused(db):
    assert await cs.daily(NOW) == 2
    assert sorted(t for t, _ in db.sent) == ["absent@x.test", "excused@x.test"]
    data = db.sent[0][1]
    assert data["lesson"] == 4 and data["lesson_title"] == "At the market"
    assert data["notes"] == "Past tense <b>verbs</b>" and data["recording_until"]
    assert await cs.daily(NOW) == 0                      # once


@pytest.mark.asyncio
async def test_failed_send_is_retried(db):
    db.failing = {"absent@x.test"}
    assert await cs.daily(NOW) == 1
    assert [r["student_id"] for r in db.rows["catchup_notice"]] == ["excused"]
    db.failing = set()
    assert await cs.daily(NOW) == 1


def test_template_escapes_notes():
    html = email_service._jinja_env.get_template("catchup.html").render(
        subject="s", pod_name="Tir A", when="Sat 20 Feb, 18:00 (UTC)", notes="Past tense <b>verbs</b>",
        lesson=4, lesson_title="At the market", recording_until="11 May 2027")
    assert "&lt;b&gt;verbs" in html and "lesson 4 (At the market)" in html and "11 May 2027" in html
