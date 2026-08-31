"""Plan Phase 6 — the warm handoff (§1.7 / §2.5) and tutor-facing progress (§1.8).

The handoff has no migration behind it: §73.1 already shipped the state machine's
columns. So what is worth testing is not the schema but the ordering and the
exceptions, because the failure modes here are all "the student ends up with no
tutor" or "the student is moved silently", and both look like success.

What is worth asserting:

1. **The new link is created BEFORE the old one closes.** An interruption must
   leave the student *with* a tutor. The partial unique index permits the
   overlap because `handoff_pending` is not `active` — that is the whole reason
   the middle state exists.
2. **Only the commitment block is bypassed.** `evaluate_assignment` refuses to
   move a commitment-locked student and names the handoff as the exception. Any
   other refusal — the concentration cap above all — must still refuse.
3. **The commitment is repointed.** Left naming the departed tutor,
   `locked_by_commitment` would block the tutor the platform just assigned, and
   the student could never be reassigned again that term.
4. **A promoted guest is not both home and guest.** Every roster query would
   return them twice.
5. **`handoff_pending` does not vanish from the roster.** This is the bug Phase 6
   found in Phase 2's code: `get_roster` filtered `state = 'active'`, so
   beginning a handoff erased the student's home tutor from their own screen —
   the silent swap, produced by the screen rather than the state machine.
6. **Progress is gated on the course join, not the link.** The endpoint runs as
   the service role, so RLS protects nothing and the gate must match what the
   policies would have enforced.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services import handoff_service, matching_service, progress_service


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_begin_refuses_when_there_is_no_home_tutor():
    with patch.object(handoff_service, "_links", return_value=[]):
        with pytest.raises(handoff_service.HandoffRefused):
            await handoff_service.begin("s1")


@pytest.mark.asyncio
async def test_begin_is_idempotent():
    """An admin clicking twice, or a tutor-level sweep touching a student it
    already touched, must not be an error."""
    pending = [{"id": "l1", "tutor_id": "t1", "role": "home",
                "state": "handoff_pending", "started_at": None}]
    with (
        patch.object(handoff_service, "_links", return_value=pending),
        patch.object(handoff_service, "status", return_value={"in_progress": True}),
    ):
        out = await handoff_service.begin("s1")
    assert out["began"] is False and out["already_pending"] is True


@pytest.mark.asyncio
async def test_begin_rejects_an_unknown_reason():
    with pytest.raises(handoff_service.HandoffRefused):
        await handoff_service.begin("s1", reason="because")


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------

def _complete_env(blocking=(), guest_row=None, commitment_rows=()):
    """Wire `complete`'s four writes to a recorder so order can be asserted."""
    calls: list[tuple] = []

    def fake_table(name):
        t = MagicMock()
        t.insert.side_effect = lambda payload: (
            calls.append(("insert", name, payload)) or MagicMock(data=[{"id": "new"}]))

        def upd(payload):
            calls.append(("update", name, payload))
            chain = MagicMock()
            chain.eq.return_value.execute.return_value = MagicMock(data=[payload])
            return chain
        t.update.side_effect = upd
        sel = MagicMock()
        sel.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=list(commitment_rows))
        t.select.return_value = sel
        return t

    rows = [{"id": "old", "tutor_id": "t_out", "role": "home",
             "state": "handoff_pending", "started_at": "2025-01-01T00:00:00+00:00"}]
    if guest_row:
        rows.append(guest_row)

    return calls, (
        patch.object(handoff_service, "_links", return_value=rows),
        patch.object(handoff_service.matching_service, "evaluate_assignment",
                     return_value={"allowed": not blocking, "blocking": list(blocking),
                                   "advisories": []}),
        patch.object(handoff_service.db, "schema",
                     return_value=MagicMock(table=fake_table)),
        patch.object(handoff_service, "status", return_value={"in_progress": False}),
        patch.object(handoff_service.progress_service, "teaches_student",
                     return_value={"allowed": True, "shared_courses": ["c1"]}),
        patch.object(handoff_service.progress_service, "_student_courses",
                     return_value=["c1"]),
    )


@pytest.mark.asyncio
async def test_the_new_link_is_created_before_the_old_one_closes():
    """An interruption must leave the student with a tutor, not without one."""
    calls, patches = _complete_env()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await handoff_service.complete("s1", "t_in")

    kinds = [(c[0], c[1]) for c in calls]
    insert_at = kinds.index(("insert", "student_tutor_link"))
    close_at = next(i for i, c in enumerate(calls)
                    if c[0] == "update" and c[1] == "student_tutor_link"
                    and c[2].get("state") == "reassigned")
    assert insert_at < close_at


@pytest.mark.asyncio
async def test_the_new_link_chains_to_the_one_it_replaces():
    """§1.8's "not a fresh start" needs a chain to walk."""
    calls, patches = _complete_env()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await handoff_service.complete("s1", "t_in")
    payload = next(c[2] for c in calls if c[0] == "insert")
    assert payload["replaced_link_id"] == "old"
    assert payload["source"] == "handoff"
    assert payload["role"] == "home"


