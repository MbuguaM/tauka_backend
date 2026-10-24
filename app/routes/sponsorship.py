"""Sponsorship (§144, flag 'sponsorship'). See sponsorship_service.

Public:  GET  /sponsorship/waitlist                  the listed learners (no account data)
         POST /sponsorship/checkout                  sponsor a listed learner or a named person
                                                     (a bearer token, if sent, links a past sponsored
                                                     learner's seat: paying it forward)
Learner: POST /sponsorship/requests                  ask for a sponsored seat
         GET  /sponsorship/requests/mine             my application and my sponsored seats
         POST /sponsorship/requests/mine/withdraw
         POST /sponsorship/claim {token}             claim a seat someone named me for
Ops:     GET  /sponsorship/admin/requests?status=    the review queue
         POST /sponsorship/admin/requests/{id}/review {decision: list|decline, reason?}
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from app.dependencies import get_current_user
from app.routes.auth import _ip_hash
from app.services import sponsorship_service as svc
from app.services.rate_limit_service import check_anonymous_rate_limit
from app.services.sponsorship_service import SponsorshipRefused

router = APIRouter()
_optional_bearer = HTTPBearer(auto_error=False)
_STATUS = {"not_enabled": 403, "not_allowed": 403, "not_configured": 503, "taken": 409, "already_open": 409,
           "no_such_request": 404, "no_such_seat": 404}
Tier = Literal["learner_tier", "tutor_tier", "intensive_tier"]


def _refused(exc: SponsorshipRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


def _optional_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer)) -> Optional[str]:
    if not credentials:
        return None
    try:
        return get_current_user(credentials)
    except HTTPException:
        return None


class Apply(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=40)
    country_code: Optional[str] = Field(None, pattern=r"^[A-Za-z]{2}$")
    reason: Literal["heritage", "partner", "relocating", "faith", "work", "other"]
    story: str = Field(..., min_length=40, max_length=600)
    tier_wanted: Tier = "tutor_tier"
    months_wanted: Literal[3, 6, 12] = 12
    consent: Literal[True]


class Checkout(BaseModel):
    mode: Literal["waitlist", "named"]
    sponsor_email: EmailStr
    sponsor_name: Optional[str] = Field(None, max_length=60)
    anonymous: bool = False
    message: Optional[str] = Field(None, max_length=300)
    request_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    months: Optional[int] = Field(None, ge=1, le=12)
    recipient_email: Optional[EmailStr] = None
    recipient_name: Optional[str] = Field(None, max_length=60)
    tier: Optional[Tier] = None


class Claim(BaseModel):
    token: str = Field(..., min_length=10, max_length=100)


class Review(BaseModel):
    decision: Literal["list", "decline"]
    reason: Optional[str] = Field(None, max_length=300)


async def _call(fn):
    try:
        out = fn()
        return await out if hasattr(out, "__await__") else out
    except SponsorshipRefused as exc:
        _refused(exc)


@router.get("/waitlist")
async def waitlist():
    return await _call(lambda: {"learners": svc.public_list()})


@router.post("/checkout")
async def checkout(req: Checkout, request: Request, user_id: Optional[str] = Depends(_optional_user)):
    allowed, reason = check_anonymous_rate_limit(_ip_hash(request), "sponsorship_checkout", max_requests=10)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)
    return await _call(lambda: svc.checkout(
        req.mode, str(req.sponsor_email), request_id=req.request_id, months=req.months,
        recipient_email=str(req.recipient_email) if req.recipient_email else None,
        recipient_name=req.recipient_name, tier=req.tier, sponsor_name=req.sponsor_name,
        anonymous=req.anonymous, message=req.message, sponsor_user_id=user_id))


@router.post("/requests")
async def apply(req: Apply, user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.apply(user_id, req.first_name, req.country_code, req.reason, req.story,
                                         req.tier_wanted, req.months_wanted))


@router.get("/requests/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.mine(user_id))


@router.post("/requests/mine/withdraw")
async def withdraw(user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.withdraw(user_id))


@router.post("/claim")
async def claim(req: Claim, user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.claim(user_id, req.token))


@router.get("/admin/requests")
async def admin_queue(status: Literal["pending", "listed", "sponsored", "declined", "withdrawn"] = "pending",
                      user_id: str = Depends(get_current_user)):
    return await _call(lambda: {"requests": svc.queue(user_id, status)})


@router.post("/admin/requests/{request_id}/review")
async def admin_review(request_id: str, req: Review, user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.review(user_id, request_id, req.decision, req.reason))
