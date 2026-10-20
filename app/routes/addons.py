"""1:1 add-ons (enrolment plan 2.6, E-15, §129).

Learner: `GET /addons` (products, and the tutors to choose from when they have
none), `POST /addons/checkout`, `GET /addons/mine`. Payment is a one-time
Stripe checkout on the web; the webhook grants the sessions.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import addon_service
from app.services.addon_service import AddonRefused

router = APIRouter()
_STATUS = {"bad_product": 400, "not_configured": 503, "tutor_required": 400, "tutor_not_eligible": 400}


class AddonCheckout(BaseModel):
    product: Literal["one_to_one_50", "one_to_one_25", "bundle4_50", "bundle4_25"]
    tutor_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    success_url: Optional[str] = Field(None, max_length=500)
    cancel_url: Optional[str] = Field(None, max_length=500)


@router.get("")
async def options(user_id: str = Depends(get_current_user)):
    return addon_service.options(user_id)


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return {"purchases": addon_service.mine(user_id)}


@router.post("/checkout")
async def checkout(req: AddonCheckout, user_id: str = Depends(get_current_user)):
    from app.services.supabase_client import supabase_admin as db
    email = None
    try:
        u = db.auth.admin.get_user_by_id(user_id)
        email = u.user.email if u and u.user else None
    except Exception:  # noqa: BLE001
        pass
    try:
        return await addon_service.start_checkout(user_id, req.product, req.tutor_id, email,
                                                  req.success_url, req.cancel_url)
    except AddonRefused as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})
