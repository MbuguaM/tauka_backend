"""Daily seat refill — enrolment plan 3.6, migration §112.

Closes refill cycles whose offers have resolved, and, in the first 14 days
after a formation day, offers live pods' empty seats to waiting learners who
fit them.
"""

import logging

from app.services import refill_service

logger = logging.getLogger(__name__)


async def run_refill_cycle() -> None:
    logger.info("Refill cycle: %s", refill_service.run())
