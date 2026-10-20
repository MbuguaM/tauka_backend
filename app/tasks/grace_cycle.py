"""Daily grace run — enrolment plan 2.3a, migration §126.

Reminds on days 3 and 6 of a missed payment and lapses it on day 7.
"""

import logging

from app.services import grace_service

logger = logging.getLogger(__name__)


async def run_grace_cycle() -> None:
    logger.info("Grace cycle: %s", await grace_service.daily())
