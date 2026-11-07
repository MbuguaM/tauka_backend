"""
Multi-vendor calling service.

Supported vendors: daily | livekit

Vendor selection order (app.video_vendor_config, via get_active_vendor):
  1. Active row in app.video_vendor_config (Supabase). Production: livekit since 2026-11-07
  2. With none: "livekit" if its keys are set, else "daily"

HOW A ROOM IS STORED
  A session's room is one text column, whichever provider made it. Daily has a page address.
  LiveKit has no page and no room to create in advance: a room exists once someone joins with
  a token for its name. So a LiveKit room is stored as `livekit://<name>`, and the token, which
  is the only way in, comes from a route that checks who is asking. A session keeps the provider
  it was made with, so switching providers leaves existing Daily rooms working.
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
    # 2026-11-07 (developer): LiveKit is the default for every session. With no active row,
    # or the row unreadable, it's LiveKit when its keys are set, else Daily.
    return "livekit" if livekit_configured() else "daily"


LIVEKIT_SCHEME = "livekit://"


def livekit_configured() -> bool:
    return bool(settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_SECRET)


def is_livekit_room(url: str | None) -> bool:
    return bool(url) and url.startswith(LIVEKIT_SCHEME)


def can_make_rooms() -> bool:
    """Is the active provider's key set?"""
    return livekit_configured() if get_active_vendor() == "livekit" else bool(settings.DAILY_API_KEY)


async def create_room(name: str, expires_at: int, properties: dict | None = None) -> str | None:
    """The room for a scheduled session, from the active provider, or None. Never raises."""
    if get_active_vendor() == "livekit":
        return f"{LIVEKIT_SCHEME}{name}" if livekit_configured() else None
    return await create_daily_room(name, expires_at, properties=properties)


# ---------------------------------------------------------------------------
# Daily.co
# ---------------------------------------------------------------------------

async def create_daily_room(name: str, expires_at: int, properties: dict | None = None) -> str | None:
    """Create (or find) a Daily.co room and return its URL, or None.

    For scheduled sessions that need a room before anyone joins — make-ups
    (enrolment plan §2f item 3). Never raises: a room that couldn't be made is a
    tutor pasting a link, not a booking that failed. A duplicate name is handled:
    Daily answers 400 when the room already exists, so the existing room is fetched.
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

def generate_livekit_token(user_id: str, room: str, name: str | None = None, expires_at: int | None = None,
                           audio_only: bool = False, can_chat: bool = False,
                           can_share_screen: bool = False) -> dict:
    """
    Generates a signed LiveKit access token for one person in one room. `audio_only` (a partner in
    low-data mode) can't send video.

    2026-11-07: tutor-led sessions turn on `can_chat` (typed questions as data messages, and a
    raised hand as the person's own attribute) and, for the session's tutor, `can_share_screen`.
    Exchanges and peer sessions keep both off: their chat goes through the contact-detail masking
    of /messages/send, which a data message would skip.

    Returns dict with keys: token, room_url (the signalling address), vendor
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
        "exp": expires_at or now + 3600,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canPublishSources": (["microphone"] if audio_only else ["microphone", "camera"])
                                 + (["screen_share", "screen_share_audio"] if can_share_screen and not audio_only else []),
            "canPublishData": can_chat,
            "canUpdateOwnMetadata": can_chat,
            "canSubscribe": True,
        },
    }
    if name:
        payload["name"] = name[:40]
    token = jwt.encode(payload, settings.LIVEKIT_SECRET, algorithm="HS256")

    # `room_url` was None, which made the token useless on its own: a LiveKit
    # client needs the signalling websocket URL as well as the token, and unlike
    # Daily there is no API call that hands it back. It comes from config.
    return {
        "token": token,
        "room_url": settings.LIVEKIT_URL or None,
        "vendor": "livekit",
    }


def livekit_access(url: str, user_id: str, name: str | None, expires_at: int, audio_only: bool = False,
                   start_video_off: bool = False, can_chat: bool = False, host: bool = False) -> dict | None:
    """What a client needs to join a `livekit://` room, or None if LiveKit isn't configured.
    `host` is the session's tutor: they may share their screen, and the app shows them the lesson panel."""
    if not livekit_configured():
        return None
    room = url[len(LIVEKIT_SCHEME):]
    token = generate_livekit_token(user_id, room, name, expires_at, audio_only, can_chat=can_chat,
                                   can_share_screen=host)
    return {"server_url": token["room_url"], "token": token["token"], "room": room,
            "start_video_off": start_video_off or audio_only, "audio_only": audio_only,
            "can_chat": can_chat, "host": host}
