"""Heritage test-outs, tutor rungs and rehearsal intakes (enrolment plan §2k, §99).

Pinned (developer decisions 2026-10-18):
1. **Who may send a request:** the learner, a rung-4 tutor, an owner, an ops
   admin. Nobody else.
2. **Who assesses:** a LINKED tutor at rung >= 2, or an ops admin. A rung-1
   reviewer can't, and an unlinked tutor can't.
3. **A new guest link is ops-only.** A rung-4 tutor may assign only a tutor the
   learner already has.
4. **The test-out's own guest link ends when the request closes.**
5. **A pass finishes Phase 0** (`phase0_completed_via = 'test_out'`) and, for a
   paid learner, confirms the seat through the normal sync.
6. **Rehearsal intakes are never targeted** (Hidar 2019 is simulated).
7. Rung refusals from SQL surface as stable words.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hierarchy_service, phase0_service, test_out_service
from app.services.hierarchy_service import HierarchyRefused
from app.services.test_out_service import TestOutRefused
from tests.conftest import make_token

COURSE = "11111111-1111-1111-1111-111111111111"
STUDENT = "00000000-0000-0000-0000-0000000000a1"
OWNER = "00000000-0000-0000-0000-0000000000b1"
OPS = "00000000-0000-0000-0000-0000000000b2"
SENIOR = "00000000-0000-0000-0000-0000000000c4"
TUTOR2 = "00000000-0000-0000-0000-0000000000c2"
TUTOR1 = "00000000-0000-0000-0000-0000000000c1"
STRANGER = "00000000-0000-0000-0000-0000000000ff"

CAPS = {OWNER: {"owner", "ops"}, OPS: {"ops"}}
RUNG = {SENIOR: 4, TUTOR2: 2, TUTOR1: 1}

INTAKES = [
    {"id": "hidar", "ethiopic_month": "hidar", "ec_year": 2019, "formation_date": "2026-11-10",
     "cutoff_opens": "2026-11-01", "cutoff_closes": "2026-11-05", "tier": "founding",
     "status": "scheduled", "mode": "rehearsal", "season": {"display_name": "Hidar 2019 (founding)"}},
    {"id": "tahsas", "ethiopic_month": "tahsas", "ec_year": 2019, "formation_date": "2026-12-10",
     "cutoff_opens": "2026-12-01", "cutoff_closes": "2026-12-05", "tier": "light",
     "status": "scheduled", "mode": "live", "season": {"display_name": "Bega 2019"}},
]


# ── a small fake of the PostgREST builder ────────────────────────────────────

class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.f.append(lambda r: r.get(c) is None); return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            if self.name == "test_out_request" and any(
                r["student_id"] == p["student_id"] and r["status"] in ("requested", "assigned") for r in rows
            ):
                raise Exception("duplicate key value violates unique constraint idx_test_out_one_open")
            row = {"id": f"{self.name}-{len(rows)+1}", "created_at": "2026-10-18T00:00:00+00:00",
                   "status": "requested", "assessor_tutor_id": None, "assessor_link_id": None,
                   "target_intake_id": None, "phase0_completed_at": None, **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        return SimpleNamespace(data=hit)


class _DB:
    def __init__(self):
        self.rows = {
            "student": [{"id": STUDENT, "tier": "tutor_tier"}],
            "tutor": [{"id": t, "status": "active", "deleted_at": None} for t in (SENIOR, TUTOR2, TUTOR1)],
            "intake": [dict(r) for r in INTAKES],
            "unit": [], "unit_progress": [], "enrolment_state": [], "test_out_request": [],
            "student_tutor_link": [],
        }

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()

    def linked(student):
        return {r["tutor_id"] for r in fake.rows["student_tutor_link"]
                if r["student_id"] == student and r["state"] == "active"}

    async def create_link(student, tutor, role, source):
        row = {"id": f"link-{len(fake.rows['student_tutor_link'])+1}", "student_id": student,
               "tutor_id": tutor, "role": role, "source": source, "state": "active"}
        fake.rows["student_tutor_link"].append(row)
        return {"created": True, "link": row}

    with patch("app.services.test_out_service.db", fake), \
         patch("app.services.phase0_service.db", fake), \
         patch("app.services.phase0_service._today", return_value=date(2026, 10, 18)), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: c in CAPS.get(a, set())), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: RUNG.get(t)), \
         patch.object(hierarchy_service, "linked_tutor_ids", side_effect=linked), \
         patch("app.services.test_out_service.matching_service.create_link", side_effect=create_link) as cl:
        fake.create_link = cl
        yield fake


def _link(db, tutor, link_id="pre-1"):
    db.rows["student_tutor_link"].append(
        {"id": link_id, "student_id": STUDENT, "tutor_id": tutor, "role": "home", "source": "matched", "state": "active"})


# ── 1. who may request ───────────────────────────────────────────────────────

@pytest.mark.parametrize("actor,expect", [
    (STUDENT, "learner"), (OWNER, "owner"), (OPS, "ops"), (SENIOR, "senior_tutor"),
    (TUTOR2, None), (STRANGER, None),
])
def test_requester_role(db, actor, expect):
    assert test_out_service.requester_role(actor, STUDENT) == expect


async def test_rung2_tutor_cannot_send_a_request(db):
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.create(TUTOR2, STUDENT, COURSE)
    assert e.value.reason == "not_allowed"


async def test_learner_request_starts_phase0_and_opens_one_request(db):
    row = await test_out_service.create(STUDENT, STUDENT, COURSE)
    assert row["requested_as"] == "learner" and row["status"] == "requested"
    # Started Phase 0; the learner is on a paid tier, so the sync moves them on.
    assert db.rows["enrolment_state"][0]["state"] == "paid"
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.create(OPS, STUDENT, COURSE)
    assert e.value.reason == "already_open"


async def test_no_test_out_after_phase0_is_done(db):
    db.rows["enrolment_state"].append({"id": "es", "student_id": STUDENT, "course_id": COURSE,
                                        "state": "seat_confirmed", "phase0_completed_at": "2026-10-10"})
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.create(STUDENT, STUDENT, COURSE)
    assert e.value.reason == "phase0_already_done"


# ── 2/3. assigning an assessor ───────────────────────────────────────────────

async def test_ops_assigning_an_unlinked_tutor_makes_a_guest_link(db):
    row = await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    assert row["status"] == "assigned" and row["assessor_tutor_id"] == TUTOR2
    assert row["assessor_link_id"] == "link-1"
    db.create_link.assert_called_once_with(STUDENT, TUTOR2, "guest", "admin")


async def test_senior_may_not_create_a_link(db):
    row = await test_out_service.create(SENIOR, STUDENT, COURSE)
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.assign(SENIOR, row["id"], TUTOR2)
    assert e.value.reason == "ops_required_for_new_link"


async def test_senior_may_assign_an_already_linked_tutor(db):
    _link(db, TUTOR2)
    row = await test_out_service.create(SENIOR, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    assert row["status"] == "assigned" and row["assessor_link_id"] is None


async def test_rung1_cannot_assess(db):
    row = await test_out_service.create(OPS, STUDENT, COURSE)
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.assign(OPS, row["id"], TUTOR1)
    assert e.value.reason == "assessor_rung"


async def test_inactive_tutor_cannot_assess(db):
    db.rows["tutor"][1]["status"] = "suspended"
    row = await test_out_service.create(OPS, STUDENT, COURSE)
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.assign(OPS, row["id"], TUTOR2)
    assert e.value.reason == "assessor_inactive"


async def test_learner_cannot_pick_their_assessor(db):
    row = await test_out_service.create(STUDENT, STUDENT, COURSE)
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.assign(STUDENT, row["id"], TUTOR2)
    assert e.value.reason == "not_allowed"


# ── 4/5. results ─────────────────────────────────────────────────────────────

async def test_pass_by_linked_assessor_finishes_phase0_and_ends_the_link(db):
    row = await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    out = test_out_service.record_result(TUTOR2, row["id"], "passed", [])
    assert out["status"] == "passed" and out["result_by"] == TUTOR2
    es = db.rows["enrolment_state"][0]
    assert es["phase0_completed_via"] == "test_out" and es["phase0_completed_at"]
    # Paid learner → the sync confirms the seat (two legal hops).
    assert es["state"] == "seat_confirmed"
    assert db.rows["student_tutor_link"][0]["state"] == "ended"


async def test_not_yet_keeps_phase0_open_and_records_codes_only(db):
    row = await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    out = test_out_service.record_result(TUTOR2, row["id"], "not_yet", ["ejectives", "gemination"])
    assert out["gap_codes"] == ["ejectives", "gemination"]
    assert db.rows["enrolment_state"][0]["phase0_completed_at"] is None
    with pytest.raises(TestOutRefused):
        test_out_service.record_result(TUTOR2, row["id"], "passed", [])   # final


async def test_free_text_gap_is_refused(db):
    row = await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    with pytest.raises(TestOutRefused) as e:
        test_out_service.record_result(TUTOR2, row["id"], "not_yet", ["struggled with ጸ"])
    assert e.value.reason == "invalid_gap_code"


async def test_someone_elses_assessment_is_refused(db):
    _link(db, SENIOR, "pre-senior")
    row = await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    # Linked and rung 4, but not the assigned assessor and not ops.
    with pytest.raises(TestOutRefused) as e:
        test_out_service.record_result(SENIOR, row["id"], "passed", [])
    assert e.value.reason == "not_allowed"


async def test_ops_admin_may_record_the_result(db):
    row = await test_out_service.create(OWNER, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    assert test_out_service.record_result(OPS, row["id"], "passed", [])["status"] == "passed"


async def test_cancel_by_learner_ends_the_link_and_strangers_cannot(db):
    row = await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    with pytest.raises(TestOutRefused):
        test_out_service.cancel(STRANGER, row["id"])
    assert test_out_service.cancel(STUDENT, row["id"])["status"] == "cancelled"
    assert db.rows["student_tutor_link"][0]["state"] == "ended"


# ── 6. rehearsal intakes ─────────────────────────────────────────────────────

def test_rehearsal_intake_is_never_targeted():
    got = phase0_service.choose_target(INTAKES, started=date(2026, 10, 18), today=date(2026, 10, 18), completed=None)
    assert got["id"] == "tahsas"


def test_finishing_by_lessons_records_the_route(db):
    db.rows["unit"] = [{"id": "u1", "course_id": COURSE, "is_preliminary": True, "unit_order": 1},
                       {"id": "u2", "course_id": COURSE, "is_preliminary": True, "unit_order": 2}]
    db.rows["unit_progress"] = [{"unit_id": "u2", "user_id": STUDENT, "status": "completed",
                                 "completed_at": "2026-11-03T10:00:00+00:00"}]
    out = phase0_service.start(STUDENT, COURSE, pace_pledge_hours=None, band=None)
    assert out["phase0_completed_via"] == "lessons" and out["state"] == "seat_confirmed"


# ── 7. rungs ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sql_error,word", [
    ("tutor_rung: only an owner may promote to or demote from rung 4", "owner_required"),
    ("tutor_rung: only an ops admin may set rungs 1-3", "ops_required"),
    ("tutor_rung: nobody sets their own rung", "not_self"),
    ("tutor_rung: no such tutor", "no_such_tutor"),
])
def test_rung_refusals_become_words(sql_error, word):
    fake = SimpleNamespace(schema=lambda _: SimpleNamespace(
        rpc=lambda *a, **k: SimpleNamespace(execute=lambda: (_ for _ in ()).throw(Exception(sql_error)))))
    with patch("app.services.hierarchy_service.db", fake):
        with pytest.raises(HierarchyRefused) as e:
            hierarchy_service.set_rung(OPS, TUTOR2, 4, "anchor")
    assert e.value.reason == word


def test_rung_needs_a_reason():
    with pytest.raises(HierarchyRefused) as e:
        hierarchy_service.set_rung(OPS, TUTOR2, 2, "  ")
    assert e.value.reason == "reason_required"


# ── routes ───────────────────────────────────────────────────────────────────

def test_route_maps_not_allowed_to_403(client):
    with patch("app.services.test_out_service.create",
               new=AsyncMock(side_effect=TestOutRefused("not_allowed"))):
        r = client.post("/test-outs", json={"course_id": COURSE, "student_id": STUDENT},
                        headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "not_allowed"


def test_route_rejects_free_text_gap_codes(client):
    r = client.post("/test-outs/x/result", json={"result": "not_yet", "gap_codes": ["anything"]},
                    headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 422


# ── screens' backend: overview, candidates, assessors, emails ────────────────

def _profiles(db):
    db.rows["user_profiles"] = [
        {"id": STUDENT, "first_name": "Selam", "last_name": "T"},
        {"id": TUTOR2, "first_name": "Marta", "last_name": None},
        {"id": SENIOR, "first_name": "Abebe", "last_name": "K"},
    ]


async def test_overview_names_and_powers(db):
    _profiles(db)
    await test_out_service.create(OPS, STUDENT, COURSE, assessor_tutor_id=TUTOR2)
    ops = test_out_service.overview(OPS)
    assert ops["can_send"] and ops["can_link"]
    assert ops["requests"][0]["student_name"] == "Selam T" and ops["requests"][0]["assessor_name"] == "Marta"
    tut = test_out_service.overview(TUTOR2)
    assert not tut["can_send"] and tut["my_rung"] == 2 and len(tut["requests"]) == 1
    sen = test_out_service.overview(SENIOR)
    assert sen["can_send"] and not sen["can_link"]


async def test_candidates_ops_all_senior_own_none_with_open_request(db):
    _profiles(db)
    other = "00000000-0000-0000-0000-0000000000a2"
    db.rows["student"].append({"id": other, "tier": "free_tier"})
    db.rows["enrolment_state"] += [
        {"id": "e1", "student_id": STUDENT, "course_id": COURSE, "state": "phase0", "phase0_completed_at": None},
        {"id": "e2", "student_id": other, "course_id": COURSE, "state": "paid", "phase0_completed_at": None},
    ]
    assert {c["student_id"] for c in test_out_service.candidates(OPS)} == {STUDENT, other}
    _link(db, SENIOR)  # SENIOR is STUDENT's tutor
    assert [c["student_id"] for c in test_out_service.candidates(SENIOR)] == [STUDENT]
    await test_out_service.create(OPS, other, COURSE)
    assert [c["student_id"] for c in test_out_service.candidates(OPS)] == [STUDENT]
    with pytest.raises(TestOutRefused):
        test_out_service.candidates(TUTOR2)


async def test_assessors_ops_sees_all_rung2_senior_sees_linked(db):
    _profiles(db)
    _link(db, TUTOR2)
    row = await test_out_service.create(OPS, STUDENT, COURSE)
    ops = test_out_service.assessors(OPS, row["id"])
    assert [(t["tutor_id"], t["linked"]) for t in ops] == [(TUTOR2, True), (SENIOR, False)]  # rung-1 excluded
    sen = test_out_service.assessors(SENIOR, row["id"])
    assert [t["tutor_id"] for t in sen] == [TUTOR2]


async def test_staff_request_without_course_uses_the_learners_only_course(db):
    db.rows["student_course"] = [{"student_id": STUDENT, "course_id": COURSE}]
    row = await test_out_service.create(OPS, STUDENT, None)
    assert row["course_id"] == COURSE
    db.rows["student_course"].append({"student_id": STUDENT, "course_id": "22222222-2222-2222-2222-222222222222"})
    test_out_service.cancel(OPS, row["id"])
    with pytest.raises(TestOutRefused) as e:
        await test_out_service.create(OPS, STUDENT, None)
    assert e.value.reason == "course_required"


async def test_learner_is_not_emailed_about_their_own_request():
    with patch("app.services.test_out_service.email_service.send_template_email", new=AsyncMock()) as send, \
         patch("app.services.test_out_service._email_of", return_value="x@example.com"):
        await test_out_service.notify_learner({"student_id": STUDENT, "requested_as": "learner"})
        send.assert_not_called()
        await test_out_service.notify_learner({"student_id": STUDENT, "requested_as": "ops"})
        assert send.call_args.args[1] == "test_out"


@pytest.mark.parametrize("audience", ["learner", "assessor"])
def test_email_template_renders(audience):
    from app.services.email_service import _jinja_env
    html = _jinja_env.get_template("test_out.html").render(
        subject="s", audience=audience, student_name="Selam", web_base_url="https://tauka.io")
    assert "test-out" in html.lower() and "Selam" in html if audience == "assessor" else "Phase 0" in html


# ── §102: the enrolment record follows matching ──────────────────────────────

def test_advance_moves_only_from_the_named_states(db):
    db.rows["enrolment_state"] = [
        {"id": "e1", "student_id": STUDENT, "course_id": COURSE, "state": "seat_confirmed"},
    ]
    assert phase0_service.advance(STUDENT, ("seat_confirmed", "wait_pool"), "matched") == 1
    assert db.rows["enrolment_state"][0]["state"] == "matched"
    # Not in a from-state: untouched (an accept for someone never offered).
    assert phase0_service.advance(STUDENT, ("seat_confirmed",), "wait_pool") == 0
    assert phase0_service.advance(STUDENT, ("matched",), "in_pod") == 1
    assert db.rows["enrolment_state"][0]["state"] == "in_pod"


def test_advance_never_raises_when_the_database_refuses(db):
    db.rows["enrolment_state"] = [{"id": "e1", "student_id": STUDENT, "course_id": COURSE, "state": "matched"}]

    def refuse(*a, **k):
        raise Exception("enrolment_state: matched -> phase0 is not a legal transition")
    with patch.object(_Q, "update", refuse):
        assert phase0_service.advance(STUDENT, ("matched",), "in_pod") == 0
