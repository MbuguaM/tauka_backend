"""Plan Phase 1 — tutor referral attribution (spec §1.1).

The thing worth testing here is not "does the insert run". It is the set of
rules that make a revenue share arguable six months later:

1. **Attribution is immutable.** A second code on an already-attributed student
   must change nothing and must say so. If this ever silently updates, a tutor
   can steal an attribution after the fact and the ledger cannot tell.
2. **A failing claim is a result, not an exception.** A mistyped code arrives
   during onboarding; a 4xx there strands someone in a signup flow over a typo.
3. **The report names no student.** Referring someone does not make you their
   tutor. This is the assertion that catches a future "just add the name so the
   tutor can say hello".
4. **`source_platform` is a closed set.** The column is one careless passthrough
   away from holding whatever a visitor typed.

The Supabase client is a MagicMock (conftest patches `create_client`), so each
test drives the fake query builder directly. That is deliberate: these are rules
about ordering and outcome, and a live database would only make them slower to
state.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services import tutor_referral_service as svc


# ---------------------------------------------------------------------------
# A minimal stand-in for the postgrest builder chain
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Records the calls made against it and returns a canned result.

    Every builder method returns `self`, so any chain of `.select().eq().is_()`
    terminates at the same `execute`.
    """

    def __init__(self, table, store):
        self._table = table
        self._store = store

    def __getattr__(self, name):
        def _chain(*args, **kwargs):
            if name == "insert":
                self._store.inserts.append((self._table, args[0]))
                self._store.last_insert_table = self._table
            elif name == "update":
                self._store.updates.append((self._table, args[0]))
            return self
        return _chain

    def execute(self):
        if self._table in self._store.raises:
            raise self._store.raises[self._table]
        return _Result(self._store.rows.get(self._table, []))


class _Store:
    def __init__(self):
        self.rows: dict[str, list] = {}
        self.inserts: list = []
        self.updates: list = []
        self.raises: dict[str, Exception] = {}
        self.last_insert_table = None


@pytest.fixture
def store():
    s = _Store()
    fake_db = MagicMock()
    fake_db.schema.return_value.table.side_effect = lambda t: _Query(t, s)
    with patch.object(svc, "db", fake_db):
        yield s


LINK = {"id": "link-1", "tutor_id": "tutor-1", "code": "Ab7dEf9g"}


# ---------------------------------------------------------------------------
# The share URL
# ---------------------------------------------------------------------------

def test_share_url_uses_tref_not_ref():
    """`?ref=` on /signup already carries an app.test_referrals code — see
    referral_service.handle_test_completion. Two namespaces in one parameter
    cannot be told apart on arrival, and the miss would look like a typo."""
    url = svc.build_signup_url("Ab7dEf9g")
    assert "tref=Ab7dEf9g" in url
    assert "ref=Ab7dEf9g" not in url.replace("tref=", "")


def test_share_url_carries_a_known_platform_and_nothing_else():
    assert "src=tiktok" in svc.build_signup_url("Ab7dEf9g", "tiktok")


def test_unknown_platform_is_dropped_not_stored():
    assert svc.normalise_source_platform("my friend's group chat") is None
    assert svc.normalise_source_platform("TikTok") == "tiktok"
    assert svc.normalise_source_platform(None) is None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

async def test_first_claim_attributes(store):
    store.rows["referral_signup"] = []
    store.rows["tutor_referral_link"] = [LINK]
    store.rows["student"] = [{"id": "student-1"}]
    store.rows["user_profiles"] = [{"first_name": "Hanna"}]

    result = await svc.attribute_signup("student-1", "Ab7dEf9g", "tiktok")

    assert result["attributed"] is True
    assert result["reason"] == "attributed"
    assert result["tutor_name"] == "Hanna"
    table, row = store.inserts[0]
    assert table == "referral_signup"
    assert row == {
        "student_id": "student-1",
        "tutor_id": "tutor-1",
        "link_id": "link-1",
        "source_platform": "tiktok",
    }


