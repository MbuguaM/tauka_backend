"""Monthly tutor standing recomputation — plan Phase 6, spec §1.6.

`/tasks/daily` fires every day; standing moves once a month. The guard is the
first thing that runs and reads `standing.cadence_day` from `app.config` rather
than hardcoding the 1st — the same shape as `payout_cycle`, with its own key so
moving payday does not silently move promotion day.

WHY THIS IS A SEPARATE JOB FROM `payout_cycle` DESPITE SHARING ITS INPUTS.

Both read `payout_service.delivered_sessions` and `quality_inputs`, and it would
be tempting to fold standing into the payout run since the numbers are already
in hand there. They are kept apart because a failure in one must not stop the
other, and because they answer to different clocks: a payout run that slips a
day is a late payment, while a standing run that slips a day is nothing at all.
Folding them would couple the second to the first's failure modes for a saving
of two queries a month.
"""

import logging

from app.services import standing_service

logger = logging.getLogger(__name__)


async def run_standing_cycle() -> None:
    """Recompute every active tutor's standing, once a month.

    Writes nothing when the verdict is unchanged — `tutor_standing` is a history
    of transitions, and a row per tutor per month would bury the two that matter
    under a hundred that say the same thing.
    """
    if not standing_service.is_cadence_day():
        return

    try:
        result = await standing_service.recompute_standings()
    except Exception as exc:  # noqa: BLE001
        logger.error("standing recompute failed: %s", exc)
        return

    logger.info("Standing cycle: %s", result)

    # Worth a line of its own in the log. On a platform where every tutor is
    # `provisional` and stays there, a run that changed nothing is the expected
    # outcome — and a run that changed nothing because it read zero tutors is
    # not. The two are indistinguishable without this.
    if result.get("computed") == 0:
        logger.warning("Standing cycle computed 0 tutors — is anyone active?")
