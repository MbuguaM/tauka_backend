"""Phase 0 and enrolment_state sync (enrolment plan 1.3 / 1.6, §98).

Pinned:
1. **Which intake a learner aims at** — the earliest whose lesson deadline is
   reachable; finished learners go by the day they finished.
2. **Finished = Phase 0 unit 2 completed** in `app.unit_progress` (the progress
   tab's verdict). Unit 1 completing is not enough.
3. **Paid before seat** (§2c): a free learner who finished stays short of
   seat_confirmed; paying moves them there, written as two legal hops.
4. **A missed deadline rolls forward**, no penalty.
5. Later states (matched, in_pod, …) are never touched by the sync.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import phase0_service
from tests.conftest import make_token, TEST_USER_ID

COURSE = "11111111-1111-1111-1111-111111111111"

# The production calendar's first five rows (§98).
INTAKES = [
    {"id": "hidar",   "ethiopic_month": "hidar",   "ec_year": 2019, "formation_date": "2026-11-10", "cutoff_opens": "2026-11-01", "cutoff_closes": "2026-11-05", "tier": "founding", "status": "scheduled", "season": {"display_name": "Hidar 2019 (founding)"}},
    {"id": "tahsas",  "ethiopic_month": "tahsas",  "ec_year": 2019, "formation_date": "2026-12-10", "cutoff_opens": "2026-12-01", "cutoff_closes": "2026-12-05", "tier": "light", "status": "scheduled", "season": {"display_name": "Bega 2019"}},
    {"id": "tir",     "ethiopic_month": "tir",     "ec_year": 2019, "formation_date": "2027-01-09", "cutoff_opens": "2026-12-28", "cutoff_closes": "2027-01-05", "tier": "flagship", "status": "scheduled", "season": {"display_name": "Bega 2019"}},
    {"id": "yekatit", "ethiopic_month": "yekatit", "ec_year": 2019, "formation_date": "2027-02-08", "cutoff_opens": "2027-02-01", "cutoff_closes": "2027-02-05", "tier": "standard", "status": "scheduled", "season": {"display_name": "Bega 2019"}},
]


# ── choose_target (pure) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("started,today,completed,expect", [
    # Started 18 Oct: Hidar's 5 Nov deadline is 18 days out, ≥ 14 → Hidar.
    (date(2026, 10, 18), date(2026, 10, 18), None, "hidar"),
    # Started 25 Oct: only 11 days to 5 Nov → Tahsas.
    (date(2026, 10, 25), date(2026, 10, 25), None, "tahsas"),
    # Started 18 Oct, now 6 Nov, not finished: Hidar's deadline passed → Tahsas.
    (date(2026, 10, 18), date(2026, 11, 6), None, "tahsas"),
    # Finished on 5 Nov: still Hidar ("finish by 5 Nov to join Hidar").
    (date(2026, 10, 18), date(2026, 11, 7), date(2026, 11, 5), "hidar"),
    # Finished on 6 Nov: one day late → Tahsas.
    (date(2026, 10, 16), date(2026, 11, 6), date(2026, 11, 6), "tahsas"),
    # Started 20 Nov: Tahsas's 5 Dec is 15 days → Tahsas.
    (date(2026, 11, 20), date(2026, 11, 20), None, "tahsas"),
    # Tir's deadline window opens 28 Dec (the exception) but closes 5 Jan.
    (date(2026, 12, 1), date(2026, 12, 1), None, "tir"),
])
def test_choose_target(started, today, completed, expect):
    got = phase0_service.choose_target(INTAKES, started=started, today=today, completed=completed)
    assert got["id"] == expect


def test_no_reachable_intake_is_none():
    assert phase0_service.choose_target(INTAKES, started=date(2027, 3, 1), today=date(2027, 3, 1), completed=None) is None


def test_cancelled_intake_is_skipped():
    rows = [{**INTAKES[0], "status": "cancelled"}, *INTAKES[1:]]
    assert phase0_service.choose_target(rows, started=date(2026, 10, 18), today=date(2026, 10, 18), completed=None)["id"] == "tahsas"


# ── sync against a fake database ─────────────────────────────────────────────

class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            row = {"id": f"{self.name}-{len(rows)+1}", "created_at": "2026-10-18T00:00:00+00:00",
                   "target_intake_id": None, "phase0_completed_at": None, **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                if self.name == "enrolment_state" and "state" in p:
                    self.db.transitions.append((r["state"], p["state"]))
                r.update(p)
        return SimpleNamespace(data=hit)


class _DB:
    def __init__(self):
        self.rows = {"intake": [dict(r) for r in INTAKES], "student": [{"id": TEST_USER_ID, "tier": "free_tier"}],
                     "unit": [{"id": "u-intro", "course_id": COURSE, "is_preliminary": True, "unit_order": 1},
                              {"id": "u-alpha", "course_id": COURSE, "is_preliminary": True, "unit_order": 2}],
                     "unit_progress": [], "enrolment_state": []}
        self.transitions = []

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.phase0_service.db", fake), \
         patch("app.services.phase0_service._today", return_value=date(2026, 10, 18)):
        yield fake


def _complete(db, unit_id, when="2026-11-03T10:00:00+00:00"):
    db.rows["unit_progress"].append({"unit_id": unit_id, "user_id": TEST_USER_ID, "status": "completed", "completed_at": when})


def test_start_creates_phase0_row_aimed_at_hidar(db):
    out = phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=10, band="europe")
    assert out["enrolled"] and out["state"] == "phase0"
    assert out["pace_pledge_hours"] == 10 and out["band"] == "europe"
    assert out["target_intake"]["ethiopic_month"] == "hidar"
    assert out["target_intake"]["cutoff_closes"] == "2026-11-05"
    assert out["target_intake"]["season"] == "Hidar 2019 (founding)"


def test_start_twice_keeps_one_row_and_updates_pledge(db):
    phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=5, band=None)
    out = phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=20, band="asia")
    assert len(db.rows["enrolment_state"]) == 1
    assert out["pace_pledge_hours"] == 20 and out["band"] == "asia"


def test_not_enrolled_until_started(db):
    assert phase0_service.status(TEST_USER_ID, COURSE) == {"enrolled": False}


def test_paying_moves_phase0_to_paid(db):
    phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=None, band=None)
    db.rows["student"][0]["tier"] = "learner_tier"
    assert phase0_service.status(TEST_USER_ID, COURSE)["state"] == "paid"


def test_finishing_unit_1_is_not_finishing_phase0(db):
    db.rows["student"][0]["tier"] = "tutor_tier"
    phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=None, band=None)
    _complete(db, "u-intro")
    out = phase0_service.status(TEST_USER_ID, COURSE)
    assert out["state"] == "paid" and out["phase0_completed_at"] is None


def test_finishing_the_alphabet_confirms_a_paid_seat_in_two_legal_hops(db):
    phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=None, band=None)
    _complete(db, "u-alpha")
    db.rows["student"][0]["tier"] = "intensive_tier"
    out = phase0_service.status(TEST_USER_ID, COURSE)
    assert out["state"] == "seat_confirmed"
    assert out["phase0_completed_at"].startswith("2026-11-03")
    assert out["target_intake"]["ethiopic_month"] == "hidar"  # finished by 5 Nov
    assert db.transitions == [("phase0", "paid"), ("paid", "seat_confirmed")]


def test_free_learner_who_finished_is_not_given_a_seat(db):
    phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=None, band=None)
    _complete(db, "u-alpha")
    out = phase0_service.status(TEST_USER_ID, COURSE)
    assert out["state"] == "phase0" and out["phase0_completed_at"]


def test_missed_deadline_rolls_forward(db):
    phase0_service.start(TEST_USER_ID, COURSE, pace_pledge_hours=None, band=None)
    with patch("app.services.phase0_service._today", return_value=date(2026, 11, 6)):
        out = phase0_service.status(TEST_USER_ID, COURSE)
    assert out["target_intake"]["ethiopic_month"] == "tahsas"


def test_later_states_are_left_alone(db):
    db.rows["enrolment_state"].append({
        "id": "e1", "student_id": TEST_USER_ID, "course_id": COURSE, "state": "in_pod",
        "created_at": "2026-10-01", "phase0_started_at": "2026-10-01", "phase0_completed_at": "2026-10-10",
        "target_intake_id": "hidar"})
    out = phase0_service.status(TEST_USER_ID, COURSE)
    assert out["state"] == "in_pod" and db.transitions == []


# ── routes ───────────────────────────────────────────────────────────────────

def test_routes_need_auth(client):
    assert client.get(f"/enrolment/me?course_id={COURSE}").status_code in (401, 403)
    assert client.post("/enrolment/start", json={"course_id": COURSE}).status_code in (401, 403)


def test_route_uses_the_jwt_caller(client, db):
    h = {"Authorization": f"Bearer {make_token()}"}
    resp = client.post("/enrolment/start", json={"course_id": COURSE, "pace_pledge_hours": 10}, headers=h)
    assert resp.status_code == 200
    assert db.rows["enrolment_state"][0]["student_id"] == TEST_USER_ID


@pytest.mark.parametrize("body", [
    {"course_id": "not-a-uuid"},
    {"course_id": COURSE, "pace_pledge_hours": 7},
    {"course_id": COURSE, "band": "africa"},
])
def test_route_rejects_bad_input(client, db, body):
    h = {"Authorization": f"Bearer {make_token()}"}
    assert client.post("/enrolment/start", json=body, headers=h).status_code == 422
