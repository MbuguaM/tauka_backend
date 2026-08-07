import logging
from app.services.gift_service import process_gift_expiries

logger = logging.getLogger(__name__)


async def run_gift_lifecycle() -> None:
    """Daily cron: process gift warnings, expiries, and impact summaries."""
    logger.info("Starting gift lifecycle check")
    try:
        result = await process_gift_expiries()
        logger.info("Gift lifecycle: %s", result)
    except Exception as exc:
        logger.error("process_gift_expiries failed: %s", exc)
