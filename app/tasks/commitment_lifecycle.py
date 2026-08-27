import logging

from app.services import booking_service, commitment_service

logger = logging.getLogger(__name__)


async def run_commitment_lifecycle() -> None:
    """Daily cron: milestone checks, then term expiry.

    Both run every day rather than behind the payout cadence guard: a milestone
    reached on the 3rd should be visible on the 3rd, and a term that ended
    yesterday should not still read as active for a month.

    **Neither touches a tier.** Whether a student drops back after a term is a
    billing question — their monthly subscription may well continue — and a job
    silently downgrading paying customers would be the worst possible way to
    find that out.
    """
    try:
        milestones = await commitment_service.sweep_milestones()
        if milestones.get("reached"):
            logger.info("Commitment milestones reached: %s", milestones)
    except Exception as exc:  # noqa: BLE001
        # Isolated: a milestone check that fails must not stop terms expiring.
        logger.error("milestone sweep failed: %s", exc)

    try:
        expired = await commitment_service.expire_finished_terms()
        if expired.get("completed"):
            logger.info("Commitment terms completed: %s", expired)
    except Exception as exc:  # noqa: BLE001
        logger.error("term expiry sweep failed: %s", exc)


async def report_unmarked_sessions() -> None:
    """Daily: log past 1-on-1s nobody has marked.

    **Reports, never decides.** A session becomes `completed` only when its tutor
    says so (`app.mark_one_to_one_outcome`), because `payout_service` pays for
    `completed` rows and a sweep would pay for lessons nobody attended.

    Unmarked sessions are unpaid sessions, so this list is the thing standing
    between a tutor and their money — which is why it is logged at WARNING rather
    than INFO once anything is on it.
    """
    try:
        stale = await booking_service.unmarked_sessions(hours=24)
    except Exception as exc:  # noqa: BLE001
        logger.error("unmarked session report failed: %s", exc)
        return

    if not stale:
        return

    logger.warning(
        "%d 1-on-1 session(s) are past and unmarked — they cannot be paid until "
        "the tutor records an outcome. Oldest: %s",
        len(stale), stale[0].get("scheduled_at"))
