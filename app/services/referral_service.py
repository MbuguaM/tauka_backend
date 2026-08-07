import logging
import secrets
import string
import urllib.parse
from datetime import datetime, timezone
from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# ── Share message templates ───────────────────────────────────────────────────

# ARC-002: the `email` share channel was removed. Only WhatsApp/copy-link
# templates remain; email_subject/email_body templates were deleted.
SHARE_MESSAGES = {
    "validate": {
        "whatsapp": (
            "I'm thinking about learning {language} on Tauka. "
            "You actually speak it — can you try their 10-minute test "
            "and let me know if it's legit? {link}"
        ),
    },
    "peer": {
        "whatsapp": (
            "Found this new site that teaches {language}, "
            "there's even a test to gauge your level. "
            "I'm intrigued, considering learning it. "
            "You should definitely check it out {link}"
        ),
    },
    "default": {
        "whatsapp": (
            "Check out this {language} test on Tauka — "
            "it's a quick way to see where you stand. {link}"
        ),
    },
}


# ── Code generation ───────────────────────────────────────────────────────────

def generate_referral_code(length: int = 8) -> str:
    """Generate a short URL-safe referral code, avoiding ambiguous chars."""
    alphabet = string.ascii_letters + string.digits
    alphabet = alphabet.translate(str.maketrans("", "", "0O1lI"))
    for _ in range(10):
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        # Optimistic uniqueness — caller handles DB unique constraint retries
        return code
    return secrets.token_urlsafe(length)[:length]


def build_whatsapp_url(message: str) -> str:
    """Build a wa.me deep link with pre-filled text (no phone — opens contact picker)."""
    return f"https://wa.me/?text={urllib.parse.quote(message)}"


def _build_share_url(language: str, code: str, intent: str | None) -> str:
    url = f"{settings.web_base_url}/test/{language}?ref={code}"
    if intent:
        url += f"&intent={intent}"
    return url


# ── Service functions ─────────────────────────────────────────────────────────

async def create_referral(
    language: str,
    intent: str | None,
    channel: str,
    sender_type: str,
    sender_name: str | None = None,
    sender_email: str | None = None,
    sender_student_id: str | None = None,
) -> dict:
    """Create a referral record and return the share link."""
    # ARC-002: recipient_email is never written — the email channel is gone.
    # Retry on code collision (extremely unlikely)
    for _ in range(5):
        code = generate_referral_code()
        try:
            result = db.schema("app").table("test_referrals").insert({
                "referral_code": code,
                "sender_type": sender_type,
                "sender_name": sender_name,
                "sender_email": sender_email,
                "sender_student_id": sender_student_id,
                "intent": intent,
                "channel": channel,
                "language": language,
            }).execute()
            referral_id = result.data[0]["id"]
            break
        except Exception as exc:
            if "unique" in str(exc).lower():
                continue
            raise
    else:
        raise RuntimeError("Could not generate a unique referral code")

    share_url = _build_share_url(language, code, intent)

    # Build WhatsApp URL
    whatsapp_url = None
    if channel == "whatsapp":
        template = SHARE_MESSAGES.get(intent or "default", SHARE_MESSAGES["default"])
        msg = template["whatsapp"].format(
            language=language.capitalize(),
            link=share_url,
            sender_name=sender_name or "Someone",
        )
        whatsapp_url = build_whatsapp_url(msg)

    # ARC-002: no email dispatch — only whatsapp/copy_link channels remain.

    return {
        "referral_id": referral_id,
        "referral_code": code,
        "share_url": share_url,
        "whatsapp_url": whatsapp_url,
    }


async def record_link_opened(referral_code: str) -> None:
    """Mark referral link as opened. Fire-and-forget."""
    try:
        result = db.schema("app").table("test_referrals").select(
            "id, link_opened"
        ).eq("referral_code", referral_code).execute()
        if result.data and not result.data[0]["link_opened"]:
            db.schema("app").table("test_referrals").update({
                "link_opened": True,
                "link_opened_at": datetime.now(timezone.utc).isoformat(),
            }).eq("referral_code", referral_code).execute()
    except Exception as exc:
        logger.warning("record_link_opened failed: %s", exc)


