"""WhatsApp reminders (enrolment plan 6.1, §133; L-011), behind the
`whatsapp_reminders` flag.

Person: `GET /whatsapp` ({enabled:false} until the flag is on), `POST
/whatsapp/opt-in {phone}`, `POST /whatsapp/opt-out`.
Meta: `GET /whatsapp/webhook` (subscription check), `POST /whatsapp/webhook`
(signed; a STOP reply opts the sender out).
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import get_current_user
from app.services import whatsapp_service
from app.services.whatsapp_service import WhatsappRefused

router = APIRouter()


class OptIn(BaseModel):
    phone: str = Field(..., max_length=20)


@router.get("")
async def mine(user_id: str = Depends(get_current_user)):
    return whatsapp_service.mine(user_id)


@router.post("/opt-in")
async def opt_in(req: OptIn, user_id: str = Depends(get_current_user)):
    try:
        return whatsapp_service.opt_in(user_id, req.phone)
    except WhatsappRefused as exc:
        raise HTTPException(status_code=403 if exc.reason == "not_enabled" else 400, detail={"reason": exc.reason})


@router.post("/opt-out")
async def opt_out(user_id: str = Depends(get_current_user)):
    return whatsapp_service.opt_out(user_id)


@router.get("/webhook", response_class=PlainTextResponse)
async def verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
                 challenge: str = Query("", alias="hub.challenge")):
    if mode == "subscribe" and settings.whatsapp_verify_token and token == settings.whatsapp_verify_token:
        return challenge
    raise HTTPException(status_code=403)


@router.post("/webhook")
async def inbound(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    raw = await request.body()
    if not whatsapp_service.verify_signature(raw, x_hub_signature_256):
        raise HTTPException(status_code=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return {"opted_out": whatsapp_service.handle_inbound(payload)}
