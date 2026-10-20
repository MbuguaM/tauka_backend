"""Fidel approval (enrolment plan 1.4, §107).

Pinned:
1. No submission without a current `tutor_voice_review` consent; withdrawing is a
   new row and blocks the next submission.
2. Only the learner's own fidelReading submission can be claimed; a new one
   supersedes a reading still waiting for the same group.
3. Reviewers are active rung >= 1 tutors or ops; redo carries codes, approve none;
   a verdict is final.
4. A pending reading whose audio was swept closes `expired` and leaves the queue.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import fidel_service, hierarchy_service
from app.services.fidel_service import FidelRefused

STU, REV, RUNG0, OPS = "s1", "r1", "r0", "ops"
UNIT = "u-alpha"
RUNG = {REV: 1}


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op = db, name, [], ("select", None)

    def select(self, *a, **k): self.op = ("select", None); return self
    def order(self, c, desc=False): self.desc = (c, desc); return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind == "insert":
            self.db.n += 1
            row = {"id": f"{self.name}-{self.db.n}", "status": "pending", "created_at": f"t{self.db.n:03d}",
                   "at": f"t{self.db.n:03d}", "gap_codes": [], **p}
            rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        if getattr(self, "desc", None):
            col, desc = self.desc
            hit = sorted(hit, key=lambda r: str(r.get(col)), reverse=desc)
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.n = 0
        self.rows = {
            "consent_record": [], "fidel_approval": [],
            "speech_submission": [
                {"id": "sub1", "user_id": STU, "section_kind": "fidelReading", "storage_path": f"speech/{STU}/a.zip"},
                {"id": "sub2", "user_id": STU, "section_kind": "fidelReading", "storage_path": f"speech/{STU}/b.zip"},
                {"id": "subx", "user_id": STU, "section_kind": "pronunciationDrills", "storage_path": f"speech/{STU}/x.zip"},
            ],
            "tutor": [{"id": REV, "status": "active", "deleted_at": None},
                      {"id": RUNG0, "status": "active", "deleted_at": None}],
            "user_profiles": [{"id": STU, "first_name": "Selam", "last_name": None}],
        }
        self.storage = SimpleNamespace(from_=lambda b: SimpleNamespace(
            create_signed_url=lambda path, s: {"signedURL": f"https://signed/{path}"}))

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.fidel_service.db", fake), patch("app.services.makeup_service.db", fake), \
         patch.object(hierarchy_service, "admin_can", side_effect=lambda a, c: a == OPS and c == "ops"), \
         patch.object(hierarchy_service, "rung_of", side_effect=lambda t: RUNG.get(t)):
        yield fake


# 1
def test_consent_required_and_withdrawable(db):
    with pytest.raises(FidelRefused) as e:
        fidel_service.submit(STU, UNIT, 0, "First Order", f"speech/{STU}/a.zip")
    assert e.value.reason == "consent_required"
    fidel_service.set_consent(STU, True)
    assert fidel_service.submit(STU, UNIT, 0, "First Order", f"speech/{STU}/a.zip")["status"] == "pending"
    fidel_service.set_consent(STU, False)
    assert not fidel_service.has_consent(STU)
    assert db.rows["consent_record"][-1]["source"] == "in_app"


# 2
def test_claim_rules_and_supersede(db):
    fidel_service.set_consent(STU, True)
    with pytest.raises(FidelRefused):
        fidel_service.submit(STU, UNIT, 0, None, f"speech/{STU}/x.zip")          # not a fidel reading
    with pytest.raises(FidelRefused):
        fidel_service.submit("other", UNIT, 0, None, f"speech/{STU}/a.zip")      # not theirs
    first = fidel_service.submit(STU, UNIT, 0, None, f"speech/{STU}/a.zip")
    fidel_service.submit(STU, UNIT, 0, None, f"speech/{STU}/b.zip")
    assert next(r for r in db.rows["fidel_approval"] if r["id"] == first["id"])["status"] == "superseded"
    groups = fidel_service.mine(STU, UNIT)["groups"]
    assert len(groups) == 1 and groups[0]["status"] == "pending"


# 3
def test_reviewers_and_verdicts(db):
    fidel_service.set_consent(STU, True)
    a = fidel_service.submit(STU, UNIT, 1, "Orders II & III", f"speech/{STU}/a.zip")
    for actor in (RUNG0, STU):
        with pytest.raises(FidelRefused):
            fidel_service.queue(actor)
    q = fidel_service.queue(REV)
    assert q[0]["student_name"] == "Selam" and q[0]["audio_url"] == f"https://signed/speech/{STU}/a.zip"
    with pytest.raises(FidelRefused) as e:
        fidel_service.review(REV, a["id"], True, ["ejectives"])
    assert e.value.reason == "codes_only_on_redo"
    out = fidel_service.review(REV, a["id"], False, ["ejectives", "gemination"])
    assert out["status"] == "redo" and out["gap_codes"] == ["ejectives", "gemination"]
    with pytest.raises(FidelRefused) as e:
        fidel_service.review(OPS, a["id"], True, [])
    assert e.value.reason == "decided"


# 4
def test_swept_audio_expires_the_reading(db):
    fidel_service.set_consent(STU, True)
    a = fidel_service.submit(STU, UNIT, 2, None, f"speech/{STU}/a.zip")
    next(r for r in db.rows["fidel_approval"] if r["id"] == a["id"])["submission_id"] = None
    assert fidel_service.queue(OPS) == []
    assert next(r for r in db.rows["fidel_approval"] if r["id"] == a["id"])["status"] == "expired"


def test_route_validates_the_path(client):
    from tests.conftest import make_token
    r = client.post("/fidel/submissions", json={"unit_id": "1" * 8 + "-1111-1111-1111-" + "1" * 12,
                                                 "group_index": 0, "storage_path": "../etc/passwd"},
                    headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 422
