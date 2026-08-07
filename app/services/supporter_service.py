import logging
from datetime import datetime, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


async def approve_for_student(session_id: str, note: str | None = None) -> dict:
    """Create a supporter record when a referred supporter approves the platform."""
    session_result = db.schema("app").table("test_sessions").select(
        "referrer_student_id, email, name"
    ).eq("id", session_id).execute()

    if not session_result.data:
        raise ValueError("Test session not found")

    session = session_result.data[0]
    referrer_student_id = session.get("referrer_student_id")
    if not referrer_student_id:
        raise ValueError("No referrer for this session")

    supporter_email = session.get("email", "")
    supporter_name = session.get("name", "")

    if not supporter_email:
        raise ValueError("Supporter email not captured yet")

    # Upsert supporter record
    try:
        result = db.schema("app").table("test_supporters").upsert({
            "supporter_email": supporter_email,
            "supporter_name": supporter_name,
            "student_id": referrer_student_id,
            "test_session_id": session_id,
            "status": "approved",
        }, on_conflict="supporter_email,student_id").execute()
        supporter_id = result.data[0]["id"]
    except Exception as exc:
        raise ValueError(f"Failed to create supporter record: {exc}") from exc

    return {"supporter_id": supporter_id}


async def opt_in_supporter(supporter_id: str) -> dict:
    """Supporter agrees to follow student progress."""
    db.schema("app").table("test_supporters").update({
        "status": "opted_in",
        "opted_in_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", supporter_id).execute()
    return {"supporter_id": supporter_id, "status": "opted_in"}


async def opt_out_supporter(supporter_id: str) -> dict:
    """Supporter stops milestone emails."""
    db.schema("app").table("test_supporters").update({
        "status": "opted_out",
    }).eq("id", supporter_id).execute()
    return {"supporter_id": supporter_id, "status": "opted_out"}


async def unsubscribe_supporter(supporter_id: str) -> dict:
    """Hard unsubscribe from all emails."""
    db.schema("app").table("test_supporters").update({
        "status": "unsubscribed",
    }).eq("id", supporter_id).execute()
    return {"supporter_id": supporter_id, "status": "unsubscribed"}


async def track_engagement(supporter_id: str, event: str) -> None:
    """Increment engagement score for valid events."""
    # ARC-002: email_open/email_click removed with the email share channel.
    valid_events = {"whatsapp_click"}
    if event not in valid_events:
        return
    try:
        result = db.schema("app").table("test_supporters").select(
            "engagement_score"
        ).eq("id", supporter_id).execute()
        if result.data:
            current = result.data[0].get("engagement_score", 0) or 0
            db.schema("app").table("test_supporters").update({
                "engagement_score": current + 1,
            }).eq("id", supporter_id).execute()
    except Exception as exc:
        logger.warning("track_engagement failed: %s", exc)


async def get_student_supporters(student_id: str) -> list[dict]:
    """Return all supporters for a student."""
    result = db.schema("app").table("test_supporters").select(
        "id, supporter_email, supporter_name, status, opted_in_at, student_visible, "
        "milestone_email_count, engagement_score, created_at"
    ).eq("student_id", student_id).order("created_at", desc=True).execute()
    return result.data or []


async def toggle_supporter_visibility(
    student_id: str,
    supporter_id: str,
    visible: bool,
) -> dict:
    """Student toggles milestone sharing for a specific supporter."""
    result = db.schema("app").table("test_supporters").select("id").eq(
        "id", supporter_id
    ).eq("student_id", student_id).execute()

    if not result.data:
        raise ValueError("Supporter not found or not linked to this student")

    db.schema("app").table("test_supporters").update({
        "student_visible": visible,
    }).eq("id", supporter_id).execute()

    return {"supporter_id": supporter_id, "student_visible": visible}
