"""Daily sweep of old tutor applications — enrolment plan §2j, §120, L-017.

Rejected and withdrawn applications are deleted 12 months after the decision.
"""

import logging

from app.services import tutor_application_service

logger = logging.getLogger(__name__)


async def run_tutor_application_retention() -> None:
    logger.info("Tutor application retention: deleted %s", tutor_application_service.sweep())
