import logging
from datetime import datetime, timedelta, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

_MILESTONE_TYPES = (
    "unit_complete",
    "streak_14",
    "first_ai_convo",
    "first_cohort_session",
    "cefr_level_up",
)


async def check_all_milestones() -> dict:
    """Daily cron: find new milestones for all students with opted-in supporters."""
    # Get students who have at least one opted-in supporter
    supporters_result = db.schema("app").table("test_supporters").select(
        "student_id"
    ).eq("status", "opted_in").execute()

    student_ids = list({s["student_id"] for s in (supporters_result.data or [])})

    milestones_found = 0
    notifications_queued = 0

    for student_id in student_ids:
        milestones = await detect_milestones_for_student(student_id)
        milestones_found += len(milestones)

        supporters_result = db.schema("app").table("test_supporters").select("id").eq(
            "student_id", student_id
        ).eq("status", "opted_in").eq("student_visible", True).execute()

        for milestone in milestones:
            for supporter in (supporters_result.data or []):
                nid = await queue_notification(
                    student_id,
                    supporter["id"],
                    milestone["type"],
                    milestone["data"],
                )
                if nid:
                    notifications_queued += 1

    return {
        "milestones_found": milestones_found,
        "notifications_queued": notifications_queued,
    }


async def detect_milestones_for_student(student_id: str) -> list[dict]:
    """Check all milestone types for a specific student."""
    milestones = []

    try:
        profile_result = db.schema("app").table("student").select("*").eq(
            "id", student_id
        ).execute()
        if not profile_result.data:
            return []
        profile = profile_result.data[0]

        # unit_complete: check if lessons_completed crossed a unit boundary
        lessons_completed = profile.get("lessons_completed", 0) or 0
        last_unit_milestone = profile.get("last_unit_milestone", 0) or 0
        units_completed = lessons_completed // 10
        if units_completed > (last_unit_milestone // 10):
            milestones.append({
                "type": "unit_complete",
                "data": {
                    "unit_number": units_completed,
                    "lessons_completed": lessons_completed,
                },
            })

        # streak_14: 14-day streak not previously notified
        streak = profile.get("current_streak", 0) or 0
        streak_notified = profile.get("streak_14_notified", False)
        if streak >= 14 and not streak_notified:
            milestones.append({
                "type": "streak_14",
                "data": {"streak_days": streak},
            })

        # first_ai_convo
        ai_convos = profile.get("ai_conversations", 0) or 0
        ai_convo_notified = profile.get("first_ai_convo_notified", False)
        if ai_convos >= 1 and not ai_convo_notified:
            milestones.append({
                "type": "first_ai_convo",
                "data": {"ai_conversations": ai_convos},
            })

        # first_cohort_session
        cohort_sessions = profile.get("cohort_sessions_attended", 0) or 0
        cohort_notified = profile.get("first_cohort_notified", False)
        if cohort_sessions >= 1 and not cohort_notified:
            milestones.append({
                "type": "first_cohort_session",
                "data": {"sessions_attended": cohort_sessions},
            })

        # cefr_level_up
        assessed_level = profile.get("assessed_level", "")
        last_level = profile.get("last_notified_level", "")
        if assessed_level and assessed_level != last_level:
            milestones.append({
                "type": "cefr_level_up",
                "data": {
                    "new_level": assessed_level,
                    "previous_level": last_level,
                },
            })

    except Exception as exc:
        logger.warning("detect_milestones_for_student %s failed: %s", student_id, exc)

    return milestones


async def queue_notification(
    student_id: str,
    supporter_id: str,
    milestone_type: str,
    milestone_data: dict,
) -> str | None:
    """Insert a pending milestone notification if conditions are met."""
    # Check min 14 days since last notification
    supporter_result = db.schema("app").table("test_supporters").select(
        "status, student_visible, last_notified_at"
    ).eq("id", supporter_id).execute()

    if not supporter_result.data:
        return None

    supporter = supporter_result.data[0]

    if supporter["status"] != "opted_in":
        return None
    if not supporter.get("student_visible", True):
        return None

    last_notified = supporter.get("last_notified_at")
    if last_notified:
        last_dt = datetime.fromisoformat(last_notified.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - last_dt < timedelta(days=14):
            return None

    # Check for duplicate pending notification
    dup = db.schema("app").table("milestone_notifications").select("id").eq(
        "student_id", student_id
    ).eq("supporter_id", supporter_id).eq("milestone_type", milestone_type).eq(
        "status", "pending"
    ).execute()

    if dup.data:
        return None

    result = db.schema("app").table("milestone_notifications").insert({
        "student_id": student_id,
        "supporter_id": supporter_id,
        "milestone_type": milestone_type,
        "milestone_data": milestone_data,
        "status": "pending",
    }).execute()

    return result.data[0]["id"] if result.data else None


async def process_pending_notifications() -> dict:
    """Send all pending milestone notifications."""
    from app.services.email_service import send_template_email

    pending_result = db.schema("app").table("milestone_notifications").select(
        "id, student_id, supporter_id, milestone_type, milestone_data"
    ).eq("status", "pending").limit(100).execute()

    sent = 0
    failed = 0
    skipped = 0

    for notif in (pending_result.data or []):
        try:
            supporter_result = db.schema("app").table("test_supporters").select(
                "supporter_email, supporter_name, engagement_score, gift_nudge_shown, "
                "gift_nudge_shown_at, student_id"
            ).eq("id", notif["supporter_id"]).execute()

            if not supporter_result.data:
                skipped += 1
                continue

            supporter = supporter_result.data[0]

            student_result = db.schema("app").table("student").select("tier").eq(
                "id", notif["student_id"]
            ).execute()
            student = student_result.data[0] if student_result.data else {}

            include_gift_nudge = should_include_gift_nudge(
                supporter, student, notif["milestone_type"]
            )

            template_name = "milestone_with_gift" if include_gift_nudge else "milestone_update"

            result = await send_template_email(
                to=supporter["supporter_email"],
                template_name=template_name,
                template_data={
                    "subject": f"Milestone update for your student",
                    "supporter_name": supporter["supporter_name"],
                    "milestone_type": notif["milestone_type"],
                    "milestone_data": notif["milestone_data"],
                    "supporter_id": notif["supporter_id"],
                },
            )

            if result["status"] == "sent":
                db.schema("app").table("milestone_notifications").update({
                    "status": "sent",
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", notif["id"]).execute()

                db.schema("app").table("test_supporters").update({
                    "last_notified_at": datetime.now(timezone.utc).isoformat(),
                    "milestone_email_count": (supporter.get("milestone_email_count", 0) or 0) + 1,
                }).eq("id", notif["supporter_id"]).execute()

                if include_gift_nudge:
                    db.schema("app").table("test_supporters").update({
                        "gift_nudge_shown": True,
                        "gift_nudge_shown_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", notif["supporter_id"]).execute()

                sent += 1
            else:
                db.schema("app").table("milestone_notifications").update({
                    "status": "failed"
                }).eq("id", notif["id"]).execute()
                failed += 1

        except Exception as exc:
            logger.error("Notification %s failed: %s", notif["id"], exc)
            failed += 1

    return {"sent": sent, "failed": failed, "skipped": skipped}


def should_include_gift_nudge(supporter: dict, student: dict, milestone_type: str) -> bool:
    """Determine if this milestone email should include a gift nudge."""
    eligible_types = {"unit_complete", "cefr_level_up"}
    if milestone_type not in eligible_types:
        return False
    if (supporter.get("engagement_score") or 0) < 3:
        return False
    if student.get("tier", "free") != "free":
        return False

    if supporter.get("gift_nudge_shown"):
        shown_at = supporter.get("gift_nudge_shown_at")
        if shown_at:
            shown_dt = datetime.fromisoformat(shown_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - shown_dt < timedelta(days=90):
                return False

    return True
