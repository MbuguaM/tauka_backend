import logging
from datetime import datetime, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

TIER_RANK = {"free": 0, "learner": 1, "tutor": 2, "intensive": 3}


async def change_tier(
    student_id: str,
    new_tier: str,
    source: str,
    gift_id: str | None = None,
    preserve_original: bool = False,
) -> dict:
    """
    Single source of truth for changing a student's tier.
    Returns {"previous_tier": str, "new_tier": str, "source": str}.
    """
    result = db.schema("app").table("student").select(
        "tier, tier_source, original_tier, active_gift_id"
    ).eq("id", student_id).execute()

    if not result.data:
        # Create profile if it doesn't exist
        db.schema("app").table("student").insert({
            "id": student_id,
            "tier": new_tier,
            "tier_source": source,
        }).execute()
        logger.info("Created student profile %s with tier %s", student_id, new_tier)
        return {"previous_tier": "free", "new_tier": new_tier, "source": source}

    profile = result.data[0]
    previous_tier = profile["tier"]
    previous_source = profile["tier_source"]

    # Idempotent check
    if previous_tier == new_tier and previous_source == source:
        return {"previous_tier": previous_tier, "new_tier": new_tier, "source": source}

    # A gift cannot downgrade a self-paid tier
    if source == "gifted" and TIER_RANK.get(new_tier, 0) < TIER_RANK.get(previous_tier, 0):
        logger.info(
            "Gift tier %s would downgrade student %s from %s — skipping tier change",
            new_tier, student_id, previous_tier,
        )
        return {"previous_tier": previous_tier, "new_tier": previous_tier, "source": previous_source}

    update: dict = {
        "tier": new_tier,
        "tier_source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if preserve_original and not profile.get("original_tier"):
        update["original_tier"] = previous_tier

    if source == "gifted" and gift_id:
        update["active_gift_id"] = gift_id

    if source != "gifted":
        update["active_gift_id"] = None
        update["original_tier"] = None

    db.schema("app").table("student").update(update).eq("id", student_id).execute()

    logger.info(
        "Tier change: student=%s %s→%s source=%s",
        student_id, previous_tier, new_tier, source,
    )
    return {"previous_tier": previous_tier, "new_tier": new_tier, "source": source}


async def restore_original_tier(student_id: str) -> dict:
    """Restore the original tier after a gift expires."""
    result = db.schema("app").table("student").select(
        "tier, original_tier"
    ).eq("id", student_id).execute()

    if not result.data:
        return {"previous_tier": "free", "new_tier": "free", "source": "self"}

    profile = result.data[0]
    restore_to = profile.get("original_tier") or "free"

    return await change_tier(student_id, restore_to, "self")
