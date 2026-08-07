import logging
from app.services.milestone_service import check_all_milestones, process_pending_notifications

logger = logging.getLogger(__name__)


async def run_milestone_check() -> None:
    """Daily cron: detect milestones and send notifications."""
    logger.info("Starting milestone check")
    try:
        found = await check_all_milestones()
        logger.info("Milestones: %s", found)
    except Exception as exc:
        logger.error("check_all_milestones failed: %s", exc)

    try:
        sent = await process_pending_notifications()
        logger.info("Notifications: %s", sent)
    except Exception as exc:
        logger.error("process_pending_notifications failed: %s", exc)
