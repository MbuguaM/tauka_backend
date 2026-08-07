import logging
from app.services.testimonial_service import (
    check_testimonial_eligibility,
    send_pending_requests,
    auto_expire_unanswered,
)

logger = logging.getLogger(__name__)


async def run_testimonial_outreach() -> None:
    """Daily cron: create, send, and expire testimonial requests."""
    logger.info("Starting testimonial outreach")

    try:
        created = await check_testimonial_eligibility()
        logger.info("Testimonial eligibility: %d new requests created", len(created))
    except Exception as exc:
        logger.error("check_testimonial_eligibility failed: %s", exc)

    try:
        sent = await send_pending_requests()
        logger.info("Testimonials sent: %s", sent)
    except Exception as exc:
        logger.error("send_pending_requests failed: %s", exc)

    try:
        expired = await auto_expire_unanswered()
        logger.info("Testimonials auto-expired: %d", expired)
    except Exception as exc:
        logger.error("auto_expire_unanswered failed: %s", exc)
