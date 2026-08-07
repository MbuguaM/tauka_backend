import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from app.dependencies import get_current_user
from app.models.schemas import CreateReferralRequest, CreateReferralResponse, ReferralLookupResponse
from app.services import referral_service
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
    bg: BackgroundTasks,
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
    bg: BackgroundTasks,
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
async def lookup_referral(referral_code: str, bg: BackgroundTasks):
    """PUBLIC — called by the test landing page when a ref param is present."""
    bg.add_task(referral_service.record_link_opened, referral_code)

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
