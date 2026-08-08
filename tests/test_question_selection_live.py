"""
Placement-test question selection, verified against the LIVE question bank.

WHY THIS IS A LIVE TEST AND NOT A MOCKED ONE
--------------------------------------------
The defects this guards against are all shape mismatches between what the bank
actually contains and what the renderers expect, and a mock is built from the
same assumption as the code — it agrees with whatever the author believed. The
2026-08-06 rebuild found 20 such defects (`sessions.md`), every one invisible to
a unit test with a hand-written fixture: `cefr_level: "B1+"` matching no filter,
options nested where nothing looked for them, `_to_client_shape` handing the
renderer a row with no `interaction.mode`. Only real rows catch those.

It is therefore SKIPPED BY DEFAULT. Run it deliberately:

    TAUKA_LIVE_DB=1 pytest tests/test_question_selection_live.py -v

It is read-only — it selects and hydrates questions but never creates a session
row, so it leaves no trace in `app.test_sessions`.

NOTE ON `conftest.py`: it patches `supabase.create_client` at module load for
the whole suite, so `test_service.db` is a MagicMock by the time this file is
imported. The `live_db` fixture builds a real service-role client and points
`test_service.db` at it for the duration — which is also why nothing here may
run in parallel with a test that relies on the mock.
"""
import json
import os

import pytest

from app.services import test_service as ts

LANGUAGE = "amharic"

# Modes `QuestionBody` in tauka-react-web/src/routes/test-question.jsx can
# actually dispatch on. A question whose mode is absent here renders as
# "This question type (unknown) can't be displayed".
RENDERABLE_MODES = {
    "choice", "multi_choice", "match", "bucket",
    "select_token", "order", "rank", "build",
}

# Every key the renderers read. `_to_client_shape` builds this from two places
# — top-level columns and the `content` jsonb — so a None here means hydration
# dropped something.
REQUIRED_CLIENT_KEYS = (
    "id", "language", "skill_area", "cefr_level",
    "question_type", "tag", "prompt", "stimulus", "interaction",
)

# Must never reach the browser. `correct_answer` is the key; the other two
# narrate it closely enough to leak it by implication, and `content` is the
# storage shape the client must never see.
FORBIDDEN_CLIENT_KEYS = {
    "correct_answer", "explanation", "distractor_rationale", "content",
}

RUNS = int(os.getenv("TAUKA_LIVE_RUNS", "10"))

pytestmark = [
    pytest.mark.live_db,
    pytest.mark.skipif(
        not os.getenv("TAUKA_LIVE_DB"),
        reason="live DB test; set TAUKA_LIVE_DB=1 to run",
    ),
]


@pytest.fixture(scope="module")
def live_db():
    """Point `test_service.db` at a real service-role client, then restore it."""
    # conftest patches `supabase.create_client` — the re-export on the package —
    # so import from the module that DEFINES it, which the patch never touched.
    # Do not try to unwrap the mock instead: `getattr(mock, "__wrapped__", None)`
    # auto-creates the attribute and hands back another MagicMock, which is
    # truthy, so every assertion below silently runs against a mock and the
    # failures blame the code under test.
    from supabase._sync.client import create_client
    from app.config import settings

    if not settings.supabase_url or not settings.supabase_key:
        pytest.skip("SUPABASE_URL / SUPABASE_KEY not configured")

    try:
        client = create_client(settings.supabase_url, settings.supabase_key)
        probe = client.schema("app").table("test_questions").select(
            "id"
        ).limit(1).execute()
    except Exception as exc:                     # unreachable host, bad key, …
        pytest.skip(f"live Supabase unavailable: {exc}")

    # Belt and braces: a mocked client sails through the probe above.
    assert isinstance(probe.data, list), (
        f"expected a real PostgREST response, got {type(probe.data).__name__} — "
        "the supabase client is still mocked"
    )

    original = ts.db
    ts.db = client
    yield client
    ts.db = original


@pytest.fixture(scope="module")
def pool(live_db):
    rows = ts._fetch_selection_pool(LANGUAGE)
    if not rows:
        pytest.skip(f"no active questions for '{LANGUAGE}'")
    return rows


