"""Payout administration and the tutor's own pay view — plan Phase 3a, §1.4.

**No route here moves money.** The furthest any of them goes is `approve`, and
`record-disbursement` writes down what a human already did by hand. §11.4:
automation is 3b, and it is blocked on O-06.

The split of authority is the point:

- A **tutor** reads their own lines and their own rate, including the inputs the
  rate was computed from. Pay that cannot be explained to the person receiving it
  is what §1.4's loyalty thesis exists to avoid.
- A **tutor never reads a run.** `payout_run.total_minor` is every tutor's pay
  added together. RLS says so; so does the absence of a route.
- Only an **admin** computes, approves, or records a disbursement — and approval
  records *who*, because a machine setting `approved_by` on a payment would be
  fabricating an approver.
"""

import logging
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import require_admin, require_tutor
from app.models.schemas import ApproveRunRequest, RecordDisbursementRequest
from app.services import payout_service

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Tutor-facing ──────────────────────────────────────────────────────────────

@router.get("/mine")
async def my_payouts(user_id: str = Depends(require_tutor)):
    """TUTOR — my payout lines and my current rate, with its inputs.

    `rate_pending` is true when nothing has computed a rate for this tutor —
    almost always a missing `tutor.country_code` or no rate card for their
    country. The client must render "your rate is being set up", never "0".
    """
    return await payout_service.tutor_payout_history(user_id)


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.post("/runs/compute")
async def compute_run(
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    admin_id: str = Depends(require_admin),
):
    """ADMIN — compute (or recompute) a period's runs, one per currency.

    Safe to call repeatedly: a run that is already `approved`, `disbursing` or
    `settled` is skipped rather than recomputed, because the numbers behind it
    are what somebody signed off on. Defaults to the month that just ended.
    """
    return await payout_service.compute_payout_run(period_start, period_end)


@router.post("/runs/approve")
async def approve_run(req: ApproveRunRequest, admin_id: str = Depends(require_admin)):
    """ADMIN — sign off a run. **The last step Phase 3a takes.**

    Nothing is disbursed by this. It marks the run and its lines as approved so
    an admin can pay them by hand and record the references.
    """
    result = await payout_service.approve_run(req.run_id, admin_id)
    if not result.get("approved"):
        raise HTTPException(status_code=409, detail=result.get("reason", "could not approve"))
    return result


@router.post("/lines/disburse")
async def record_disbursement(
    req: RecordDisbursementRequest,
    admin_id: str = Depends(require_admin),
):
    """ADMIN — record that a line was paid, by hand.

    **This is the manual `PayoutDriver` from §11.4.** `external_ref` holds an
    M-Pesa receipt or a bank reference identically, so a future automated driver
    writes the same column and nothing downstream changes.

    Pass the FX fields when the money settled in a currency other than the one
    the line is denominated in — the table refuses that combination without a
    rate, because otherwise a tutor's real pay drifts with the market and neither
    side can reconstruct why last month differed (§11.3).
    """
    result = await payout_service.record_disbursement(
        line_id=req.line_id,
        external_ref=req.external_ref,
        settled_amount_minor=req.settled_amount_minor,
        settled_currency=req.settled_currency,
        fx_rate=req.fx_rate,
        fx_source=req.fx_source,
    )
    if not result.get("settled"):
        raise HTTPException(status_code=400, detail=result.get("reason", "could not settle"))
    return result


@router.get("/float")
async def float_position(admin_id: str = Depends(require_admin)):
    """ADMIN — the reserve position per currency, and whether it reconciles.

    `agrees` false means the fold over the ledger disagrees with the running
    balance stamped on the last row. That is a real problem and never a display
    issue: both are derived from the same append-only table.
    """
    return await payout_service.check_float_balance()


@router.post("/float/append")
async def append_float(
    currency: str = Query(..., min_length=3, max_length=3),
    direction: str = Query(..., pattern="^(credit|debit)$"),
    amount_minor: int = Query(..., gt=0),
    reason: str = Query(...),
    admin_id: str = Depends(require_admin),
):
    """ADMIN — record a movement in the reserve.

    Topping the float up, or recording revenue landing in it. Append-only: a
    mistake is corrected with a compensating row (`reason=adjustment`), never by
    editing — the database enforces that with a trigger, for the service role
    too.
    """
    from app.services.supabase_client import supabase_admin as db
    try:
        result = db.schema("app").rpc("append_float_ledger", {
            "p_currency": currency.upper(),
            "p_direction": direction,
            "p_amount": amount_minor,
            "p_reason": reason,
            "p_ref_type": None,
            "p_ref_id": None,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("float append failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"entry_id": result.data, "currency": currency.upper()}
