"""Push dispatch — enrolment plan 6.2, FC-001 / FC-008 / O-20.

tauka-python decides; the `send-push` Edge Function holds the Firebase
credentials and reads `app.push_tokens` (Flutter writes them). Off until
PUSH_ENABLED is true: the app's notification stack is suspended (FC-008) and
the function is undeployed (O-20), so calling it would only fail.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def send(user_id: str, title: str, body: str, deep_link: str | None = None) -> bool:
    if not settings.push_enabled or not settings.supabase_url or not settings.supabase_key:
        return False
    payload = {"userId": user_id, "title": title[:120], "body": body[:240],
               "data": {"deepLink": deep_link} if deep_link else {}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{settings.supabase_url}/functions/v1/send-push", json=payload,
                                  headers={"Authorization": f"Bearer {settings.supabase_key}"})
        return r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("send-push failed: %s", exc)
        return False