async def connect_test_session(referral_code: str, test_session_id: str) -> None:
    """Link a referral to a test session and set test_started=true."""
    try:
        result = db.schema("app").table("test_referrals").select(
            "id, sender_student_id"
        ).eq("referral_code", referral_code).execute()

        if not result.data:
            return

        referral = result.data[0]

        db.schema("app").table("test_referrals").update({
            "test_started": True,
            "test_session_id": test_session_id,
        }).eq("referral_code", referral_code).execute()

        # Copy sender_student_id → test_sessions.referrer_student_id
        if referral.get("sender_student_id"):
            db.schema("app").table("test_sessions").update({
                "referrer_student_id": referral["sender_student_id"],
                "referral_code": referral_code,
            }).eq("id", test_session_id).execute()
    except Exception as exc:
        logger.warning("connect_test_session failed: %s", exc)


async def handle_test_completion(referral_code: str) -> None:
    """Notify sender when their referred contact completes the test."""
    from app.services.email_service import send_email

    try:
        result = db.schema("app").table("test_referrals").select("*").eq(
            "referral_code", referral_code
        ).execute()
        if not result.data:
            return

        referral = result.data[0]
        if referral.get("sender_notified_on_completion"):
            return

        db.schema("app").table("test_referrals").update({
            "test_completed": True,
            "sender_notified_on_completion": True,
        }).eq("referral_code", referral_code).execute()

        intent = referral.get("intent")
        language = referral.get("language", "").capitalize()
        sender_name = referral.get("sender_name", "Someone")
        sender_type = referral.get("sender_type")
        sender_email = referral.get("sender_email")

        # Get completed result from session
        session_result = db.schema("app").table("test_sessions").select(
            "cefr_result, name"
        ).eq("id", referral.get("test_session_id")).execute()
        recipient_name = ""
        cefr = ""
        if session_result.data:
            recipient_name = session_result.data[0].get("name", "Your contact")
            cefr = session_result.data[0].get("cefr_result", "")

        signup_link = f"{settings.web_base_url}/signup?ref={referral_code}"
        test_link = _build_share_url(language.lower(), referral_code, intent)

        if sender_type == "visitor" and sender_email:
            if intent == "validate":
                body = (
                    f"<p>Hi {sender_name},</p>"
                    f"<p><strong>{recipient_name}</strong> completed the {language} assessment. "
                    f"They scored <strong>{cefr}</strong>.</p>"
                    f"<p>Ready to start learning yourself? "
                    f"<a href='{signup_link}'>Sign up for Tauka</a></p>"
                )
            else:
                body = (
                    f"<p>Hi {sender_name},</p>"
                    f"<p><strong>{recipient_name}</strong> took the {language} test too! "
                    f"They scored <strong>{cefr}</strong>.</p>"
                    f"<p>See how you compare — "
                    f"<a href='{test_link}'>take the test yourself</a>.</p>"
                )
            await send_email(
                to=sender_email,
                subject=f"{recipient_name} completed the {language} assessment",
                html_body=body,
            )

        elif sender_type == "student" and referral.get("sender_student_id"):
            student_id = referral["sender_student_id"]
            try:
                user_result = db.auth.admin.get_user_by_id(student_id)
                email = user_result.user.email if user_result and user_result.user else None
                if email:
                    if intent == "validate":
                        body = (
                            f"<p><strong>{recipient_name}</strong> took the assessment "
                            f"and scored <strong>{cefr}</strong>. "
                            f"Check your Supporters page to see if they approved Tauka.</p>"
                        )
                    else:
                        body = (
                            f"<p><strong>{recipient_name}</strong> took the {language} test! "
                            f"They scored <strong>{cefr}</strong>.</p>"
                        )
                    await send_email(
                        to=email,
                        subject=f"{recipient_name} completed the {language} test",
                        html_body=body,
                    )
            except Exception as exc:
                logger.warning("Student notification failed: %s", exc)

    except Exception as exc:
        logger.warning("handle_test_completion failed: %s", exc)


