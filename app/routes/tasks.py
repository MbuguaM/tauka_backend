"""
Scheduled jobs, triggered externally instead of by an in-process scheduler.

WHY THERE IS NO SCHEDULER ANY MORE
----------------------------------
This service used APScheduler started from the FastAPI lifespan. That cannot
work on a serverless host: the function is frozen the moment a response is
returned, so the event loop APScheduler needs never ticks and **not one job ever
fired**. Silently — the app boots, logs "Scheduler started with 3 jobs", and
then does nothing at 06:00. Gift expiry warnings would simply never send.

It was also wrong on a persistent host the moment there were two instances:
in-process APScheduler has no jobstore and no lock, so every instance runs every
job. Two instances means every supporter gets two milestone emails, and a
rolling deploy briefly gives you both.

An external trigger fixes both. It is externally observable (you can see the
invocation and its status), it cannot double-fire from scaling, and it is
testable with curl.

VERCEL CRON NOTES
-----------------
  * Vercel Cron issues **GET** only — hence GET, not POST, for what is really
    a mutating action.
  * It authenticates by sending `Authorization: Bearer $CRON_SECRET` when a
    `CRON_SECRET` env var exists on the project. Set it, or this refuses
    everything.
  * The Hobby plan has historically capped cron jobs per project and their
    frequency at roughly once daily. That is why all three jobs live behind ONE
    endpoint rather than three — it fits the tightest plan, and the jobs are
    daily anyway.
"""
import logging
import secrets

from fastapi import APIRouter, Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _authorise(authorization: str | None) -> None:
    """
    Fail CLOSED. An unset secret refuses every request rather than allowing
    them: this endpoint can mail every supporter in the database, so the
    consequence of a missing env var must be "nothing runs", never "anyone
    can run it".
    """
    if not settings.cron_secret:
        logger.error(
            "CRON_SECRET is not set — refusing to run scheduled jobs. Set it on "
            "the host; Vercel Cron sends it automatically once it exists."
        )
        raise HTTPException(status_code=503, detail="Scheduled jobs are not configured")

    expected = f"Bearer {settings.cron_secret}"
    # Constant-time: a plain == leaks the secret one byte at a time to anyone
    # who can measure the response, and this endpoint is public by necessity.
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/daily")
async def run_daily_tasks(authorization: str | None = Header(default=None)):
    """
    Run every daily job, in order. Triggered by Vercel Cron.

    Each job is isolated: one raising must not stop the rest, because a failure
    in testimonial outreach is no reason for gift expiries to go unprocessed.
    Returns per-job status so a failure is visible in the cron log rather than
    buried, and reports 200 even with failures inside — the trigger succeeded,
    and a non-200 would make Vercel retry the jobs that already worked.
    """
    _authorise(authorization)

    from app.tasks.gift_lifecycle import run_gift_lifecycle
    from app.tasks.milestone_check import run_milestone_check
    from app.tasks.testimonial_outreach import run_testimonial_outreach

    jobs = (
        ("gift_lifecycle", run_gift_lifecycle),
        ("milestone_check", run_milestone_check),
        ("testimonial_outreach", run_testimonial_outreach),
    )

    results: dict[str, str] = {}
    for name, job in jobs:
        try:
            await job()
            results[name] = "ok"
            logger.info("Scheduled job %s completed", name)
        except Exception as exc:
            results[name] = f"failed: {exc}"
            logger.exception("Scheduled job %s failed", name)

    return {"ran": results}
