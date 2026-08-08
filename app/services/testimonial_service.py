import logging
from datetime import datetime, timedelta, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


async def check_testimonial_eligibility() -> list[dict]:
    """Find expired gifts eligible for testimonial request."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    # Expired gifts, 7+ days ago, with no existing testimonial request
    expired_gifts = db.schema("app").table("gift_subscriptions").select(
        "id, supporter_id, student_id, expires_at, activated_at"
    ).eq("status", "expired").lt("expires_at", cutoff.isoformat()).execute()

    created = []

    for gift in (expired_gifts.data or []):
        # Check no existing testimonial for this gift
        existing = db.schema("app").table("testimonial_requests").select("id").eq(
            "gift_id", gift["id"]
        ).execute()
        if existing.data:
            continue

        # Check supporter is still opted_in
        supporter_result = db.schema("app").table("test_supporters").select("status").eq(
            "id", gift["supporter_id"]
        ).execute()
        if not supporter_result.data or supporter_result.data[0]["status"] != "opted_in":
            continue

        # Check student activity during gifted period (best-effort)
        profile_result = db.schema("app").table("student").select(
            "lessons_completed, cohort_sessions_attended"
        ).eq("id", gift["student_id"]).execute()

        if profile_result.data:
            p = profile_result.data[0]
            lessons = p.get("lessons_completed", 0) or 0
            sessions = p.get("cohort_sessions_attended", 0) or 0
            # Need at least 1 unit completed or 2 sessions
            if lessons < 10 and sessions < 2:
                continue

        result = db.schema("app").table("testimonial_requests").insert({
            "supporter_id": gift["supporter_id"],
            "gift_id": gift["id"],
            "status": "pending",
        }).execute()

        if result.data:
            created.append(result.data[0])

    return created


async def send_pending_requests() -> dict:
    """Send testimonial request emails for pending rows."""
    from app.services.email_service import send_template_email

    pending = db.schema("app").table("testimonial_requests").select(
        "id, supporter_id"
    ).eq("status", "pending").execute()

    sent = 0

    for req in (pending.data or []):
        try:
            supporter_result = db.schema("app").table("test_supporters").select(
                "supporter_email, supporter_name"
            ).eq("id", req["supporter_id"]).execute()

            if not supporter_result.data:
                continue

            supporter = supporter_result.data[0]

            from app.config import settings
            submit_url = f"{settings.web_base_url}/testimonials/{req['id']}/submit"
            decline_url = f"{settings.web_base_url}/testimonials/{req['id']}/decline"

            result = await send_template_email(
                to=supporter["supporter_email"],
                template_name="testimonial_request",
                template_data={
                    "subject": "Would you share a quick word about Tauka?",
                    "supporter_name": supporter["supporter_name"],
                    "submit_url": submit_url,
                    "decline_url": decline_url,
                },
            )

            if result["status"] == "sent":
                db.schema("app").table("testimonial_requests").update({
                    "status": "sent",
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", req["id"]).execute()
                sent += 1

        except Exception as exc:
            logger.warning("Failed to send testimonial request %s: %s", req["id"], exc)

    return {"sent": sent}


async def submit_testimonial(
    request_id: str,
    quote_text: str,
    display_name: str,
    display_preference: str,
) -> dict:
    """Handle a supporter submitting their testimonial."""
    if not (20 <= len(quote_text) <= 500):
        raise ValueError("Quote must be between 20 and 500 characters")

    valid_preferences = {"full_name", "first_name_initial", "anonymous"}
    if display_preference not in valid_preferences:
        raise ValueError(f"display_preference must be one of {valid_preferences}")

    req_result = db.schema("app").table("testimonial_requests").select("status").eq(
        "id", request_id
    ).execute()

    if not req_result.data:
        raise ValueError("Testimonial request not found")

    if req_result.data[0]["status"] != "sent":
        raise ValueError(f"Request is not in 'sent' state (current: {req_result.data[0]['status']})")

    db.schema("app").table("testimonial_requests").update({
        "status": "submitted",
        "quote_text": quote_text,
        "display_name": display_name,
        "display_preference": display_preference,
    }).eq("id", request_id).execute()

    # Notify admin (best-effort). Empty ADMIN_EMAIL disables it — checked here so
    # opting out stays silent instead of logging a "no recipients" error per send.
    try:
        from app.config import settings
        if settings.admin_email:
            from app.services.email_service import send_email
            await send_email(
                to=settings.admin_email,
                subject="New testimonial submitted",
                html_body=f"<p>Request ID: {request_id}</p><p>Quote: {quote_text}</p>",
            )
    except Exception as exc:
        logger.warning("Admin testimonial notification failed: %s", exc)

    return {"request_id": request_id, "status": "submitted"}


async def decline_testimonial(request_id: str) -> dict:
    """Mark a testimonial request as declined."""
    db.schema("app").table("testimonial_requests").update({
        "status": "declined",
    }).eq("id", request_id).execute()
    return {"request_id": request_id, "status": "declined"}


async def auto_expire_unanswered() -> int:
    """Set sent requests older than 14 days to declined."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    result = db.schema("app").table("testimonial_requests").update({
        "status": "declined",
    }).eq("status", "sent").lt("sent_at", cutoff).execute()

    count = len(result.data) if result.data else 0
    logger.info("Auto-expired %d testimonial requests", count)
    return count


async def get_published_testimonials(language: str | None = None) -> list[dict]:
    """Return published testimonials for the marketing site."""
    query = db.schema("app").table("testimonial_requests").select(
        "id, quote_text, display_name, display_preference, published_at, supporter_id"
    ).eq("status", "published").order("published_at", desc=True)

    result = query.execute()
    testimonials = []

    for row in (result.data or []):
        display = _format_display_name(row)
        testimonials.append({
            "quote": row["quote_text"],
            "display_name": display,
        })

    return testimonials


def _format_display_name(row: dict) -> str:
    pref = row.get("display_preference", "anonymous")
    name = row.get("display_name", "")
    if pref == "full_name":
        return name
    if pref == "first_name_initial":
        parts = name.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[-1][0]}."
        return name
    return "Anonymous supporter"
