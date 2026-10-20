"""M-Pesa (enrolment plan 2.4, §130).

Learner: `GET /mpesa` (is it available, prices, the saved number's last four),
`POST /mpesa/subscribe {tier, phone}`, `POST /mpesa/pay {phone?}`,
`GET /mpesa/payments/{id}` (poll after the prompt), `POST /mpesa/auto {on}`.
Daraja: `POST /mpesa/callback/{token}` — never trusted on its own; the payment
is re-checked with an STK query before anything is paid.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import mpesa_service
from app.services.mpesa_service import MpesaRefused

router = APIRouter()
_STATUS = {"not_configured": 503, "bad_phone": 400, "bad_tier": 400, "already_subscribed": 409, "no_phone": 400,
           "nothing_to_pay": 409, "push_failed": 502, "no_such_payment": 404, "bad_token": 403}


def _refused(exc: MpesaRefused):
    raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


class Subscribe(BaseModel):
    tier: Literal["learner_tier", "tutor_tier", "intensive_tier"]
    phone: str = Field(..., max_length=20)


class Pay(BaseModel):
    phone: Optional[str] = Field(None, max_length=20)


class Auto(BaseModel):
    on: bool


@router.get("")
async def info(user_id: str = Depends(get_current_user)):
    from app.services.supabase_client import supabase_admin as db
    payer = (db.schema("app").table("mpesa_payer").select("phone_last4, auto_prompt").eq("student_id", user_id)
             .limit(1).execute()).data or []
    return {"available": mpesa_service.configured(),
            "prices_kes": {t: mpesa_service.price_kes(t) for t in mpesa_service.TIERS},
            "phone_last4": payer[0]["phone_last4"] if payer else None,
            "auto_prompt": payer[0]["auto_prompt"] if payer else None}


@router.post("/subscribe")
async def subscribe(req: Subscribe, user_id: str = Depends(get_current_user)):
    from app.services.supabase_client import supabase_admin as db
    email = None
    try:
        u = db.auth.admin.get_user_by_id(user_id)
        email = u.user.email if u and u.user else None
    except Exception:  # noqa: BLE001
        pass
    try:
        return await mpesa_service.subscribe(user_id, req.tier, req.phone, email)
    except MpesaRefused as exc:
        _refused(exc)


@router.post("/pay")
async def pay(req: Pay, user_id: str = Depends(get_current_user)):
    try:
        return await mpesa_service.pay_open_invoice(user_id, req.phone)
    except MpesaRefused as exc:
        _refused(exc)


@router.get("/payments/{payment_id}")
async def payment(payment_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await mpesa_service.status(user_id, payment_id)
    except MpesaRefused as exc:
        _refused(exc)


@router.post("/auto")
async def auto(req: Auto, user_id: str = Depends(get_current_user)):
    from app.services.supabase_client import supabase_admin as db
    db.schema("app").table("mpesa_payer").update({"auto_prompt": req.on}).eq("student_id", user_id).execute()
    return {"auto_prompt": req.on}


@router.post("/callback/{token}")
async def callback(token: str, request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        await mpesa_service.callback(token, body)
    except MpesaRefused as exc:
        _refused(exc)
    except Exception as exc:  # noqa: BLE001
        # Daraja only needs an acknowledgement; a failed confirmation is retried
        # by the learner's status poll (GET /mpesa/payments/{id} confirms too).
        import logging
        logging.getLogger(__name__).error("mpesa callback: %s", exc)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