@pytest.mark.asyncio
async def test_only_the_commitment_block_is_bypassed():
    """The concentration cap must still refuse. If every block were bypassed,
    the handoff would be a hole straight through §1.4."""
    calls, patches = _complete_env(blocking=[
        "this student has an active termly commitment locking their home tutor",
        "tutor would hold 100.0% of linked students, over the 60% concentration cap",
    ])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        with pytest.raises(handoff_service.HandoffRefused) as exc:
            await handoff_service.complete("s1", "t_in")
    assert "concentration cap" in str(exc.value)
    assert "termly commitment" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_commitment_only_block_lets_the_handoff_through_and_says_so():
    calls, patches = _complete_env(blocking=[
        "this student has an active termly commitment locking their home tutor",
    ])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        out = await handoff_service.complete("s1", "t_in")
    assert out["completed"] is True
    assert out["commitment_lock_bypassed"] is True


@pytest.mark.asyncio
async def test_an_active_commitment_is_repointed_at_the_new_tutor():
    """Left naming the departed tutor, `locked_by_commitment` would block the
    replacement the platform just assigned — for the rest of the term."""
    calls, patches = _complete_env(commitment_rows=[
        {"id": "com1", "locked_home_tutor_id": "t_out", "status": "active"},
    ])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        out = await handoff_service.complete("s1", "t_in")
    assert out["commitment_repointed"] is True
    assert any(c[1] == "enrollment_commitment"
               and c[2].get("locked_home_tutor_id") == "t_in" for c in calls)


@pytest.mark.asyncio
async def test_a_promoted_guest_stops_being_a_guest():
    """Otherwise every roster query returns them twice — once as home, once as
    guest."""
    guest = {"id": "g1", "tutor_id": "t_in", "role": "guest",
             "state": "active", "started_at": None}
    calls, patches = _complete_env(guest_row=guest)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await handoff_service.complete("s1", "t_in")
    assert any(c[0] == "update" and c[2].get("state") == "ended" for c in calls)


@pytest.mark.asyncio
async def test_a_tutor_cannot_replace_themselves():
    calls, patches = _complete_env()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        with pytest.raises(handoff_service.HandoffRefused):
            await handoff_service.complete("s1", "t_out")


@pytest.mark.asyncio
async def test_a_handoff_that_grants_no_history_access_says_so():
    """The gap §1.8 would otherwise hit silently: a handoff writes
    `student_tutor_link`, but reading history is gated on the course join."""
    calls, patches = _complete_env()
    with (
        patches[0], patches[1], patches[2], patches[3],
        patch.object(handoff_service.progress_service, "teaches_student",
                     return_value={"allowed": False, "shared_courses": []}),
        patch.object(handoff_service.progress_service, "_student_courses",
                     return_value=["c1", "c2"]),
    ):
        out = await handoff_service.complete("s1", "t_in")
    assert out["progress_access"]["granted"] is False
    assert out["progress_access"]["missing_course_assignments"] == ["c1", "c2"]


# ---------------------------------------------------------------------------
# The roster bug this phase found in Phase 2's code
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_handing_off_home_tutor_stays_on_the_roster():
    """Before Phase 6, `get_roster` filtered `state = 'active'`, so beginning a
    handoff made the student's home tutor vanish from their own tutor screen and
    the roster read as "no home tutor". That is the silent swap §1.7 exists to
    prevent, produced by the screen instead of the state machine."""
    rows = [{"id": "l1", "tutor_id": "t1", "role": "home",
             "source": "matched", "state": "handoff_pending",
             "started_at": "2025-01-01T00:00:00+00:00"}]
    chain = MagicMock()
    chain.select.return_value.eq.return_value.in_.return_value.order.return_value \
        .execute.return_value = MagicMock(data=rows)

    with (
        patch.object(matching_service.db, "schema",
                     return_value=MagicMock(table=lambda _n: chain)),
        patch.object(matching_service, "_tutor_display", return_value={}),
        patch.object(matching_service, "active_tutor_count", return_value=1),
        patch.object(matching_service, "_config",
                     return_value=dict(matching_service._DEFAULTS)),
    ):
        roster = await matching_service.get_roster("s1")

    assert roster["home"] is not None, "the outgoing tutor must not vanish"
    assert roster["home"]["handing_off"] is True
    assert roster["handoff_in_progress"] is True


