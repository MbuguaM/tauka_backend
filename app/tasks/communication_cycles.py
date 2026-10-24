"""Communication jobs — enrolment plan Track 6.

Daily: clock-change warnings (6.5), pod welcomes and season posts (6.3).
Hourly (`GET /tasks/hourly`): session reminders 24 h and 1 h before (6.1/6.2).
"""

import logging

logger = logging.getLogger(__name__)


async def run_dst_cycle() -> None:
    from app.services import dst_service
    logger.info("DST warnings: %s", await dst_service.daily())


async def run_season_cycle() -> None:
    from app.services import season_service
    logger.info("Season channel: %s", season_service.daily())


async def run_qa_sample_cycle() -> None:
    """Plan 8.7: this month's QA observations (idempotent per tutor per month)."""
    from app.services import tutor_ops_service
    logger.info("QA sampling: %s", tutor_ops_service.sample_month())


async def run_peer_cycle() -> None:
    """Track 5: pair on Mondays (when the peer_sessions flag is on); tidy the queue."""
    from app.services import peer_service
    logger.info("Peer sessions: %s", peer_service.weekly())


async def run_benchmark_snapshot() -> None:
    """Track 9: keep the benchmarks after each formation (once per formation)."""
    from app.services import dashboard_service
    logger.info("Benchmark snapshot taken: %s", dashboard_service.snapshot())


async def run_gift_card_cycle() -> None:
    """Track 10: end redeemed gift cards' access; lapse old unredeemed ones."""
    from app.services import commerce_service
    logger.info("Gift cards: %s", await commerce_service.daily())


async def run_sponsorship_cycle() -> None:
    """§144: release lapsed holds, end sponsored seats after their months, lapse unclaimed named seats."""
    from app.services import sponsorship_service
    logger.info("Sponsorship: %s", await sponsorship_service.daily())


async def run_unit_assessment_cycle() -> None:
    """§141: open the speaking checks of learners who reached a block without opening the app."""
    from app.services import unit_assessment_service
    logger.info("Unit assessments opened: %s", unit_assessment_service.daily())


async def run_mentoring_cycle() -> None:
    """Track 10 pilot: heritage-beginner mentoring, the first Monday of the month."""
    from app.services import pilots_service
    logger.info("Mentoring: %s", pilots_service.pair_mentoring())


async def run_reminder_cycle() -> None:
    from app.services import reminder_service
    logger.info("Session reminders: %s", await reminder_service.hourly())


async def run_cohort_room_cycle() -> None:
    """Hourly: Daily rooms for pod sessions in the next 24 hours (plan §3 item 3)."""
    from app.services import cohort_room_service
    logger.info("Pod rooms: %s", await cohort_room_service.hourly())


async def run_exchange_hourly() -> None:
    """Hourly (features addendum A): lapsed requests, rooms, unrated sessions."""
    from app.services import exchange_service
    logger.info("Exchange hourly: %s", await exchange_service.hourly())


async def run_exchange_daily() -> None:
    """Daily (A8, A9): inactive partners, the waitlist, held rewards."""
    from app.services import exchange_service
    logger.info("Exchange daily: %s", exchange_service.daily())


async def run_summary_refresh() -> None:
    """Daily (B4): regenerate flagged and out-of-date lesson summaries."""
    from app.services import summary_service
    logger.info("Summary refresh: %s", await summary_service.refresh())
