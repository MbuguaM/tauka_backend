"""
Work that follows a request but must not fail it.

WHY THIS EXISTS INSTEAD OF `BackgroundTasks`
--------------------------------------------
FastAPI's `BackgroundTasks` run *after* the response is sent. On a persistent
server that is free work. On Vercel — and any serverless host — the execution
context is frozen the instant the response is returned, so the task may simply
never run. Nothing raises, the endpoint returns 200, and the effect silently
does not happen: the placement-test result email is never sent, the referral is
never linked to the session, the token usage is never logged.

That failure is invisible in exactly the way the bugs in this codebase have
historically been invisible, so the fix is to stop deferring past the response
boundary at all. `run_followup` awaits the work inline, which means it really
happens, while keeping the two properties `BackgroundTasks` was chosen for:

  * **It cannot fail the request.** Every exception is logged and swallowed. A
    mail outage must never turn a completed assessment into a 500.
  * **It cannot hang the request.** Serverless platforms kill a function at a
    hard wall-clock limit — 10s on Vercel Hobby — and an unbounded await inside
    that budget converts a slow dependency into a dead endpoint with no
    response at all. The timeout abandons the work instead, loudly.

The cost is latency: this work is now on the critical path. That is the trade
being made deliberately — a result email that arrives at the cost of 400ms is
worth more than one that is instant and hypothetical.
"""
import asyncio
import logging
from typing import Awaitable

from app.config import settings

logger = logging.getLogger(__name__)


async def run_followup(awaitable: Awaitable, *, label: str,
                       timeout: float | None = None) -> bool:
    """
    Await `awaitable`, bounded and non-fatal. Returns whether it completed.

    `label` is what appears in the log when it does not, so make it the thing
    you would search for at 2am — the caller and the effect, not the function
    name.
    """
    limit = settings.followup_timeout_seconds if timeout is None else timeout
    try:
        await asyncio.wait_for(awaitable, timeout=limit)
        return True
    except asyncio.TimeoutError:
        # Distinct from a failure: the work may have partially applied, and the
        # cause is almost always a slow external dependency rather than a bug.
        logger.error(
            "FOLLOWUP TIMEOUT: %s exceeded %.1fs and was abandoned mid-flight",
            label, limit,
        )
    except Exception:
        logger.exception("FOLLOWUP FAILED: %s", label)
    return False


async def run_followups(*jobs: tuple[Awaitable, str]) -> dict[str, bool]:
    """
    Run several follow-ups concurrently, each isolated from the others.

    Sequential awaits would sum their latencies into the request, and on a
    10s budget three 3s sends is the difference between working and timing out.
    One failing must not cancel its siblings, hence `gather` over the already
    exception-swallowing `run_followup` rather than `return_exceptions`.
    """
    if not jobs:
        return {}
    results = await asyncio.gather(
        *(run_followup(aw, label=label) for aw, label in jobs)
    )
    return {label: ok for (_, label), ok in zip(jobs, results)}
