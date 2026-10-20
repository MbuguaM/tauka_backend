"""Video rooms for pod (group class) sessions — features-addendum plan §3 item 3.

Found 2026-10-20: nothing ever set `cohort_session.video_session_id`, so the
app's Join button (which waits for one, `CohortSession.canJoin`) could never
light up for a pod session. Make-ups, clinics and peer sessions all had rooms;
pods didn't.

Hourly (`GET /tasks/hourly`): for every scheduled pod session starting within
the next 24 hours that has no video session yet, create the Daily room, the
`app.video_session` row (tutor_id, title, scheduled_at, duration_minutes,
video_call_url — CONFLICT-055's columns) and a participant row per active
member, then point the pod session at it. Same shape as make-ups
(`makeup_delivery.provision_room`), with one difference: **no room, no row.**
A video session without a URL would enable Join and send everyone to an error
page, so when Daily isn't configured this does nothing and says so once a run.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

AHEAD = timedelta(hours=24)


def _t(name: str):
    return db.schema("app").table(name)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


async def provision(session: dict, cohort: dict) -> str | None:
    """The room for one pod session. Returns the video session id, or None."""
    from app.services import calling_service
    tutor = session.get("lead_tutor_id") or cohort.get("tutor_id")
    if not tutor:
        return None
    start = _ts(session["scheduled_at"])
    minutes = int(session.get("duration_minutes") or 50)
    url = await calling_service.create_daily_room(
        f"pod-{session['id'].replace('-', '')}", int((start + timedelta(minutes=minutes + 60)).timestamp()))
    if not url:
        return None
    created = (_t("video_session").insert({
        "tutor_id": tutor, "title": cohort.get("name") or "Pod session", "scheduled_at": start.isoformat(),
        "duration_minutes": minutes, "video_call_url": url,
    }).execute()).data or []
    vs_id = (created or [{}])[0].get("id")
    if not vs_id:
        return None
    members = [m["user_id"] for m in (_t("cohort_memberships").select("user_id, status").eq("cohort_id", cohort["id"])
               .eq("status", "active").execute()).data or []]
    if members:
        _t("video_session_participant").insert([{"session_id": vs_id, "student_id": m} for m in members]).execute()
    _t("cohort_session").update({"video_session_id": vs_id}).eq("id", session["id"]).execute()
    return vs_id


async def hourly(now: datetime | None = None) -> dict:
    from app.config import settings
    now = now or datetime.now(timezone.utc)
    rows = (_t("cohort_session").select("id, cohort_id, scheduled_at, duration_minutes, status, video_session_id, lead_tutor_id")
            .eq("status", "scheduled").is_("video_session_id", "null")
            .gte("scheduled_at", (now - timedelta(minutes=30)).isoformat())
            .lte("scheduled_at", (now + AHEAD).isoformat()).execute()).data or []
    out = {"due": len(rows), "made": 0}
    if not rows:
        return out
    if not settings.DAILY_API_KEY:
        logger.warning("pod rooms: %s session(s) due in 24 h but DAILY_API_KEY is empty: no rooms made", len(rows))
        out["skipped"] = "no_daily_key"
        return out
    cohorts = {c["id"]: c for c in (_t("cohorts").select("id, name, tutor_id, status")
               .in_("id", list({r["cohort_id"] for r in rows})).execute()).data or []}
    for s in rows:
        c = cohorts.get(s["cohort_id"])
        if not c or c.get("status") not in ("active", "forming"):
            continue
        try:
            if await provision(s, c):
                out["made"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("pod room for session %s failed: %s", s["id"], exc)
    return out
