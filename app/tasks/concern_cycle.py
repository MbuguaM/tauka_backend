"""Daily concern-report alerts — enrolment plan 4.6, migration §117.

Resends any new-report alert that failed, and sends the "not acknowledged after
24 hours" alert once. The cron is daily, so that alert lands between 24 and 48
hours after the report; the first alert goes at once from the request.
"""

import logging

from app.services import concern_service

logger = logging.getLogger(__name__)


async def run_concern_cycle() -> None:
    logger.info("Concern cycle: %s", await concern_service.daily())
