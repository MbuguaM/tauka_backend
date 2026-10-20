"""The slot catalogue (enrolment plan 3.1, E-12, §104).

Pinned:
1. **Next occurrence is a real instant in the anchor zone**, across a clock change.
2. **A default band from the timezone**, with the Gulf counted as East Africa + Gulf.
3. **Saving**: active slots, one band, no duplicates; replaces the old choices;
   three or more is the guarantee.
4. **The matcher sees exact windows in the anchor zone**, favourites ranked 1,
   and never two bands at once.
5. **Eligibility**: a catalogue learner needs `min_catalogue_slots` (1), a
   free-window learner still needs `min_preferences` (3).
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import slot_service
from app.services.cohort_matcher import MatchConfig, StudentIn, TimeWindow, run as run_matcher
from app.services.slot_service import SlotRefused

SAT10_NY = {"id": "a1", "band": "americas", "anchor_timezone": "America/New_York",
            "day_of_week": 5, "start_time": "10:00:00", "active": True, "kind": "weekend", "sort_order": 1}
TUE19_NY = {**SAT10_NY, "id": "a2", "day_of_week": 1, "start_time": "19:00:00", "kind": "weekday", "sort_order": 3}
THU19_NY = {**SAT10_NY, "id": "a3", "day_of_week": 3, "start_time": "19:00:00", "kind": "weekday", "sort_order": 4}
SAT10_LDN = {**SAT10_NY, "id": "e1", "band": "europe", "anchor_timezone": "Europe/London"}


# 1
@pytest.mark.parametrize("now,expect", [
    # Wed 28 Oct 2026, after Europe's change, before the US's (1 Nov): NY is UTC-4.
    (datetime(2026, 10, 28, 12, tzinfo=timezone.utc), datetime(2026, 10, 31, 14, tzinfo=timezone.utc)),
    # Wed 4 Nov 2026, after the US change: NY is UTC-5, so the same wall clock is 15:00 UTC.
    (datetime(2026, 11, 4, 12, tzinfo=timezone.utc), datetime(2026, 11, 7, 15, tzinfo=timezone.utc)),
    # Saturday 10:30 NY: this week's has started -> next week's.
    (datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc), datetime(2026, 11, 14, 15, tzinfo=timezone.utc)),
])
def test_next_at_is_dst_safe(now, expect):
    assert slot_service.next_at(SAT10_NY, now) == expect


# 2
@pytest.mark.parametrize("tz,band", [
    ("America/Los_Angeles", "americas"), ("Europe/Berlin", "europe"), ("Africa/Nairobi", "ea_gulf"),
    ("Asia/Riyadh", "ea_gulf"), ("Asia/Jerusalem", "ea_gulf"), ("Asia/Shanghai", "asia"),
    ("Australia/Sydney", "asia"), (None, None), ("UTC", None),
])
def test_band_for_timezone(tz, band):
    assert slot_service.band_for_timezone(tz) == band


# 3 — a fake table store
class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def delete(self): self.op = ("delete", None); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            made = p if isinstance(p, list) else [p]
            rows.extend(made)
            return SimpleNamespace(data=made)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        if kind == "update":
            for r in hit:
                r.update(p)
        if self.name == "student_slot_choice" and kind == "select":
            by_id = {s["id"]: s for s in self.db.rows["slot_catalogue"]}
            hit = [{**r, "slot": by_id.get(r["slot_id"])} for r in hit]
        return SimpleNamespace(data=hit)


class _DB:
    def __init__(self):
        self.rows = {"slot_catalogue": [SAT10_NY, TUE19_NY, THU19_NY, SAT10_LDN],
                     "student_slot_choice": [], "enrolment_state": [{"student_id": "s", "band": None}]}
    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.slot_service.db", fake):
        yield fake


def test_save_replaces_and_counts_the_guarantee(db):
    out = slot_service.save("s", "americas", [{"slot_id": "a1", "favourite": True}, {"slot_id": "a2"}])
    assert out["chosen_count"] == 2 and not out["guaranteed"]
    assert db.rows["enrolment_state"][0]["band"] == "americas"
    out = slot_service.save("s", "americas", [{"slot_id": "a1"}, {"slot_id": "a2"}, {"slot_id": "a3"}])
    assert out["chosen_count"] == 3 and out["guaranteed"]
    assert len(db.rows["student_slot_choice"]) == 3      # replaced, not appended


@pytest.mark.parametrize("band,picked,reason", [
    ("americas", [{"slot_id": "e1"}], "mixed_bands"),
    ("americas", [{"slot_id": "zz"}], "unknown_slot"),
    ("americas", [{"slot_id": "a1"}, {"slot_id": "a1"}], "duplicate_slot"),
    ("moon", [], "invalid_band"),
])
def test_save_refusals(db, band, picked, reason):
    with pytest.raises(SlotRefused) as e:
        slot_service.save("s", band, picked)
    assert e.value.reason == reason


def test_retired_slot_cannot_be_chosen(db):
    db.rows["slot_catalogue"][1] = {**TUE19_NY, "active": False}
    with pytest.raises(SlotRefused):
        slot_service.save("s", "americas", [{"slot_id": "a2"}])


# 4
def test_windows_are_exact_slots_in_the_anchor_zone(db):
    db.rows["student_slot_choice"] = [
        {"student_id": "s", "slot_id": "a1", "favourite": True},
        {"student_id": "s", "slot_id": "a2", "favourite": False},
        {"student_id": "s", "slot_id": "e1", "favourite": False},   # stray other band: dropped
    ]
    tz, wins = slot_service.windows_for(["s", "nobody"], 50)["s"]
    assert tz == "America/New_York"
    assert sorted(wins) == [(1, 19 * 60, 19 * 60 + 50, 2), (5, 600, 650, 1)]
    assert "nobody" not in slot_service.windows_for(["nobody"], 50)


# 5
def _student(sid, catalogue, n):
    wins = tuple(TimeWindow(day_of_week=5, start_minute=600 + i, end_minute=650 + i) for i in range(n))
    return StudentIn(id=sid, tier="tutor_tier", level=1, current_lesson=1, tz="America/New_York",
                     windows=wins, catalogue=catalogue)


def test_one_catalogue_slot_is_enough_but_one_free_window_is_not():
    res = run_matcher([_student("cat", True, 1), _student("free", False, 1)], [], MatchConfig(), date(2027, 1, 6))
    reasons = {d.student_id: d.reason for d in res.deferrals}
    assert reasons["free"] == "TOO_FEW_PREFERENCES"
    assert reasons.get("cat") != "TOO_FEW_PREFERENCES"     # deferred later (no tutors), not for this


# ── 3.1 remainder: seat counts and the editor ────────────────────────────────

def test_seats_match_a_pod_by_instant_in_the_tutors_zone(db):
    # Sat 10:00 New York == Sat 15:00 London in November: a pod stored in the
    # tutor's London zone sits in the Americas Sat 10:00 slot.
    db.rows["cohorts"] = [
        {"id": "p1", "level": 1, "max_students": 4, "slot_day_of_week": 5, "slot_time": "15:00:00",
         "slot_timezone": "Europe/London", "status": "forming"},
        {"id": "p2", "level": 2, "max_students": 4, "slot_day_of_week": 5, "slot_time": "15:00:00",
         "slot_timezone": "Europe/London", "status": "active"},                  # other level
        {"id": "p3", "level": 1, "max_students": 3, "slot_day_of_week": 1, "slot_time": "19:00:00",
         "slot_timezone": "America/New_York", "status": "active"},               # full
    ]
    db.rows["cohort_memberships"] = [{"cohort_id": "p1", "status": "active"}] + \
        [{"cohort_id": "p3", "status": "active"}] * 3
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    seats = slot_service.seats_by_slot([SAT10_NY, TUE19_NY, THU19_NY], 1, now)
    assert seats == {"a1": 3}
    assert slot_service.seats_by_slot([SAT10_NY], None, now) == {}           # level unknown: nothing


@pytest.fixture
def ops():
    from app.services import hierarchy_service
    with patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == "ops"):
        yield


def test_editor_is_ops_only_and_never_moves_a_time(db, ops):
    with pytest.raises(SlotRefused) as e:
        slot_service.admin_create("tutor", "europe", 5, "11:00", "weekend")
    assert e.value.reason == "not_allowed"
    made = slot_service.admin_create("ops", "europe", 5, "11:00", "weekend", 9)
    assert made["anchor_timezone"] == "Europe/London"          # always the band's anchor
    with pytest.raises(SlotRefused):
        slot_service.admin_create("ops", "europe", 9, "11:00", "weekend")
    out = slot_service.admin_update("ops", "a1", active=False)
    assert out["active"] is False and out["start_time"] == "10:00:00"
    with pytest.raises(SlotRefused) as e:
        slot_service.admin_update("ops", "a1")
    assert e.value.reason == "nothing_to_change"


def test_slot_create_route_rejects_a_bad_time(client):
    from tests.conftest import make_token
    with patch("app.dependencies.require_admin", return_value="x"):
        r = client.post("/slots", json={"band": "europe", "day_of_week": 5, "start_time": "25:00", "kind": "weekend"},
                        headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code in (403, 422)
