"""Daily pod schedule changes — enrolment plan 4.5, migration §113.

Expires proposals nobody finished agreeing to by their start, and applies agreed
ones whose start has come.
"""

import logging

from app.services import schedule_service

logger = logging.getLogger(__name__)


async def run_schedule_cycle() -> None:
    logger.info("Schedule cycle: %s", await schedule_service.daily())
