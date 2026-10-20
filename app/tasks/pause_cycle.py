"""Daily pause run — enrolment plan 4.4, migration §111.

Starts pauses whose paid month has ended, reminds a week before a pause runs
out, and lapses pauses past their 3 months into a cancellation.
"""

import logging

from app.services import pause_service

logger = logging.getLogger(__name__)


async def run_pause_cycle() -> None:
    logger.info("Pause cycle: %s", await pause_service.daily())
