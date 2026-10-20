"""WhatsApp reminders through a BSP — enrolment plan 6.1, L-011, §133.

Behind the `whatsapp_reminders` flag. A person gives a number FOR WhatsApp
with an explicit opt-in (`whatsapp_contact`, encrypted); every message is an
approved template (WhatsApp only allows templates outside a 24-hour reply
window). Replying STOP opts them out (`handle_inbound`).

Provider (env WHATSAPP_PROVIDER):
  * console (default) — logs what would be sent; no network, can't message anyone.
  * meta — the WhatsApp Cloud API: WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_VERIFY_TOKEN (webhook), WHATSAPP_APP_SECRET (signature).
A BSP (360dialog, Twilio…) is the same shape and slots in here.

Templates used (registered with Meta before going live; language en):
  session_reminder_24h  {1}=pod or session name {2}=local time
  session_reminder_1h   {1}=name {2}=local time
  phase0_deadline       {1}=intake {2}=deadline
  seat_offer            {1}=pod name
  cover_urgent          {1}=local time
  exchange_request      {1}=local time   (features addendum A9, 2026-10-20)
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.core.flags import is_ready
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

_E164 = re.compile(r"^\+?[1-9]\d{7,14}$")


class WhatsappRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t():
    return db.schema("app").table("whatsapp_contact")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fernet():
    from cryptography.fernet import Fernet
    if not settings.encryption_key:
        raise WhatsappRefused("not_configured")
    k = settings.encryption_key
    return Fernet(k.encode() if isinstance(k, str) else k)


def normalise(raw: str) -> str:
    digits = re.sub(r"[\s\-()]", "", raw or "")
    if not _E164.match(digits):
        raise WhatsappRefused("bad_phone")
    return digits.lstrip("+")


def phone_hash(msisdn: str) -> str:
    return hashlib.sha256(f"wa:{msisdn}".encode()).hexdigest()


def enabled() -> bool:
    return is_ready("whatsapp_reminders")


def mine(user_id: str) -> dict:
    if not enabled():
        return {"enabled": False}
    rows = (_t().select("phone_last4, opted_in_at, opted_out_at").eq("user_id", user_id).limit(1).execute()).data or []
    r = rows[0] if rows else None
    return {"enabled": True, "opted_in": bool(r and not r.get("opted_out_at")),
            "phone_last4": r["phone_last4"] if r else None}


def opt_in(user_id: str, phone: str) -> dict:
    if not enabled():
        raise WhatsappRefused("not_enabled")
    msisdn = normalise(phone)
    _t().upsert({"user_id": user_id, "phone_encrypted": _fernet().encrypt(msisdn.encode()).decode(),
                 "phone_last4": msisdn[-4:], "phone_hash": phone_hash(msisdn), "opted_in_at": _now(),
                 "opted_out_at": None, "updated_at": _now()}).execute()
    return mine(user_id)


def opt_out(user_id: str) -> dict:
    _t().update({"opted_out_at": _now(), "updated_at": _now()}).eq("user_id", user_id).execute()
    return mine(user_id) if enabled() else {"enabled": False}


def _number(user_id: str) -> str | None:
    rows = (_t().select("phone_encrypted, opted_out_at").eq("user_id", user_id).limit(1).execute()).data or []
    if not rows or rows[0].get("opted_out_at"):
        return None
    return _fernet().decrypt(rows[0]["phone_encrypted"].encode()).decode()


async def send_template(user_id: str, template: str, params: list[str]) -> bool:
    """True if sent. False when off, not opted in, or the provider refused."""
    if not enabled():
        return False
    number = _number(user_id)
    if not number:
        return False
    provider = settings.whatsapp_provider or "console"
    if provider == "console":
        logger.info("[whatsapp console] to …%s template=%s params=%s", number[-4:], template, params)
        return True
    if provider == "meta":
        body = {"messaging_product": "whatsapp", "to": number, "type": "template",
                "template": {"name": template, "language": {"code": "en"},
                             "components": [{"type": "body", "parameters": [
                                 {"type": "text", "text": str(p)[:60]} for p in params]}]}}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"https://graph.facebook.com/v19.0/{settings.whatsapp_phone_number_id}/messages",
                                  json=body, headers={"Authorization": f"Bearer {settings.whatsapp_token}"})
        if r.status_code >= 300:
            logger.warning("whatsapp send refused (%s): %s", r.status_code, r.text[:200])
        return r.status_code < 300
    logger.error("unknown WHATSAPP_PROVIDER %r", provider)
    return False


# ── Meta's webhook ──────────────────────────────────────────────────────────

def verify_signature(raw: bytes, header: str | None) -> bool:
    if not settings.whatsapp_app_secret:
        return False
    want = "sha256=" + hmac.new(settings.whatsapp_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header or "")


def handle_inbound(payload: dict) -> int:
    """Opt out anyone who replied STOP (or UNSUBSCRIBE). Returns how many."""
    n = 0
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for msg in (change.get("value") or {}).get("messages") or []:
                text = ((msg.get("text") or {}).get("body") or "").strip().upper()
                if text in ("STOP", "UNSUBSCRIBE", "STOP ALL"):
                    h = phone_hash(str(msg.get("from") or "").lstrip("+"))
                    got = (_t().update({"opted_out_at": _now(), "updated_at": _now()}).eq("phone_hash", h)
                           .is_("opted_out_at", "null").execute()).data or []
                    n += len(got)
    return n
