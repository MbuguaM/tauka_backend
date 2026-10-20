"""Daily catch-up emails — enrolment plan 4.8, migration §114.

Sends each learner marked absent or excused the tutor's notes and the lesson's
practice, once the notes are written or 48 hours have passed.
"""

import logging

from app.services import catchup_service

logger = logging.getLogger(__name__)


async def run_catchup_cycle() -> None:
    logger.info("Catch-up cycle: sent %s", await catchup_service.daily())
