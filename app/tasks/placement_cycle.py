"""Daily placement-notice catch-up — enrolment plan 3.5, migration §110.

The accept request sends placement emails inline; this sends any it missed
(a failed or abandoned send) for pods with a membership from the last 14 days.
"""

import logging

from app.services import placement_service

logger = logging.getLogger(__name__)


async def run_placement_cycle() -> None:
    logger.info("Placement cycle: sent %s", await placement_service.daily())
