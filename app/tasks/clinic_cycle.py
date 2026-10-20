"""Daily Phase 0 clinic run — enrolment plan 1.5, migration §105.

Creates a video room for any scheduled clinic missing one, and sends each
booked learner one reminder email within the day before their clinic.
"""

import logging

from app.services import clinic_service

logger = logging.getLogger(__name__)


async def run_clinic_cycle() -> None:
    logger.info("Clinic cycle: %s", await clinic_service.daily())
