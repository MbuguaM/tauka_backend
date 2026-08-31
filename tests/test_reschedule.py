"""Plan Phase 7 — moving a booking (spec §2.6).

The SQL half of this was exercised against the live database when §80 was
applied, in a rolled-back transaction: a late move followed by an "early"
cancel was refused a refund, an honest early move followed by an early cancel
was refunded, and the entitlement balance came out at 4 from 5 granted minus 2
consumed plus 1 refunded. Mocking `app.reschedule_one_to_one` to assert what it
returns would test the mock.

What is worth asserting **here**, in the service layer:

1. **The optional arguments are omitted, not sent as null.** `p_tutor` absent
   and `p_tutor: None` behave identically in the function, but the RPC log is
   the only record of what was actually asked for, and "moved to tutor null"
   is a worse thing to read at 3am than "moved".
2. **Every refusal word the RPC can raise has a sentence.** A word missing from
   `REFUSALS` surfaces to the student as a generic failure, which is how
   `reschedule_limit` would have read as "something went wrong" forever.
3. **The message states both facts.** Whether the refund was forfeited and how
   many moves are left. A student not told about the forfeit at this moment
   finds out at cancellation — the punitive surprise §2.6 exists to avoid.
4. **A genuine fault is not swallowed as a refusal.** `_refusal_from` must not
   turn a dropped connection into "that booking no longer exists".
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services import booking_service
from app.services.booking_service import BookingRefused


def _rpc(returning: dict | Exception):
    """Patch the RPC and hand back the recorder so calls can be inspected."""
    calls: list[tuple] = []

    def fake_rpc(name, payload):
        calls.append((name, payload))
        if isinstance(returning, Exception):
            raise returning
        return MagicMock(execute=lambda: MagicMock(data=returning))

    schema = MagicMock()
    schema.rpc.side_effect = fake_rpc
    return calls, patch.object(booking_service.db, "schema", return_value=schema)


# ---------------------------------------------------------------------------
# What gets sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_optional_arguments_are_omitted_rather_than_sent_as_null():
    calls, patched = _rpc({"refund_forfeited": False, "reschedules_left": 2})
    with patched:
        await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")
    _, payload = calls[0]
    assert set(payload) == {"p_session", "p_at", "p_by"}
    assert "p_tutor" not in payload
    assert "p_availability" not in payload


@pytest.mark.asyncio
async def test_a_tutor_change_is_passed_through():
    calls, patched = _rpc({"refund_forfeited": False, "reschedules_left": 1})
    with patched:
        await booking_service.reschedule(
            "s1", "2026-09-01T10:00:00Z", "u1",
            tutor_id="t2", availability_id="a9")
    _, payload = calls[0]
    assert payload["p_tutor"] == "t2"
    assert payload["p_availability"] == "a9"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", [
    "no_such_session", "not_reschedulable", "not_yours", "past_slot",
    "not_your_tutor", "slot_taken", "reschedule_limit",
])
def test_every_reschedule_refusal_word_has_a_sentence(word):
    """A word the RPC can raise but `REFUSALS` does not know renders as a
    generic failure. `not_reschedulable` and `reschedule_limit` are new in §80
    and are the two that would have been missed."""
    assert word in booking_service.REFUSALS
    assert BookingRefused(word).message != "That booking could not be completed."


@pytest.mark.asyncio
async def test_a_refusal_becomes_bookingrefused_not_a_500():
    calls, patched = _rpc(RuntimeError('... raise exception "reschedule_limit" ...'))
    with patched:
        with pytest.raises(BookingRefused) as exc:
            await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")
    assert exc.value.reason == "reschedule_limit"
    assert "moved" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_a_genuine_fault_is_not_dressed_up_as_a_refusal():
    """A dropped connection is not "that booking no longer exists"."""
    calls, patched = _rpc(RuntimeError("connection reset by peer"))
    with patched:
        with pytest.raises(RuntimeError):
            await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")


# ---------------------------------------------------------------------------
# What the student is told
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_forfeited_refund_is_stated_at_the_moment_it_happens():
    """Not left to be discovered at cancellation from a balance that did not
    move. That discovery IS the punitive copy §2.6 forbids."""
    calls, patched = _rpc({
        "refund_forfeited": True, "reschedules_left": 1,
        "scheduled_at": "2026-09-01T10:00:00Z",
    })
    with patched:
        out = await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")
    assert out["refund_forfeited"] is True
    assert "will not return it to your balance" in out["message"]
    assert "once more" in out["message"]


@pytest.mark.asyncio
async def test_an_ordinary_move_says_nothing_about_forfeits():
    """No warning where there is nothing to warn about. A reschedule that cost
    nothing must not read like one that did."""
    calls, patched = _rpc({"refund_forfeited": False, "reschedules_left": 2})
    with patched:
        out = await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")
    assert out["refund_forfeited"] is False
    assert "balance" not in out["message"]
    assert out["message"].startswith("Moved.")


@pytest.mark.asyncio
async def test_the_last_permitted_move_says_so():
    calls, patched = _rpc({"refund_forfeited": False, "reschedules_left": 0})
    with patched:
        out = await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")
    assert "cannot be moved again" in out["message"]


@pytest.mark.asyncio
async def test_a_missing_reschedules_left_is_simply_not_mentioned():
    """A degraded response must not produce "You can move it None more times"."""
    calls, patched = _rpc({"refund_forfeited": False})
    with patched:
        out = await booking_service.reschedule("s1", "2026-09-01T10:00:00Z", "u1")
    assert "None" not in out["message"]
    assert out["reschedules_left"] is None
