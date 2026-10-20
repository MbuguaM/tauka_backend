import logging

from app.services.enrolment_service import sweep_waitlist

logger = logging.getLogger(__name__)


async def run_waitlist_retention() -> None:
    """
    Daily cron: delete waitlist entries that never became an account and are
    past their 12-month `delete_after` (enrolment flow, Retention).

    A failure here is silent data *retention*, never data loss — so it logs
    loudly and never raises past the daily runner.
    """
    try:
        deleted = sweep_waitlist()
        logger.info("Waitlist retention: deleted %d expired entries", deleted)
    except Exception as exc:
        logger.error("run_waitlist_retention failed: %s", exc)
