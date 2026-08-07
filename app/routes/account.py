import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import get_current_user
from app.models.schemas import ChangeSubscriptionRequest
from app.services import account_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/subscription")
async def get_subscription(user_id: str = Depends(get_current_user)):
    """Full subscription state for the account portal — Supabase + Stripe merged."""
    try:
        return await account_service.get_full_subscription(user_id)
    except Exception as exc:
        logger.error("get_subscription failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not fetch subscription details")


@router.post("/subscription/change")
async def change_subscription(
    req: ChangeSubscriptionRequest,
    user_id: str = Depends(get_current_user),
):
    """Change subscription plan (free→paid creates checkout, paid→paid updates immediately)."""
    try:
        return await account_service.change_subscription(
            user_id, req.new_tier, req.success_url, req.cancel_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Subscription change failed: {exc}")


@router.post("/subscription/cancel")
async def cancel_subscription(user_id: str = Depends(get_current_user)):
    """Cancel subscription at end of current billing period."""
    try:
        result = await account_service.change_subscription(
            user_id, "free", None, None
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cancellation failed: {exc}")


@router.post("/subscription/reactivate")
async def reactivate_subscription(user_id: str = Depends(get_current_user)):
    """Reactivate a cancelled subscription before the period ends."""
    try:
        return await account_service.reactivate_subscription(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Reactivation failed: {exc}")


@router.get("/billing-history")
async def billing_history(
    user_id: str = Depends(get_current_user),
    limit: int = Query(default=10, ge=1, le=100),
    starting_after: str | None = Query(default=None),
):
    """Paginated Stripe invoice history."""
    return await account_service.get_billing_history(user_id, limit, starting_after)


@router.get("/payment-method")
async def get_payment_method(user_id: str = Depends(get_current_user)):
    """Current card on file (brand, last4, expiry)."""
    return await account_service.get_payment_method(user_id)


@router.post("/update-payment-method")
async def update_payment_method(user_id: str = Depends(get_current_user)):
    """Create a Stripe Billing Portal session to update the payment method."""
    try:
        portal_url = await account_service.create_billing_portal_session(user_id)
        return {"portal_url": portal_url}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Portal session failed: {exc}")
