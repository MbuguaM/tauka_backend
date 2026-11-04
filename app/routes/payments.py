"""One-off payments (§145). See payment_service.

Provider: POST /payments/paystack/webhook   signed by Paystack, no auth
Demo:     GET  /payments/demo/{reference}   a local page standing in for checkout
          POST /payments/demo/{reference}   "Pay": settles exactly as a webhook would

Ops:      GET  /payments/admin/orders       paid and refunded one-offs (finance or ops)
          POST /payments/admin/refund       {reference, reason, revoke_access?}

          POST /payments/demo/plan/{student_id}/{event}   "renew" or "fail", standing in for Paystack

All demo routes answer 404 unless PAYMENT_PROVIDER=demo on a local API_BASE_URL.
"""

import html
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.dependencies import require_admin
from app.services import payment_service as svc
from app.services import billing_service, refund_service
from app.services.billing_service import BillingRefused
from app.services.refund_service import RefundRefused

logger = logging.getLogger(__name__)
router = APIRouter()
_STATUS = {"not_allowed": 403, "no_such_order": 404, "in_use": 409, "window_passed": 409, "not_refundable": 409,
           "payment_failed": 502}


class RefundRequest(BaseModel):
    reference: str = Field(..., max_length=60)
    reason: str = Field(..., min_length=3, max_length=300)
    revoke_access: bool = False


@router.get("/admin/orders")
async def admin_orders(admin_id: str = Depends(require_admin)):
    try:
        return {"orders": refund_service.orders(admin_id)}
    except RefundRefused as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


@router.post("/admin/refund")
async def admin_refund(req: RefundRequest, admin_id: str = Depends(require_admin)):
    try:
        return await refund_service.refund(admin_id, req.reference, req.reason, req.revoke_access)
    except RefundRefused as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


@router.post("/paystack/webhook")
async def paystack_webhook(request: Request):
    payload = await request.body()
    if not svc.paystack_signature_ok(payload, request.headers.get("x-paystack-signature", "")):
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad payload")
    try:
        await svc.handle_paystack_event(event)
    except Exception as exc:  # noqa: BLE001  (a 500 makes Paystack retry)
        logger.exception("Paystack webhook failed: %s", exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed")
    return {"received": True}


@router.post("/demo/plan/{student_id}/{event}")
async def demo_plan_event(student_id: str, event: str):
    if svc.provider() != "demo" or not svc.configured():
        raise HTTPException(status_code=404)
    try:
        return await billing_service.demo_event(student_id, event)
    except BillingRefused as exc:
        raise HTTPException(status_code=400, detail={"reason": exc.reason})


@router.get("/demo/{reference}", response_class=HTMLResponse)
async def demo_checkout(reference: str):
    order = svc.demo_order(reference)
    if not order:
        raise HTTPException(status_code=404)
    amount = f"{order['currency']} {order['amount_minor'] / 100:,.2f}"
    what = order["what"]
    already = order["status"] != "pending"
    action = html.escape(reference, quote=True)
    pay = ('<p>This order is already paid.</p>' if already else
           f'<form method="post" action="/payments/demo/{action}"><button>Pay {html.escape(amount)}</button></form>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Test checkout</title>
<style>body{{font-family:system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:0 16px;color:#222}}
button{{font:inherit;padding:12px 20px;background:#222;color:#fff;border:0;border-radius:6px;cursor:pointer}}
a{{display:inline-block;margin-top:16px}}.note{{background:#fff3cd;padding:10px 12px;border-radius:6px}}</style></head>
<body><p class="note">Test checkout. No real money moves, and this page only exists on a local server.</p>
<h1>{html.escape(amount)}</h1><p>{html.escape(what)}</p>{pay}
<a href="{html.escape(order['cancel_url'], quote=True)}">Cancel and go back</a></body></html>"""


@router.post("/demo/{reference}")
async def demo_pay(reference: str):
    order = svc.demo_order(reference)
    if not order:
        raise HTTPException(status_code=404)
    await svc.settle(reference, order["amount_minor"], order["currency"])
    return RedirectResponse(order["success_url"], status_code=303)
