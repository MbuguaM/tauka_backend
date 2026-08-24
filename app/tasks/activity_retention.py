import logging

from app.services.activity_retention_service import run_activity_retention

logger = logging.getLogger(__name__)


async def run_activity_retention_task() -> None:
    """
    Daily cron: prune expired activity files and sweep orphaned bucket objects.

    Phase 12 of `activity-telemetry-plan.md`. Deleting is the whole job, so a
    failure here is silent data *retention*, never data loss — which is why it
    logs loudly and never raises past the daily runner.
    """
    logger.info("Starting activity retention")
    try:
        result = await run_activity_retention()
        logger.info("Activity retention: %s", result)
    except Exception as exc:
        logger.error("run_activity_retention failed: %s", exc)
