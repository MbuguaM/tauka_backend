"""One way to tell a person something — enrolment plan Track 6.

    notify(user_id, kind, ref, title, body, link=None, whatsapp=None, email=None)

fans out to the channels that are switched on for that person:

  * PUSH (6.2) — through the `send-push` Edge Function, only when PUSH_ENABLED.
    The app's stack is suspended until Firebase is configured (FC-008); the
    function needs FIREBASE_PROJECT_ID / FIREBASE_SERVICE_ACCOUNT (O-20).
  * WHATSAPP (6.1) — only with the `whatsapp_reminders` flag on AND the person
    opted in (`whatsapp_contact`), as an approved template (`whatsapp` = the
    template name and its parameters).
  * EMAIL (ALL-11) — when the caller passes `email` = the template name and its
    data. The only channel on for everyone, and the only one reaching Windows.

`ref` + `kind` make it once-only: the `notice_log` row is claimed first, and
released if every channel failed so the next run retries. Never raises.
"""

import logging

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


def _log():
    return db.schema("app").table("notice_log")


def _claim(user_id: str, kind: str, ref: str) -> bool:
    try:
        _log().insert({"user_id": user_id, "kind": kind, "ref": ref}).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return False
        logger.error("notice claim failed (%s/%s): %s", kind, ref, exc)
        return False


def already(user_id: str, kind: str, ref: str) -> bool:
    try:
        return bool((_log().select("id").eq("user_id", user_id).eq("kind", kind).eq("ref", ref).limit(1)
                     .execute()).data)
    except Exception:  # noqa: BLE001
        return False


async def _send_email(user_id: str, template: str, data: dict) -> bool:
    from app.config import settings
    from app.services.email_service import send_template_email
    address = db.auth.admin.get_user_by_id(user_id).user.email
    if not address:
        return False
    result = await send_template_email(address, template, {"web_base_url": settings.web_base_url, **data},
                                       tags=["reminder"])
    return result.get("status") == "sent"


async def notify(user_id: str, kind: str, ref: str, title: str, body: str, link: str | None = None,
                 whatsapp: tuple[str, list[str]] | None = None,
                 email: tuple[str, dict] | None = None) -> list[str]:
    """Returns the channels that delivered (empty if nothing was switched on)."""
    from app.services import push_service, whatsapp_service
    if not user_id or not _claim(user_id, kind, ref):
        return []
    sent: list[str] = []
    try:
        if await push_service.send(user_id, title, body, link):
            sent.append("push")
    except Exception as exc:  # noqa: BLE001
        logger.warning("push %s failed: %s", kind, exc)
    if whatsapp:
        try:
            if await whatsapp_service.send_template(user_id, whatsapp[0], whatsapp[1]):
                sent.append("whatsapp")
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp %s failed: %s", kind, exc)
    if email:
        try:
            if await _send_email(user_id, email[0], email[1]):
                sent.append("email")
        except Exception as exc:  # noqa: BLE001
            logger.warning("email %s failed: %s", kind, exc)
    try:
        if sent:
            _log().update({"channels": sent}).eq("user_id", user_id).eq("kind", kind).eq("ref", ref).execute()
        else:
            # Nothing went out (nothing switched on, or both failed): release, so
            # a later run — perhaps with a channel switched on — can try again.
            _log().delete().eq("user_id", user_id).eq("kind", kind).eq("ref", ref).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("notice log update failed: %s", exc)
    return sent
