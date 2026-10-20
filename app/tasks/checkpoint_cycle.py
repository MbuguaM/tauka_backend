"""Daily pod checkpoint run — enrolment plan 4.2, migration §109.

Opens this Ethiopic month's checkpoint for every live pod that doesn't have
one yet. Runs daily so a missed formation day is caught up the next morning.
"""

import logging

from app.services import checkpoint_service

logger = logging.getLogger(__name__)


async def run_checkpoint_cycle() -> None:
    logger.info("Checkpoint cycle: opened %s", checkpoint_service.open_due())
