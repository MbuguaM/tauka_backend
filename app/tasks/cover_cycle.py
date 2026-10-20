"""Daily cover run — enrolment plan §2h, migration §123.

Alerts ops about uncovered sessions within 30 hours, and cancels sessions that
started without cover (our failure: credits).
"""

import logging

from app.services import cover_service

logger = logging.getLogger(__name__)


async def run_cover_cycle() -> None:
    logger.info("Cover cycle: %s", await cover_service.daily())
