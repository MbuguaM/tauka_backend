"""
Multi-vendor calling service.

Supported vendors: daily | livekit

Vendor selection order:
  1. Caller-supplied vendor override (from CallRequest.vendor)
  2. Active row in app.video_vendor_config (Supabase)
  3. Falls back to "daily" if neither is available
"""

import time
import uuid
import httpx
import jwt
from app.config import settings
from app.core.supabase_client import supabase


# ---------------------------------------------------------------------------
# Vendor config helper
# ---------------------------------------------------------------------------

def get_active_vendor() -> str:
    """Read the active vendor from app.video_vendor_config."""
    try:
        result = (
            supabase.schema("app")
            .table("video_vendor_config")
            .select("vendor")
            .eq("is_active", True)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["vendor"]
    except Exception:
        pass
    return "daily"


# ---------------------------------------------------------------------------
# Daily.co
# ---------------------------------------------------------------------------

async def generate_daily_token(user_id: str, room: str) -> dict:
    """
    Creates a Daily.co room and returns a meeting token.

    Returns dict with keys: token, room_url, vendor

    UNVERIFIED ASSUMPTION, flagged 2026-08-24: the original docstring claimed
    "Daily returns the existing room if name already exists". That has not been
    checked against Daily's API, and `raise_for_status()` below means that if
    Daily instead returns 400 for a duplicate name, **the second person to join
    any session fails** while the first succeeds — an intermittent bug that would
    look like a network problem. Verify before the first real cohort session; if
    the API does 400, catch it and GET the existing room instead.
    """
    headers = {
        "Authorization": f"Bearer {settings.DAILY_API_KEY}",
        "Content-Type": "application/json",
    }
    exp = int(time.time()) + 3600

    async with httpx.AsyncClient(timeout=15) as client:
        # Create room — Daily returns the existing room if name already exists
        room_res = await client.post(
            "https://api.daily.co/v1/rooms",
            headers=headers,
            json={"name": room, "properties": {"exp": exp}},
        )
        room_res.raise_for_status()
        room_url = room_res.json()["url"]

        # Create meeting token
        token_res = await client.post(
            "https://api.daily.co/v1/meeting-tokens",
            headers=headers,
            json={
                "properties": {
                    "room_name": room,
                    "exp": exp,
                    "user_id": user_id,
                    "is_owner": False,
                }
            },
        )
        token_res.raise_for_status()
        token = token_res.json()["token"]

    return {"token": token, "room_url": room_url, "vendor": "daily"}


async def create_daily_room(name: str, expires_at: int, properties: dict | None = None) -> str | None:
    """Create (or find) a Daily.co room and return its URL, or None.

    For scheduled sessions that need a room before anyone joins — make-ups
    (enrolment plan §2f item 3). Never raises: a room that couldn't be made is a
    tutor pasting a link, not a booking that failed. Unlike
    `generate_daily_token`, a duplicate name is handled — Daily answers 400 when
    the room already exists, so the existing room is fetched instead.
    """
    if not settings.DAILY_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {settings.DAILY_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                "https://api.daily.co/v1/rooms", headers=headers,
                # Peer sessions (plan 5.3) pass start_video_off: audio-only by default.
                json={"name": name, "properties": {"exp": expires_at, **(properties or {})}},
            )
            if res.status_code == 200:
                return res.json().get("url")
            if res.status_code == 400 and "already" in res.text.lower():
                got = await client.get(f"https://api.daily.co/v1/rooms/{name}", headers=headers)
                if got.status_code == 200:
                    return got.json().get("url")
            import logging
            logging.getLogger(__name__).warning("Daily room %s: HTTP %s %s", name, res.status_code, res.text[:200])
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("Daily room %s failed: %s", name, exc)
    return None


async def create_daily_meeting_token(room_name: str, user_id: str, user_name: str | None, expires_at: int,
                                     audio_only: bool = False) -> str | None:
    """A per-person join token for a (private) Daily room, or None. Never raises.

    Exchange sessions (features addendum A7, 2026-10-20): the room is private, so
    this token is the only way in, and it carries the person's permissions. Video
    starts off for everyone. `audio_only` (a partner in low-data mode) means they
    can't send video and don't receive it either, so a call can't use their data
    on video even if the other person switches a camera on; screen sharing is
    never allowed. If Daily rejects the receive permission, the token is retried
    with the send restriction alone, which still keeps their camera off.
    """
    if not settings.DAILY_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {settings.DAILY_API_KEY}", "Content-Type": "application/json"}
    base = {"room_name": room_name, "user_id": user_id, "exp": expires_at, "is_owner": False,
            "start_video_off": True, "enable_screenshare": False}
    if user_name:
        base["user_name"] = user_name[:40]
    if audio_only:
        # Daily requires every media type in canReceive.base, and the custom
        # tracks as objects (checked against the API 2026-10-20).
        no_video = {"video": False, "audio": True, "screenVideo": False, "screenAudio": True,
                    "customVideo": {"*": False}, "customAudio": {"*": True}}
        attempts = [{**base, "permissions": {"canSend": ["audio"], "canReceive": {"base": no_video}}},
                    {**base, "permissions": {"canSend": ["audio"]}}]
    else:
        attempts = [{**base, "permissions": {"canSend": ["audio", "video"]}}]
    import logging
    log = logging.getLogger(__name__)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for props in attempts:
                res = await client.post("https://api.daily.co/v1/meeting-tokens", headers=headers,
                                        json={"properties": props})
                if res.status_code == 200:
                    return res.json().get("token")
                log.warning("Daily token for %s: HTTP %s %s", room_name, res.status_code, res.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("Daily token for %s failed: %s", room_name, exc)
    return None


# ---------------------------------------------------------------------------
# LiveKit
# ---------------------------------------------------------------------------

def generate_livekit_token(user_id: str, room: str) -> dict:
    """
    Generates a signed LiveKit access token with full publish/subscribe grants.

    Returns dict with keys: token, room_url (None for LiveKit), vendor
    """
    # Fail loudly on a missing secret. `jwt.encode(payload, "")` does NOT raise —
    # it signs with an empty key and returns a perfectly well-formed token that
    # LiveKit rejects at connect time, a long way from the cause. Until
    # 2026-08-24 this was the live behaviour, because the settings field was
    # named `livekit_secret` while .env carries LIVEKIT_API_SECRET.
    if not settings.LIVEKIT_SECRET or not settings.LIVEKIT_API_KEY:
        raise RuntimeError(
            "LiveKit is not configured: LIVEKIT_API_KEY and LIVEKIT_API_SECRET "
            "must both be set. Refusing to mint an unsignable token."
        )

    now = int(time.time())
    payload = {
        "iss": settings.LIVEKIT_API_KEY,
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": 0,
        "exp": now + 3600,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
        },
    }
    token = jwt.encode(payload, settings.LIVEKIT_SECRET, algorithm="HS256")

    # `room_url` was None, which made the token useless on its own: a LiveKit
    # client needs the signalling websocket URL as well as the token, and unlike
    # Daily there is no API call that hands it back. It comes from config.
    return {
        "token": token,
        "room_url": settings.LIVEKIT_URL or None,
        "vendor": "livekit",
    }


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

async def generate_call_token(
    user_id: str,
    room: str,
    vendor: str | None = None,
) -> dict:
    """
    Generate a call token for the appropriate vendor.

    vendor is resolved from: override → DB config → "daily"
    """
    resolved = vendor or get_active_vendor()

    if resolved == "livekit":
        return generate_livekit_token(user_id, room)

    # Default: Daily.co
    return await generate_daily_token(user_id, room)
