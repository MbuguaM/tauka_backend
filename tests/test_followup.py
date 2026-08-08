"""
`run_followup` is what replaced BackgroundTasks, and every post-request effect
in the app now depends on its three guarantees. These test exactly those:

  1. the work actually runs (the whole reason for the change),
  2. a failure never reaches the caller (a mail outage must not 500 a completed
     assessment),
  3. a hang is abandoned at the timeout (a serverless function killed at its
     wall-clock limit returns NO response at all, which is worse than a slow one).
"""
import asyncio

import pytest

from app.core.followup import run_followup, run_followups


async def _ok(marker: list, value="done"):
    marker.append(value)
    return value


async def _boom():
    raise RuntimeError("provider exploded")


async def _hang():
    await asyncio.sleep(5)


# ── 1. It runs ───────────────────────────────────────────────────────────────

async def test_completes_and_reports_success():
    marker = []
    assert await run_followup(_ok(marker), label="test") is True
    assert marker == ["done"], "the awaitable did not actually execute"


# ── 2. It cannot fail the request ────────────────────────────────────────────

async def test_exception_is_swallowed_and_reported():
    assert await run_followup(_boom(), label="test") is False


async def test_exception_does_not_propagate_to_caller():
    """The property the endpoints rely on: this must not need a try/except."""
    await run_followup(_boom(), label="test")   # no raise == pass


# ── 3. It cannot hang the request ────────────────────────────────────────────

async def test_timeout_abandons_the_work():
    assert await run_followup(_hang(), label="test", timeout=0.05) is False


async def test_timeout_is_bounded_by_the_limit_not_the_work():
    started = asyncio.get_running_loop().time()
    await run_followup(_hang(), label="test", timeout=0.05)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0, f"waited {elapsed:.2f}s for a 0.05s timeout"


# ── Concurrency ──────────────────────────────────────────────────────────────

async def test_run_followups_isolates_failures():
    """One failing job must not cancel its siblings — a broken testimonial
    send is no reason for the result email to go unsent."""
    marker = []
    results = await run_followups(
        (_ok(marker, "a"), "job-a"),
        (_boom(), "job-b"),
        (_ok(marker, "c"), "job-c"),
    )
    assert results == {"job-a": True, "job-b": False, "job-c": True}
    assert sorted(marker) == ["a", "c"]


async def test_run_followups_is_concurrent_not_sequential():
    """Sequential awaits would sum into the request and blow the function
    budget; three 0.2s jobs must cost ~0.2s, not 0.6s."""
    async def slow():
        await asyncio.sleep(0.2)

    started = asyncio.get_running_loop().time()
    await run_followups((slow(), "a"), (slow(), "b"), (slow(), "c"))
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.5, f"ran sequentially: {elapsed:.2f}s for 3x0.2s"


async def test_no_jobs_is_a_noop():
    assert await run_followups() == {}


@pytest.mark.parametrize("label", ["", "a" * 200])
async def test_label_is_never_load_bearing(label):
    """Logging must not be able to break the effect it is describing."""
    marker = []
    assert await run_followup(_ok(marker), label=label) is True
