"""Who may join a scheduled LiveKit session, and when.

Daily rooms are entered through their address. A LiveKit room is entered only with a token,
so this is the gate: someone in the session, near its time.

In the session means its tutor, a `video_session_participant`, or through what it belongs to:
a pod's members and leading tutor, or a 1-on-1's student and tutor. Pod members are read live,
because participant rows are only written once, when the room is made.

The session opens 15 minutes before its start, a little ahead of the app's 10-minute Join button
so a slow clock doesn't refuse someone, and closes an hour after its end, like Daily rooms did.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services import calling_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

OPENS_BEFORE = timedelta(minutes=15)
CLOSES_AFTER = timedelta(hours=1)
_FIELDS = "id, tutor_id, title, scheduled_at, duration_minutes, video_call_url"


class JoinRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _in_session(user_id: str, vs: dict) -> bool:
    if vs.get("tutor_id") == user_id:
        return True
    vs_id = vs["id"]
    if (_t("video_session_participant").select("student_id").eq("session_id", vs_id)
            .eq("student_id", user_id).limit(1).execute()).data:
        return True
    for pod in (_t("cohort_session").select("cohort_id, lead_tutor_id").eq("video_session_id", vs_id).limit(1).execute()).data or []:
        if pod.get("lead_tutor_id") == user_id:
            return True
        if (_t("cohort_memberships").select("user_id").eq("cohort_id", pod["cohort_id"]).eq("user_id", user_id)
                .eq("status", "active").limit(1).execute()).data:
            return True
    for one in (_t("one_to_one_session").select("student_id, tutor_id").eq("video_session_id", vs_id).limit(1).execute()).data or []:
        if user_id in (one.get("student_id"), one.get("tutor_id")):
            return True
    return False


def may_join_room(user_id: str, room: str) -> bool:
    """For a token asked for by room name. A room with no session belongs to nobody. Fails closed."""
    try:
        rows = (_t("video_session").select(_FIELDS).eq("video_call_url", f"{calling_service.LIVEKIT_SCHEME}{room}")
                .limit(1).execute()).data or []
        return bool(rows) and _in_session(user_id, rows[0])
    except Exception as exc:  # noqa: BLE001
        logger.error("livekit: could not check %s for %s: %s", room, user_id, exc)
        return False


def _display_name(user_id: str) -> str | None:
    # app.user_profiles (tauka-flutter): first_name is the name shown to others in a call.
    rows = (_t("user_profiles").select("first_name").eq("id", user_id).limit(1).execute()).data or []
    return (rows[0].get("first_name") if rows else None) or None


def join(user_id: str, video_session_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = (_t("video_session").select(_FIELDS).eq("id", video_session_id).limit(1).execute()).data or []
    if not rows:
        raise JoinRefused("no_such_session")
    vs = rows[0]
    if not _in_session(user_id, vs):
        raise JoinRefused("not_in_session")
    if not calling_service.is_livekit_room(vs.get("video_call_url")):
        raise JoinRefused("not_livekit")
    start = _ts(vs["scheduled_at"])
    closes = start + timedelta(minutes=int(vs.get("duration_minutes") or 60)) + CLOSES_AFTER
    if now < start - OPENS_BEFORE:
        raise JoinRefused("not_open")
    if now > closes:
        raise JoinRefused("closed")
    access = calling_service.livekit_access(vs["video_call_url"], user_id, _display_name(user_id), int(closes.timestamp()))
    if not access:
        raise JoinRefused("not_configured")
    return access
