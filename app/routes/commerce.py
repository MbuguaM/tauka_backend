"""Institutions, community partners, gift cards (enrolment plan Track 10, §139).

Ops: `POST /commerce/orgs`, `POST /commerce/contracts`, `POST /commerce/contracts/{id}/activate`,
`GET /commerce/contracts/{id}/report`, `POST /commerce/partners`, `GET /commerce/partners/{id}/statement`.
Learner: `POST /commerce/join {code}`, `POST /commerce/gift-cards/redeem {code}`.
Anyone: `POST /commerce/gift-cards {tier, months, email, message?}` (checkout).
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.dependencies import get_current_user, require_admin
from app.services import commerce_service as svc
from app.services.commerce_service import CommerceRefused

router = APIRouter()
_STATUS = {"not_allowed": 403, "not_enabled": 403, "not_configured": 503, "no_seats": 409, "not_draft": 409,
           "no_such_contract": 404, "no_such_card": 404, "no_such_partner": 404}
Tier = Literal["learner_tier", "tutor_tier", "intensive_tier"]


def _refused(exc: CommerceRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Org(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    contact_email: EmailStr
    country_code: Optional[str] = Field(None, max_length=2)


class Contract(BaseModel):
    org_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    tier: Tier
    seats: int = Field(..., ge=1, le=500)
    starts_on: date
    ends_on: date
    price_minor: int = Field(..., ge=0)
    currency: str = Field(..., min_length=3, max_length=3)


class Partner(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    code: str = Field(..., pattern=r"^[A-Za-z0-9_-]{4,40}$")
    contact_email: Optional[EmailStr] = None
    commission_bp: int = Field(1000, ge=0, le=5000)


class Code(BaseModel):
    code: str = Field(..., min_length=4, max_length=20)


class Card(BaseModel):
    tier: Tier
    months: int = Field(..., ge=1, le=12)
    email: EmailStr
    message: Optional[str] = Field(None, max_length=300)


async def _call(fn):
    try:
        out = fn()
        return await out if hasattr(out, "__await__") else out
    except CommerceRefused as exc:
        _refused(exc)


@router.post("/orgs")
async def org(req: Org, admin_id: str = Depends(require_admin)):
    return await _call(lambda: svc.create_org(admin_id, req.name, str(req.contact_email), req.country_code))


@router.post("/contracts")
async def contract(req: Contract, admin_id: str = Depends(require_admin)):
    return await _call(lambda: svc.create_contract(admin_id, req.org_id, req.tier, req.seats, req.starts_on,
                                                   req.ends_on, req.price_minor, req.currency))


@router.post("/contracts/{contract_id}/activate")
async def activate(contract_id: str, admin_id: str = Depends(require_admin)):
    return await _call(lambda: svc.activate_contract(admin_id, contract_id))


@router.get("/contracts/{contract_id}/report")
async def report(contract_id: str, admin_id: str = Depends(require_admin)):
    return await _call(lambda: svc.employer_report(admin_id, contract_id))


@router.post("/partners")
async def partner(req: Partner, admin_id: str = Depends(require_admin)):
    return await _call(lambda: svc.create_partner(admin_id, req.name, req.code,
                                                  str(req.contact_email) if req.contact_email else None,
                                                  req.commission_bp))


@router.get("/partners/{partner_id}/statement")
async def statement(partner_id: str, start: date, end: date, admin_id: str = Depends(require_admin)):
    return await _call(lambda: svc.statement(admin_id, partner_id, start, end))


@router.post("/join")
async def join(req: Code, user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.join(user_id, req.code))


class Consent(BaseModel):
    granted: bool


@router.post("/report-consent")
async def report_consent(req: Consent, user_id: str = Depends(get_current_user)):
    """May my organisation see my progress and attendance? (employer_reporting)"""
    return svc.set_report_consent(user_id, req.granted)


@router.post("/gift-cards")
async def buy(req: Card):
    return await _call(lambda: svc.buy_card(req.tier, req.months, str(req.email), req.message))


@router.post("/gift-cards/redeem")
async def redeem(req: Code, user_id: str = Depends(get_current_user)):
    return await _call(lambda: svc.redeem(user_id, req.code))
