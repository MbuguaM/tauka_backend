import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    """Register all cron jobs and start the scheduler."""
    scheduler.add_job(
        "app.tasks.milestone_check:run_milestone_check",
        "cron",
        hour=6,
        minute=0,
        id="milestone_check",
        replace_existing=True,
    )
    scheduler.add_job(
        "app.tasks.gift_lifecycle:run_gift_lifecycle",
        "cron",
        hour=7,
        minute=0,
        id="gift_lifecycle",
        replace_existing=True,
    )
    scheduler.add_job(
        "app.tasks.testimonial_outreach:run_testimonial_outreach",
        "cron",
        hour=8,
        minute=0,
        id="testimonial_outreach",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))