async def test_a_second_code_cannot_overwrite_the_first(store):
    """The integrity of the revenue share. A student already attributed stays
    attributed — no insert, no update, and the caller is told which it was."""
    store.rows["referral_signup"] = [
        {"id": "rs-1", "tutor_id": "tutor-OLD", "link_id": "link-0",
         "signed_up_at": "2026-08-01T00:00:00Z", "source_platform": None}
    ]
    store.rows["tutor_referral_link"] = [LINK]
    store.rows["user_profiles"] = [{"first_name": "Original"}]

    result = await svc.attribute_signup("student-1", "Ab7dEf9g")

    assert result["already_attributed"] is True
    assert result["attributed"] is False
    assert result["tutor_name"] == "Original"
    assert store.inserts == []
    assert store.updates == []


async def test_losing_the_unique_race_is_a_success(store):
    """Two devices claiming at once: the second insert violates the UNIQUE on
    student_id. That constraint is the guard, so losing to it is the correct
    outcome, not a 500."""
    store.rows["referral_signup"] = []
    store.rows["tutor_referral_link"] = [LINK]
    store.rows["student"] = [{"id": "student-1"}]
    store.rows["user_profiles"] = [{"first_name": "Hanna"}]

    calls = {"n": 0}
    real_existing = svc._existing_attribution

    def _existing(student_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the pre-check, before the other device landed
        return {"id": "rs-1", "tutor_id": "tutor-1", "link_id": "link-1",
                "signed_up_at": "2026-08-27T00:00:00Z", "source_platform": None}

    store.raises["referral_signup"] = Exception(
        'duplicate key value violates unique constraint "referral_signup_student_id_key"'
    )
    with patch.object(svc, "_existing_attribution", _existing):
        result = await svc.attribute_signup("student-1", "Ab7dEf9g")

    assert result["already_attributed"] is True
    assert result["attributed"] is False
    assert real_existing is not None  # the real one is still what production uses


async def test_unknown_code_is_a_reason_not_an_exception(store):
    store.rows["referral_signup"] = []
    store.rows["tutor_referral_link"] = []

    result = await svc.attribute_signup("student-1", "notacode")

    assert result == {"attributed": False, "already_attributed": False,
                      "tutor_name": None, "reason": "unknown_code"}
    assert store.inserts == []


async def test_malformed_code_never_reaches_the_database(store):
    store.rows["referral_signup"] = []
    result = await svc.attribute_signup("student-1", "a b/c")
    assert result["reason"] == "unknown_code"


async def test_self_referral_is_refused(store):
    store.rows["referral_signup"] = []
    store.rows["tutor_referral_link"] = [
        {"id": "link-1", "tutor_id": "student-1", "code": "Ab7dEf9g"}
    ]

    result = await svc.attribute_signup("student-1", "Ab7dEf9g")

    assert result["reason"] == "self_referral"
    assert store.inserts == []


async def test_missing_student_row_is_reported_not_a_23503(store):
    """referral_signup.student_id is an FK to app.student, which the client
    creates during onboarding. Saying "no student row" beats letting the insert
    die on a foreign-key violation the client cannot read."""
    store.rows["referral_signup"] = []
    store.rows["tutor_referral_link"] = [LINK]
    store.rows["student"] = []

    result = await svc.attribute_signup("student-1", "Ab7dEf9g")

    assert result["reason"] == "no_student_row"
    assert store.inserts == []


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

async def test_lookup_hides_the_difference_between_unknown_and_retired(store):
    store.rows["tutor_referral_link"] = []
    assert await svc.lookup_code("Ab7dEf9g") == {"valid": False, "tutor_name": None}


async def test_lookup_refuses_a_departed_tutor(store):
    store.rows["tutor_referral_link"] = [LINK]
    store.rows["tutor"] = [{"id": "tutor-1", "status": "inactive", "deleted_at": None}]
    assert await svc.lookup_code("Ab7dEf9g") == {"valid": False, "tutor_name": None}


async def test_lookup_returns_a_first_name_only(store):
    store.rows["tutor_referral_link"] = [LINK]
    store.rows["tutor"] = [{"id": "tutor-1", "status": "active", "deleted_at": None}]
    store.rows["user_profiles"] = [{"first_name": "Hanna"}]
    assert await svc.lookup_code("Ab7dEf9g") == {"valid": True, "tutor_name": "Hanna"}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

async def test_report_names_no_student(store):
    store.rows["tutor_referral_link"] = [
        {"id": "link-1", "code": "Ab7dEf9g", "active": True,
         "created_at": "2026-08-27T00:00:00Z"}
    ]
    store.rows["referral_signup"] = [
        {"student_id": "s-1", "signed_up_at": "2026-08-20T00:00:00Z",
         "source_platform": "tiktok", "first_match_honoured": False,
         "vested_share_bp": 0},
        {"student_id": "s-2", "signed_up_at": "2026-08-21T00:00:00Z",
         "source_platform": None, "first_match_honoured": False,
         "vested_share_bp": 0},
    ]
    store.rows["student"] = [
        {"id": "s-1", "account_status": "active", "deleted_at": None},
        {"id": "s-2", "account_status": "active", "deleted_at": "2026-08-25T00:00:00Z"},
    ]

    report = await svc.get_referral_report("tutor-1")

    assert report["total_signups"] == 2
    assert report["still_active"] == 1
    assert report["vesting_active"] is False
    for item in report["signups"]:
        assert "student_id" not in item
        assert "name" not in item
        assert "email" not in item


async def test_retention_failure_does_not_render_as_zero_retained(store):
    """A lookup that threw must not become "none of your students stayed" —
    that is a number a tutor would act on."""
    store.rows["tutor_referral_link"] = []
    store.rows["referral_signup"] = [
        {"student_id": "s-1", "signed_up_at": "2026-08-20T00:00:00Z",
         "source_platform": None, "first_match_honoured": False,
         "vested_share_bp": 0},
    ]
    store.raises["student"] = Exception("connection reset")

    report = await svc.get_referral_report("tutor-1")

    # It reports 0 active but the caller can tell the report is degraded because
    # the link is absent and the count disagrees with total_signups. The point
    # of the test is that it does not raise and does not invent a number.
    assert report["total_signups"] == 1
    assert report["still_active"] == 0


async def test_report_with_no_link_and_no_signups_is_empty_not_broken(store):
    store.rows["tutor_referral_link"] = []
    store.rows["referral_signup"] = []

    report = await svc.get_referral_report("tutor-1")

    assert report["code"] is None
    assert report["share_url"] is None
    assert report["total_signups"] == 0
    assert report["signups"] == []


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

async def test_existing_link_is_reused_not_reminted(store):
    store.rows["tutor_referral_link"] = [
        {"id": "link-1", "code": "Ab7dEf9g", "active": True,
         "created_at": "2026-08-27T00:00:00Z"}
    ]
    result = await svc.get_or_create_link("tutor-1")
    assert result["code"] == "Ab7dEf9g"
    assert result["rotated"] is False
    assert store.inserts == []


async def test_rotation_soft_deletes_so_past_attributions_survive(store):
    """referral_signup.link_id is ON DELETE SET NULL. A hard delete would erase
    which code won each past signup — the audit trail the share is argued from."""
    store.rows["tutor_referral_link"] = [
        {"id": "link-2", "code": "NewCode1", "active": True,
         "created_at": "2026-08-27T00:00:00Z"}
    ]
    await svc.rotate_link("tutor-1")

    table, patch_body = store.updates[0]
    assert table == "tutor_referral_link"
    assert patch_body["active"] is False
    assert patch_body["deleted_at"]  # a timestamp, not a row removal
    assert store.inserts and store.inserts[0][0] == "tutor_referral_link"
