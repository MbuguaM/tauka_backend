import hashlib
import io
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from app.core.followup import run_followup
from app.dependencies import get_current_user, require_admin
from app.models.schemas import (
    Phase1SubmitRequest,
    EmailCaptureRequest,
    FinalSubmitRequest,
    ShareEventRequest,
    SeedQuestionsRequest,
    LoadQuestionBankRequest,
)
from app.services.rate_limit_service import check_anonymous_rate_limit
from app.services.test_service import (
    create_test_session,
    submit_phase_1,
    capture_email,
    submit_final,
)
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_ip_hash(request: Request) -> str:
    """Hash client IP for anonymous rate limiting (privacy-preserving)."""
    ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


@router.post("/{language}/start")
async def start_test(language: str, request: Request):
    """PUBLIC — create a new test session and return Phase 1 questions."""
    ip_hash = _get_ip_hash(request)
    allowed, reason = check_anonymous_rate_limit(ip_hash, "test_start", max_requests=5)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    referral_code = body.get("referral_code")

    # Resolve referrer from referral code if present (overrides direct referrer_student_id)
    referrer_student_id = body.get("referrer_student_id")
    if referral_code:
        from app.services.referral_service import get_referral_by_code
        ref = await get_referral_by_code(referral_code)
        if ref and ref.get("valid"):
            # Referrer will be set inside connect_test_session once session is created
            pass

    try:
        result = await create_test_session(
            language=language,
            ip_hash=ip_hash,
            user_agent=request.headers.get("user-agent", ""),
            referrer_student_id=referrer_student_id,
            referral_code=referral_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Link referral to session (sets test_started=true and copies sender_student_id).
    # Inline: this is a DB write the referral loop depends on, and deferring it
    # past the response meant it never happened on serverless — the referrer
    # would never be credited for a test they prompted.
    if referral_code:
        from app.services.referral_service import connect_test_session
        await run_followup(
            connect_test_session(referral_code, result["session_id"]),
            label=f"connect_test_session {referral_code}",
        )

    return result


@router.post("/submit-phase-1")
async def submit_phase_1_route(req: Phase1SubmitRequest):
    """PUBLIC — score Phase 1 answers."""
    try:
        return await submit_phase_1(req.session_id, req.answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/capture-email")
async def capture_email_route(req: EmailCaptureRequest):
    """PUBLIC — capture email and return Phase 2 questions."""
    try:
        return await capture_email(req.session_id, req.name, req.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/submit-final")
async def submit_final_route(req: FinalSubmitRequest):
    """PUBLIC — score full test and send result email."""
    try:
        result = await submit_final(req.session_id, req.answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Notify referral sender that their contact completed the test
    session_data = db.schema("app").table("test_sessions").select("referral_code").eq(
        "id", req.session_id
    ).execute()
    if session_data.data and session_data.data[0].get("referral_code"):
        from app.services.referral_service import handle_test_completion
        code = session_data.data[0]["referral_code"]
        await run_followup(handle_test_completion(code),
                           label=f"handle_test_completion {code}")

    return result


@router.post("/share")
async def log_share(req: ShareEventRequest):
    """PUBLIC — fire-and-forget share analytics."""
    try:
        db.schema("app").table("test_share_events").insert({
            "test_session_id": req.session_id,
            "channel": req.channel,
            "recipient_count": req.recipient_count,
        }).execute()
    except Exception as exc:
        logger.warning("Share log failed: %s", exc)
    return {"logged": True}


@router.get("/{language}/result/{session_id}")
async def get_result(language: str, session_id: str):
    """
    PUBLIC — re-serve a completed result to a cold page load.

    Must be declared BEFORE the /og-image route below or it would shadow it:
    FastAPI matches in declaration order and `/{session_id}` alone cannot
    swallow the longer path, but the reverse ordering trap is easy to
    reintroduce when adding routes here.

    Unauthenticated by design — the whole point is a shared link that opens for
    someone with no account. The uuid is the capability; `get_public_result`
    allowlists what that buys.
    """
    from app.services.test_service import get_public_result
    try:
        return await get_public_result(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Result not found")


@router.get("/{language}/result/{session_id}/og-image")
async def og_image(language: str, session_id: str):
    """PUBLIC — generate OG image for social sharing."""
    result = db.schema("app").table("test_sessions").select(
        "cefr_result, final_score, name"
    ).eq("id", session_id).execute()

    if not result.data or not result.data[0].get("cefr_result"):
        raise HTTPException(status_code=404, detail="Test result not found")

    session = result.data[0]
    cefr = session["cefr_result"]
    name = session.get("name", "")

    img_bytes = _generate_og_image(language, cefr, name)
    return Response(content=img_bytes, media_type="image/png")


def _generate_og_image(language: str, cefr: str, name: str) -> bytes:
    """Generate a simple OG image using Pillow."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    width, height = 1200, 630
    bg_color = (30, 50, 80)
    accent = (100, 180, 255)
    white = (255, 255, 255)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Background gradient effect (simple)
    for y in range(height):
        ratio = y / height
        r = int(bg_color[0] + (50 - bg_color[0]) * ratio)
        g = int(bg_color[1] + (70 - bg_color[1]) * ratio)
        b = int(bg_color[2] + (100 - bg_color[2]) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Title
    draw.text((100, 100), "Tauka", fill=accent, font=None)
    draw.text((100, 180), f"{language.capitalize()} Proficiency Test", fill=white, font=None)

    # CEFR level — large
    draw.text((100, 300), cefr, fill=accent, font=None)
    draw.text((100, 400), f"Proficiency Level", fill=white, font=None)

    if name:
        draw.text((100, 500), f"Result for {name}", fill=white, font=None)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.post("/admin/load-bank")
async def load_bank(
    req: LoadQuestionBankRequest,
    user_id: str = Depends(require_admin),
):
    """
    Admin-only — load a reviewed question bank file into app.test_questions.

    This is how an authored bank ships. `/admin/seed-questions` below is the
    AI drafting path, which produces candidates for review rather than
    publishable questions. Upserts on id, so re-running after an edit updates
    in place.
    """
    from app.services.question_bank_service import load_question_bank
    try:
        return await load_question_bank(req.path, req.language)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Bank file not found: {req.path}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Question bank load failed")
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/admin/seed-questions")
async def seed_questions(
    req: SeedQuestionsRequest,
    user_id: str = Depends(require_admin),
):
    """Admin-only — trigger question bank generation for a language."""
    from app.services.question_bank_service import seed_question_bank
    try:
        result = await seed_question_bank(req.language, req.lesson_range)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result
