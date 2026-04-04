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
    Creates a Daily.co room (idempotent) and returns a meeting token.

    Returns dict with keys: token, room_url, vendor
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


# ---------------------------------------------------------------------------
# LiveKit
# ---------------------------------------------------------------------------

def generate_livekit_token(user_id: str, room: str) -> dict:
    """
    Generates a signed LiveKit access token with full publish/subscribe grants.

    Returns dict with keys: token, room_url (None for LiveKit), vendor
    """
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
    return {"token": token, "room_url": None, "vendor": "livekit"}


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
