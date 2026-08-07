import logging
from fastapi import APIRouter, HTTPException
from app.models.schemas import GiftCheckoutRequest, GiftRefundRequest
from app.services import gift_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/checkout")
async def create_checkout(req: GiftCheckoutRequest):
    """PUBLIC — supporter creates a gift checkout (no account required)."""
    try:
        return await gift_service.create_gift_checkout(
            supporter_id=req.supporter_id,
            duration_months=req.duration_months,
            anonymous=req.anonymous,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Checkout failed: {exc}")


@router.get("/success")
async def gift_success(session_id: str):
    """PUBLIC — Stripe redirects here after successful gift payment."""
    gift_result = db.schema("app").table("gift_subscriptions").select(
        "id, status, duration_months, expires_at, student_id, anonymous"
    ).eq("stripe_payment_id", session_id).execute()

    if not gift_result.data:
        raise HTTPException(status_code=404, detail="Gift not found")

    gift = gift_result.data[0]
    return {
        "gift_id": gift["id"],
        "status": gift["status"],
        "duration_months": gift["duration_months"],
        "expires_at": gift.get("expires_at"),
    }


@router.post("/refund")
async def request_refund(req: GiftRefundRequest):
    """PUBLIC — supporter requests a refund within 7 days."""
    try:
        return await gift_service.request_refund(req.gift_id, req.supporter_email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Refund failed: {exc}")
