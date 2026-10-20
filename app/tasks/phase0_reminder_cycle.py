"""Daily Phase 0 reminders — enrolment plan 1.3, migration §127."""

import logging

from app.services import phase0_reminder_service

logger = logging.getLogger(__name__)


async def run_phase0_reminder_cycle() -> None:
    logger.info("Phase 0 reminders: %s", await phase0_reminder_service.daily())
