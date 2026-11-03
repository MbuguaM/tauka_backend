import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import get_current_user
from app.models.schemas import ChangeSubscriptionRequest
from app.services import account_service
from app.services.billing_service import BillingRefused

logger = logging.getLogger(__name__)
router = APIRouter()
_STATUS = {"already_subscribed": 409, "not_configured": 503, "not_enabled": 403, "no_such_plan": 404,
           "plan_change_unavailable": 409, "not_ready": 409, "payment_failed": 502}


def _refused(exc: BillingRefused) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


@router.get("/subscription")
async def get_subscription(user_id: str = Depends(get_current_user)):
    """Full subscription state for the account portal."""
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
    """Free to paid opens a checkout, and paid to free cancels at period end."""
    try:
        return await account_service.change_subscription(
            user_id, req.new_tier, req.success_url, req.cancel_url, req.channel
        )
    except BillingRefused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Subscription change failed: {exc}")


@router.post("/subscription/cancel")
async def cancel_subscription(user_id: str = Depends(get_current_user)):
    """Cancel subscription at end of current billing period."""
    try:
        result = await account_service.change_subscription(
            user_id, "free_tier", None, None
        )
        return result
    except BillingRefused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cancellation failed: {exc}")


@router.post("/subscription/reactivate")
async def reactivate_subscription(user_id: str = Depends(get_current_user)):
    """Reactivate a cancelled subscription before the period ends."""
    try:
        return await account_service.reactivate_subscription(user_id)
    except BillingRefused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Reactivation failed: {exc}")


@router.post("/subscription/pay-now")
async def pay_now(user_id: str = Depends(get_current_user)):
    """Pay this month once: an M-Pesa learner's renewal, or anyone in grace."""
    try:
        return await account_service.pay_now(user_id)
    except BillingRefused as exc:
        raise _refused(exc)


@router.get("/billing-history")
async def billing_history(
    user_id: str = Depends(get_current_user),
    limit: int = Query(default=10, ge=1, le=100),
    starting_after: str | None = Query(default=None),
):
    """Paid months, newest first."""
    return await account_service.get_billing_history(user_id, limit, starting_after)


@router.get("/payment-method")
async def get_payment_method(user_id: str = Depends(get_current_user)):
    """Current card on file (brand, last4, expiry)."""
    return await account_service.get_payment_method(user_id)


@router.post("/update-payment-method")
async def update_payment_method(user_id: str = Depends(get_current_user)):
    """Paystack's hosted page for changing a card."""
    try:
        portal_url = await account_service.create_billing_portal_session(user_id)
        return {"portal_url": portal_url}
    except BillingRefused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Portal session failed: {exc}")