async def handle_approval(referral_code: str) -> None:
    """Notify sender when a validate-intent referral approves the platform."""
    from app.services.email_service import send_email

    try:
        result = db.schema("app").table("test_referrals").select("*").eq(
            "referral_code", referral_code
        ).execute()
        if not result.data:
            return

        referral = result.data[0]
        if referral.get("sender_notified_on_approval"):
            return

        db.schema("app").table("test_referrals").update({
            "approved": True,
            "sender_notified_on_approval": True,
        }).eq("referral_code", referral_code).execute()

        intent = referral.get("intent")
        if intent != "validate":
            return

        language = referral.get("language", "").capitalize()
        sender_name = referral.get("sender_name", "Someone")
        sender_type = referral.get("sender_type")
        sender_email = referral.get("sender_email")

        # Get supporter name from session
        session_id = None
        session_result = db.schema("app").table("test_sessions").select("id, name").eq(
            "id", referral.get("test_session_id")
        ).execute()
        supporter_name = "Your contact"
        if session_result.data:
            supporter_name = session_result.data[0].get("name", "Your contact")
            session_id = session_result.data[0]["id"]

        signup_link = f"{settings.web_base_url}/signup?ref={referral_code}"

        if sender_type == "visitor" and sender_email:
            body = (
                f"<p>Hi {sender_name},</p>"
                f"<p><strong>{supporter_name}</strong> tried the Tauka assessment "
                f"and recommends it for your {language} learning.</p>"
                f"<p>Ready to start? <a href='{signup_link}'>Sign up for Tauka</a></p>"
            )
            await send_email(
                to=sender_email,
                subject=f"{supporter_name} recommends Tauka for your {language} learning",
                html_body=body,
                reply_to=None,
            )

        elif sender_type == "student" and referral.get("sender_student_id"):
            student_id = referral["sender_student_id"]
            try:
                user_result = db.auth.admin.get_user_by_id(student_id)
                email = user_result.user.email if user_result and user_result.user else None
                if email:
                    body = (
                        f"<p><strong>{supporter_name}</strong> approved Tauka for you! "
                        f"They'll appear in your Supporters list.</p>"
                    )
                    await send_email(
                        to=email,
                        subject=f"{supporter_name} approved Tauka for you",
                        html_body=body,
                    )
            except Exception as exc:
                logger.warning("Student approval notification failed: %s", exc)

    except Exception as exc:
        logger.warning("handle_approval failed: %s", exc)


async def get_referral_by_code(referral_code: str) -> dict | None:
    """Look up a referral by code for the test landing page."""
    result = db.schema("app").table("test_referrals").select(
        "sender_name, intent, language"
    ).eq("referral_code", referral_code).execute()

    if not result.data:
        return None

    row = result.data[0]
    return {
        "sender_name": row.get("sender_name"),
        "intent": row.get("intent"),
        "language": row.get("language"),
        "valid": True,
    }


async def get_sent_referrals(student_id: str) -> list[dict]:
    """Return all referrals sent by an authenticated student."""
    result = db.schema("app").table("test_referrals").select(
        "referral_code, language, intent, channel, recipient_email, "
        "link_opened, test_started, test_completed, approved, created_at"
    ).eq("sender_student_id", student_id).order("created_at", desc=True).execute()
    return result.data or []

# ARC-002: the internal _send_referral_email helper was removed along with the
# email share channel. Referral notifications about completion/approval are
# still sent via the dedicated handlers above; the share *invite* itself is now
# whatsapp/copy_link only.
