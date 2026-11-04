"""A tutor teaching from a phone (D16, §156).

Tutors teach mainly from the desktop app, but a phone is allowed for a change of scenery or a
non-standard class. When a tutor chooses it, the super admin and the head of tutors are told,
once per session. An owner holds `head_of_tutors` through app.admin_can().

The platform comes from the app, so this is a notice and not proof. It can't stop a join: the
app doesn't wait for it. Nothing about pay, rungs, matching or access may read these rows.
"""

import html
import logging
import uuid
from datetime import datetime, timezone

from app.config import settings
from app.services import email_service
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CAPABILITY = "head_of_tutors"
PLATFORM_NAMES = {"android": "an Android phone", "ios": "an iPhone"}


class OffDesktopRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _teaches(user_id: str, session: dict) -> bool:
    if session.get("tutor_id") == user_id:
        return True
    # app.cohort_session.lead_tutor_id may be a cover tutor; app.one_to_one_session.tutor_id is the tutor.
    video_session_id = session["id"]
    pods = (_t("cohort_session").select("lead_tutor_id").eq("video_session_id", video_session_id).limit(1).execute()).data or []
    if any(p.get("lead_tutor_id") == user_id for p in pods):
        return True
    ones = (_t("one_to_one_session").select("tutor_id").eq("video_session_id", video_session_id).limit(1).execute()).data or []
    return any(o.get("tutor_id") == user_id for o in ones)


def record(user_id: str, video_session_id: str, platform: str) -> dict | None:
    """The new row, or None if this tutor already reported this session."""
    if platform not in PLATFORM_NAMES:
        raise OffDesktopRefused("invalid_platform")
    try:
        uuid.UUID(video_session_id)
    except ValueError as exc:
        raise OffDesktopRefused("no_such_session") from exc
    # app.video_session (tauka-flutter): only id, tutor_id, title and scheduled_at are read.
    rows = (_t("video_session").select("id, tutor_id, title, scheduled_at").eq("id", video_session_id).limit(1).execute()).data or []
    if not rows:
        raise OffDesktopRefused("no_such_session")
    session = rows[0]
    if not _teaches(user_id, session):
        raise OffDesktopRefused("not_the_tutor")
    seen = (_t("off_desktop_session").select("id").eq("video_session_id", video_session_id).eq("tutor_id", user_id)
            .limit(1).execute()).data
    if seen:
        return None
    got = (_t("off_desktop_session").upsert(
        {"video_session_id": video_session_id, "tutor_id": user_id, "platform": platform},
        on_conflict="video_session_id,tutor_id", ignore_duplicates=True).execute()).data or []
    if not got:
        return None     # a second report got there first
    return {**got[0], "title": session.get("title"), "scheduled_at": session.get("scheduled_at")}


def _recipients() -> list[str]:
    from app.services.makeup_delivery import _email_of
    # app.admin_users (tauka-flutter): every active admin, filtered through app.admin_can().
    admins = [a["id"] for a in (_t("admin_users").select("id, deleted_at").execute()).data or []
              if not a.get("deleted_at")]
    emails = [e for e in (_email_of(a) for a in admins if hs.admin_can(a, CAPABILITY)) if e]
    return emails or ([settings.admin_email] if settings.admin_email else [])


def _tutor_name(user_id: str) -> str:
    # app.user_profiles (tauka-flutter): first_name is the name shown to others.
    rows = (_t("user_profiles").select("first_name").eq("id", user_id).limit(1).execute()).data or []
    return (rows[0].get("first_name") if rows else None) or "A tutor"


async def notify(row: dict) -> None:
    """Runs after the response. A failure leaves notified_at empty, and never reaches the tutor."""
    try:
        to = _recipients()
        if not to:
            logger.warning("off desktop: nobody to tell about session %s", row["video_session_id"])
            return
        name = html.escape(_tutor_name(row["tutor_id"]))
        title = html.escape(row.get("title") or "A live session")
        when = html.escape(str(row.get("scheduled_at") or "no time recorded"))
        result = await email_service.send_email(
            to, f"{name} is teaching a session from a phone",
            f"<p>{name} chose to teach this session from {PLATFORM_NAMES[row['platform']]} instead of the desktop app.</p>"
            f"<p>Session: {title}<br>Scheduled (UTC): {when}</p>"
            "<p>This is allowed for a change of scenery or a non-standard class. Tutors should teach mainly from the desktop app.</p>",
            tags=["off_desktop_session"])
        if (result or {}).get("status") != "failed":
            _t("off_desktop_session").update({"notified_at": datetime.now(timezone.utc).isoformat()}).eq("id", row["id"]).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("off desktop: could not send notice for session %s: %s", row.get("video_session_id"), exc)
