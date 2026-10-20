"""Telegram for tutor ops — enrolment plan 6.4.

One bot posting to one ops group (TELEGRAM_BOT_TOKEN, TELEGRAM_OPS_CHAT_ID):
cover requests, late drops and reported no-shows, so the people on standby and
ops see them where they already talk. Off when either is empty.

NEVER for anything about a learner's wellbeing or a concern report: those stay
with the safeguarding capability (L-015). Messages carry times and counts,
never a learner's name.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def post(text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_ops_chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                                  json={"chat_id": settings.telegram_ops_chat_id, "text": text[:3500],
                                        "disable_web_page_preview": True})
        return r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram post failed: %s", exc)
        return False