def test_mid_handoff_is_reported_as_a_transition_not_as_unassigned():
    """An admin told "no active home tutor" would try to assign one, which is
    the single thing that must not happen while a handoff is pending."""
    rows = [{"tutor_id": "t1", "role": "home", "state": "handoff_pending"}]
    chain = MagicMock()
    chain.select.return_value.eq.return_value.in_.return_value.execute.return_value = \
        MagicMock(data=rows)

    with (
        patch.object(matching_service.db, "schema",
                     return_value=MagicMock(table=lambda _n: chain)),
        patch.object(matching_service, "active_tutor_count", return_value=1),
        patch.object(matching_service, "_config",
                     return_value=dict(matching_service._DEFAULTS)),
        patch.object(matching_service, "guest_rotation", return_value={}),
    ):
        out = matching_service.validate_link_set("s1")

    assert out["handoff_pending_count"] == 1
    assert any("mid-handoff" in p for p in out["problems"])
    assert not any(p == "no active home tutor" for p in out["problems"])


# ---------------------------------------------------------------------------
# Progress (§1.8)
# ---------------------------------------------------------------------------

def test_progress_is_gated_on_the_course_join_not_the_link():
    """This runs as the service role, so RLS protects nothing. Gating on
    `student_tutor_link` would let a guest tutor read history that
    `er_select_own_or_teacher_admin` denies them, with the service role as the
    laundering step."""
    with (
        patch.object(progress_service, "teaches_student",
                     return_value={"allowed": False, "shared_courses": []}),
        patch.object(progress_service, "_has_link", return_value=None),
    ):
        with pytest.raises(progress_service.ProgressDenied):
            progress_service.student_progress("t1", "s1")


def test_a_linked_tutor_without_the_course_gets_an_actionable_refusal():
    """"Not yours" would send an admin looking for a missing link that is
    already there. The message names the real fix."""
    with (
        patch.object(progress_service, "teaches_student",
                     return_value={"allowed": False, "shared_courses": []}),
        patch.object(progress_service, "_has_link",
                     return_value={"id": "l1", "role": "home", "state": "active"}),
    ):
        with pytest.raises(progress_service.ProgressDenied) as exc:
            progress_service.student_progress("t1", "s1")
    assert "course" in str(exc.value)


def test_progress_never_returns_voice_or_speech_while_l002_is_open():
    """The developer's Phase 6 scope decision, asserted rather than trusted to a
    comment: no data category a tutor could not already read."""
    with (
        patch.object(progress_service, "teaches_student",
                     return_value={"allowed": True, "shared_courses": ["c1"]}),
        patch.object(progress_service, "_unit_history", return_value=[]),
        patch.object(progress_service, "_exercise_history",
                     return_value={"results": [], "count": 0, "trend": None}),
        patch.object(progress_service, "_attendance", return_value={}),
        patch.object(progress_service, "_pace", return_value=None),
        patch.object(progress_service, "_activity",
                     return_value={"visible": False, "summary": None}),
        patch.object(progress_service, "_relationship", return_value={}),
    ):
        out = progress_service.student_progress("t1", "s1")

    assert out["speech"]["available"] is False
    assert "L-002" in out["speech"]["reason"]
    assert "submissions" not in out.get("speech", {})


def test_an_empty_exercise_history_is_a_count_not_a_zero_percent():
    """`exercise_result` held 0 rows when this shipped. "No exercises yet" and
    "0% average" are different statements and only one of them is true."""
    failing = MagicMock()
    failing.table.side_effect = RuntimeError("boom")
    with patch.object(progress_service.db, "schema", return_value=failing):
        out = progress_service._exercise_history("s1")
    assert out == {"results": [], "count": 0, "trend": None}


def test_an_unmarked_cohort_session_is_never_counted_as_missed():
    """The cohort domain's rule, applied to a tutor-facing read. Folding
    unmarked sessions into the rate converts missing data into an accusation."""
    rows = [{"status": "present"}, {"status": "absent"}, {"status": "pending"}]

    def fake_table(name):
        t = MagicMock()
        if name == "cohort_session_attendance":
            t.select.return_value.eq.return_value.limit.return_value.execute.return_value = \
                MagicMock(data=rows)
        else:
            t.select.return_value.eq.return_value.order.return_value.limit.return_value \
                .execute.return_value = MagicMock(data=[])
        return t

    with patch.object(progress_service.db, "schema",
                      return_value=MagicMock(table=fake_table)):
        out = progress_service._attendance("s1", "t1")

    assert out["cohort"]["present"] == 1
    assert out["cohort"]["absent"] == 1
    assert out["cohort"]["unmarked"] == 1
    assert out["cohort"]["rate_pct"] == 50.0, "the unmarked row must not dilute it"


def test_activity_is_withheld_on_a_free_tier_and_says_which_it_is():
    """A missing row means "not shared on this plan", never "no activity". A
    tutor reading 0 minutes about a free-tier student would be reading a
    permission as a behaviour."""
    t = MagicMock()
    t.select.return_value.eq.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=[{"tier": "free_tier"}])
    with patch.object(progress_service.db, "schema",
                      return_value=MagicMock(table=lambda _n: t)):
        out = progress_service._activity("s1")
    assert out["visible"] is False
    assert out["summary"] is None
    assert "plan" in out["reason"]
