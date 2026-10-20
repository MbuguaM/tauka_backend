"""Daily make-up run — enrolment plan §2e, migration §92.

Sweeps first (withdraw learners whose pod formed, cancel empty groups, cancel
groups nobody scheduled before their deadline — which grants credits), then
groups whoever is waiting. Daily, because a learner's paid month starts the day
they pay, not on a shared calendar date.
"""

import logging

from app.services import makeup_delivery, makeup_service

logger = logging.getLogger(__name__)


async def run_makeup_cycle() -> None:
    result = makeup_service.run()
    logger.info("Make-up cycle: %s", result)
    # Booked make-ups: a room for any that lack one, and day-before reminders
    # (§2f items 3, 5; §103). Separate so a mail failure can't stop the planner.
    try:
        logger.info("Make-up delivery: %s", await makeup_delivery.daily())
    except Exception as exc:  # noqa: BLE001
        logger.error("make-up delivery failed: %s", exc)