# ---------------------------------------------------------------------------
# The fetch is column-scoped
# ---------------------------------------------------------------------------

def test_selection_pool_fetches_only_selection_columns(pool):
    """
    Selection reads four columns; `select *` downloaded 97% of its bytes to throw
    them away (168 kB vs 4.5 kB measured on 158 rows). Asserting the exact key
    set is what stops a future `select("*")` creeping back in unnoticed.
    """
    expected = {"id", "cefr_level", "skill_area", "question_type"}
    offenders = [r for r in pool if set(r) != expected]
    assert not offenders, f"selection pool carrying extra columns: {offenders[:3]}"


def test_selection_pool_is_not_row_limited(pool, live_db):
    """
    Bounding by column is safe; bounding by row was not. An earlier version
    capped candidates at 2 (Phase 1) and 5 (Phase 2) with no ORDER BY, so the
    same handful of questions circulated no matter how large the bank grew.
    """
    total = live_db.schema("app").table("test_questions").select(
        "id", count="exact"
    ).eq("language", LANGUAGE).eq("active", True).execute().count
    assert len(pool) == total, "pool is not every active question"


# ---------------------------------------------------------------------------
# Hydration preserves what selection decided
# ---------------------------------------------------------------------------

def test_hydration_preserves_order_and_completeness(pool):
    """
    `_interleave_modes` spends real effort spacing modes out, and PostgREST makes
    no ordering promise for an `in_` filter — so hydration must restore the
    selected order rather than trust the response.
    """
    selected = ts._select_phase1_questions(LANGUAGE, pool=pool)
    hydrated = ts._hydrate(selected)

    assert [q["id"] for q in hydrated] == [q["id"] for q in selected]
    assert all(q.get("content") for q in hydrated), "hydration returned no content"


def test_hydrate_empty_selection_makes_no_request():
    assert ts._hydrate([]) == []


# ---------------------------------------------------------------------------
# The served payload is renderable, and leaks nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("run", range(RUNS))
def test_full_run_is_renderable_and_leaks_no_answers(pool, run):
    """One whole sitting: both phases, hydrated, shuffled, shaped for the wire."""
    phase1 = ts._select_phase1_questions(LANGUAGE, pool=pool)
    phase2 = ts._select_phase2_questions(
        LANGUAGE, "A2", count=ts.PHASE_2_COUNT,
        exclude_ids={q["id"] for q in phase1}, pool=pool,
    )

    assert len(phase1) == ts.PHASE_1_COUNT
    assert len(phase2) == ts.PHASE_2_COUNT
    ids = [q["id"] for q in phase1 + phase2]
    assert len(set(ids)) == len(ids), "a question was served twice in one sitting"

    for label, selected in (("phase1", phase1), ("phase2", phase2)):
        hydrated = ts._hydrate(selected)
        ts._shuffle_options(hydrated)
        for q in ts._to_client_shape(hydrated):
            missing = [k for k in REQUIRED_CLIENT_KEYS if q.get(k) is None]
            assert not missing, f"{label} {q['id']} missing {missing}"

            mode = (q.get("interaction") or {}).get("mode")
            assert mode in RENDERABLE_MODES, \
                f"{label} {q['id']} has unrenderable mode {mode!r}"

            leaked = FORBIDDEN_CLIENT_KEYS & set(q)
            assert not leaked, f"{label} {q['id']} leaked {leaked} to the client"


def test_served_payload_is_smaller_than_the_whole_bank(pool, live_db):
    """
    Guards the optimisation itself, not just its correctness: a regression to
    `select *` would still render fine and still hide the answer key, so nothing
    above would fail. This is the check that notices.
    """
    whole_bank = live_db.schema("app").table("test_questions").select("*").eq(
        "language", LANGUAGE).eq("active", True).execute().data
    bank_bytes = len(json.dumps(whole_bank).encode())
    pool_bytes = len(json.dumps(pool).encode())

    assert pool_bytes < bank_bytes * 0.15, (
        f"selection pool is {pool_bytes / bank_bytes:.1%} of the full bank "
        f"({pool_bytes:,} of {bank_bytes:,} bytes) — has select(*) returned?"
    )
