import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.followup import run_followup
from app.dependencies import get_current_user, require_tutor
from app.models.schemas import (
    ClaimReferralRequest,
    ClaimReferralResponse,
    CreateReferralRequest,
    CreateReferralResponse,
    ReferralLookupResponse,
    TutorReferralLinkResponse,
    TutorReferralLookupResponse,
)
from app.services import referral_service, tutor_referral_service
from app.services.rate_limit_service import check_anonymous_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_ip_hash(request: Request) -> str:
    import hashlib
    ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


@router.post("/create", response_model=CreateReferralResponse)
async def create_referral(
    req: CreateReferralRequest,
    request: Request,
):
    """PUBLIC — unauthenticated visitor creates a referral share link."""
    ip_hash = _get_ip_hash(request)
    allowed, reason = check_anonymous_rate_limit(ip_hash, "referral_create", max_requests=10)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    if not req.sender_name or not req.sender_email:
        raise HTTPException(
            status_code=400,
            detail="sender_name and sender_email are required for unauthenticated referrals",
        )

    try:
        result = await referral_service.create_referral(
            language=req.language,
            intent=req.intent,
            channel=req.channel,
            sender_type="visitor",
            sender_name=req.sender_name,
            sender_email=str(req.sender_email),
            sender_student_id=None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return CreateReferralResponse(**result)


@router.post("/create-authenticated", response_model=CreateReferralResponse)
async def create_referral_authenticated(
    req: CreateReferralRequest,
    user_id: str = Depends(get_current_user),
):
    """AUTHENTICATED — logged-in student creates a referral share link."""
    from app.services.supabase_client import supabase_admin as db

    # Pull sender details from student profile
    sender_name = req.sender_name
    sender_email = str(req.sender_email) if req.sender_email else None

    if not sender_name or not sender_email:
        try:
            user_result = db.auth.admin.get_user_by_id(user_id)
            if user_result and user_result.user:
                meta = user_result.user.user_metadata or {}
                sender_name = sender_name or meta.get("full_name", "")
                sender_email = sender_email or user_result.user.email
        except Exception:
            pass

    try:
        result = await referral_service.create_referral(
            language=req.language,
            intent=req.intent,
            channel=req.channel,
            sender_type="student",
            sender_name=sender_name,
            sender_email=sender_email,
            sender_student_id=user_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return CreateReferralResponse(**result)


@router.get("/mine")
async def my_referrals(user_id: str = Depends(get_current_user)):
    """AUTHENTICATED — return all referrals sent by this student."""
    return await referral_service.get_sent_referrals(user_id)


@router.get("/lookup/{referral_code}", response_model=ReferralLookupResponse)
async def lookup_referral(referral_code: str):
    """PUBLIC — called by the test landing page when a ref param is present."""
    await run_followup(referral_service.record_link_opened(referral_code),
                       label=f"record_link_opened {referral_code}")

    result = await referral_service.get_referral_by_code(referral_code)
    if not result:
        return ReferralLookupResponse(
            sender_name=None, intent=None, language="", valid=False
        )

    return ReferralLookupResponse(
        sender_name=result.get("sender_name"),
        intent=result.get("intent"),
        language=result.get("language", ""),
        valid=True,
    )


# ── Tutor referral attribution (plan Phase 1, spec §1.1) ──────────────────────
#
# Everything below concerns app.tutor_referral_link / app.referral_signup — a
# tutor's shareable signup code and the paying signups attributed to it. The
# routes above concern app.test_referrals, which attributes a placement-test
# share between two people who may never sign up. Two referral systems, one
# prefix; CONFLICT-039 records why both exist so nobody merges them.


@router.get("/tutor/link", response_model=TutorReferralLinkResponse)
async def get_tutor_referral_link(user_id: str = Depends(require_tutor)):
    """TUTOR — the caller's current code, minted on first ask.

    The tutor id comes from the verified JWT, never from the path. Spec §1.1
    writes this as `GET /tutor/{id}/referrals`; a client-supplied id on a route
    that returns revenue-share data is an IDOR waiting to be found, and every
    other route in this service already reads the subject from the token.
    """
    try:
        return await tutor_referral_service.get_or_create_link(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/tutor/link", response_model=TutorReferralLinkResponse)
async def rotate_tutor_referral_link(user_id: str = Depends(require_tutor)):
    """TUTOR — retire the current code and mint a new one.

    Rotation is soft: past attributions keep pointing at the retired link, so
    which code won a given signup stays answerable. The retired code stops
    attributing immediately, which is the reason to rotate one.
    """
    try:
        return await tutor_referral_service.rotate_link(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/tutor/lookup/{code}", response_model=TutorReferralLookupResponse)
async def lookup_tutor_referral(code: str, request: Request):
    """PUBLIC — called by the web signup page when `?tref=` is present.

    Returns the same shape for an unknown code and a retired one, so an
    enumeration attempt learns nothing from the difference. Rate-limited by IP
    hash for the same reason.
    """
    ip_hash = _get_ip_hash(request)
    allowed, reason = check_anonymous_rate_limit(
        ip_hash, "tutor_referral_lookup", max_requests=30
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    return await tutor_referral_service.lookup_code(code)


@router.post("/tutor/claim", response_model=ClaimReferralResponse)
async def claim_tutor_referral(
    req: ClaimReferralRequest,
    user_id: str = Depends(get_current_user),
):
    """AUTHENTICATED STUDENT — attribute this account to a tutor's code.

    Called once, after onboarding has created the `app.student` row. Every
    outcome that is the student's or the code's fault comes back 200 with a
    `reason`, not as an error: a mistyped code must never be able to strand
    someone mid-onboarding, and the client has nothing useful to do with a 4xx
    here beyond showing the same message.

    Attribution is claim-on-first-signup and immutable. A second call with a
    different code returns `already_attributed` and changes nothing.
    """
    try:
        return await tutor_referral_service.attribute_signup(
            student_id=user_id,
            code=req.code,
            source_platform=req.source_platform,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("claim_tutor_referral failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Could not record the referral")
