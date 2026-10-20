"""Placement notices (enrolment plan 3.5, §110, §110b).

Pinned:
1. The .ics is valid RFC 5545 shape: CRLF lines, one VEVENT per scheduled
   session with its id as UID, UTC times, escaped text.
2. The email is written in the learner's own timezone with the season and the
   Ethiopic month, and renders with the checklist.
3. Once per learner per pod: claimed before sending; a failed send releases the
   claim; a stale unsent claim is retried; a pod that isn't live, and members
   from before the last 14 days, get nothing.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import email_service, placement_service as ps

NOW = datetime(2027, 1, 7, 12, tzinfo=timezone.utc)
FIRST = "2027-01-09T15:00:00+00:00"   # 1 Tir 2019, a Saturday


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if self.name == "placement_notice" and any(
                    (r["cohort_id"], r["student_id"]) == (p["cohort_id"], p["student_id"]) for r in rows):
                raise Exception("duplicate key placement_notice_pkey")
            row = {"sent_at": None, **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        if kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        joined = (NOW - timedelta(days=1)).isoformat()
        self.rows = {
            "cohorts": [{"id": "pod1", "name": "Tir, Saturdays", "tutor_id": "tut", "status": "active",
                         "sessions_per_month": 3},
                        {"id": "pod-forming", "name": "F", "tutor_id": "tut", "status": "forming"}],
            "cohort_memberships": [
                {"cohort_id": "pod1", "user_id": "a", "status": "active", "joined_at": joined},
                {"cohort_id": "pod1", "user_id": "b", "status": "active", "joined_at": joined},
                {"cohort_id": "pod1", "user_id": "old", "status": "active",
                 "joined_at": (NOW - timedelta(days=40)).isoformat()},
                {"cohort_id": "pod-forming", "user_id": "c", "status": "active", "joined_at": joined},
            ],
            "cohort_session": [
                {"id": "cs1", "cohort_id": "pod1", "scheduled_at": FIRST, "duration_minutes": 50, "status": "scheduled"},
                {"id": "cs2", "cohort_id": "pod1", "scheduled_at": "2027-01-16T15:00:00+00:00",
                 "duration_minutes": 50, "status": "scheduled"},
                {"id": "cs-x", "cohort_id": "pod1", "scheduled_at": "2027-01-23T15:00:00+00:00",
                 "duration_minutes": 50, "status": "cancelled"},
            ],
            "user_profiles": [{"id": "tut", "first_name": "Marta"},
                              {"id": "a", "timezone": "America/New_York"}, {"id": "b", "timezone": "Europe/London"}],
            "placement_notice": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    sent = []

    async def send(to, template, data, tags=None, attachments=None, reply_to=None):
        sent.append((to, data, attachments))
        return {"status": "failed" if to in fake.failing else "sent"}

    fake.failing = set()
    with patch("app.services.placement_service.db", fake), patch("app.services.makeup_delivery.db", fake), \
         patch("app.services.placement_service._now", return_value=NOW), \
         patch("app.services.placement_service._email_of", side_effect=lambda u: f"{u}@x.test"), \
         patch.object(email_service, "send_template_email", side_effect=send):
        fake.sent = sent
        yield fake


# 1
def test_ics_shape():
    sessions = [{"id": "cs1", "scheduled_at": FIRST, "duration_minutes": 50}]
    body = ps.ics({"name": "Tir, Saturdays; A1"}, sessions, NOW).decode()
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "UID:cs1@tauka.io" in body
    assert "DTSTART:20270109T150000Z" in body and "DTEND:20270109T155000Z" in body
    assert "SUMMARY:Tauka pod session · Tir\\, Saturdays\\; A1" in body
    assert body.count("BEGIN:VEVENT") == 1 and "\n" not in body.replace("\r\n", "")


# 2
def test_content_in_learners_zone():
    sessions = [{"id": "cs1", "scheduled_at": FIRST, "duration_minutes": 50}]
    c = ps.content({"name": "Tir, Saturdays", "sessions_per_month": 3}, sessions, "Marta", 4, "America/New_York")
    assert c["first_session"] == "Sat 9 Jan, 10:00 (America/New_York)"
    assert c["slot"] == "Saturdays at 10:00 (America/New_York), three times a month"
    assert c["ethiopic_month"] == "Tir 2019" and c["season"].startswith("Bega")


def test_template_renders_checklist():
    sessions = [{"id": "cs1", "scheduled_at": FIRST, "duration_minutes": 50}]
    data = ps.content({"name": "Tir <b>"}, sessions, "Marta", 4, "UTC")
    html = email_service._jinja_env.get_template("placement.html").render(**data)
    assert "Test your device" in html and "turn your camera off" in html and "end of the month" in html
    assert "Tir &lt;b&gt;" in html and "&lt;strong" not in html


# 3
@pytest.mark.asyncio
async def test_once_per_learner(db):
    assert await ps.notify_cohort("pod1", NOW) == 2
    learners = [s for s in db.sent if s[0] != "tut@x.test"]
    assert {s[0] for s in learners} == {"a@x.test", "b@x.test"}  # not "old"
    _, data, att = learners[0]
    assert att[0]["filename"] == "tauka-pod.ics" and att[0]["content"].count(b"BEGIN:VEVENT") == 2
    assert data["members"] == 3 and data["tutor_name"] == "Marta"
    assert all(r["sent_at"] for r in db.rows["placement_notice"])
    # Plan 3.5, the tutor's half: one email naming the two newcomers.
    tutor = [s for s in db.sent if s[0] == "tut@x.test"]
    assert len(tutor) == 1 and len(tutor[0][1]["names"]) == 2 and tutor[0][1]["new_pod"] is False
    assert await ps.notify_cohort("pod1", NOW) == 0
    assert len(db.sent) == 3


@pytest.mark.asyncio
async def test_not_live_gets_nothing(db):
    assert await ps.notify_cohort("pod-forming", NOW) == 0 and db.sent == []


@pytest.mark.asyncio
async def test_failed_send_releases_claim_and_stale_claim_retried(db):
    db.failing = {"a@x.test"}
    assert await ps.notify_cohort("pod1", NOW) == 1
    assert {r["student_id"] for r in db.rows["placement_notice"]} == {"b"}
    db.failing = set()
    # An abandoned send: claimed two hours ago, never stamped.
    db.rows["placement_notice"].append({"cohort_id": "pod1", "student_id": "a", "sent_at": None,
                                        "claimed_at": (NOW - timedelta(hours=2)).isoformat()})
    assert await ps.notify_cohort("pod1", NOW) == 1
    # A fresh unsent claim (someone else sending right now) is left alone.
    db.rows["placement_notice"] = [{"cohort_id": "pod1", "student_id": "a", "sent_at": None,
                                    "claimed_at": NOW.isoformat()}]
    before = len(db.sent)
    await ps.notify_cohort("pod1", NOW)
    assert not any(s[0] == "a@x.test" for s in db.sent[before:])


def test_tutor_template_renders():
    html = email_service._jinja_env.get_template("placement_tutor.html").render(
        subject="s", pod_name="Tir, Saturdays", names=["Abebe", "Hana"], members=4, new_pod=False,
        first_session="Sat 9 Jan, 10:00 (Africa/Nairobi)", slot="Saturdays at 10:00", minutes=50,
        web_base_url="https://tauka.io")
    assert "2 new learners in Tir, Saturdays" in html and "Abebe, Hana" in html
