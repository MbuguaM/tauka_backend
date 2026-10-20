"""Cohort matching — offers, deferrals, and the admin's dry run.

Mounted at `/matching`. Three audiences, and they do not overlap:

- **Student** — the offers they hold, and accept/decline. Every route reads its
  subject from the verified JWT; there is no `{student_id}` in any path,
  because an offer is a statement about one person and a client-supplied id
  would be an IDOR.
- **Admin** — run a dry cycle, read the deferral brief, force a sweep.
- **Nobody else.** There is no tutor-facing route here: a tutor learns about a
  cohort when it exists, not while it is being proposed.

WHY ACCEPT IS A POST AND NOT A SUPABASE UPDATE. `app.match_offer` has no client
write policy (§86.4) on purpose. Accepting checks live capacity, invalidates the
student's other holds, creates the teaching link through `matching_service`, and
re-offers the seats it releases. A client that could write `status` would skip
all four.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_current_user, require_admin
from app.services import intake_service, offer_service

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Student ──────────────────────────────────────────────────────────────────

@router.get("/offers")
async def my_offers(user_id: str = Depends(get_current_user)):
    """STUDENT — the offers I hold, or why I have none.

    Returns `{offers: [...], deferral: {...} | null}`. Exactly one of the two is
    populated, and the deferral carries a plain-language reason rather than
    silence: plan §4b Stage 1 makes "we could not place you, and here is what
    would change that" part of what the student is owed.
    """
    return await offer_service.list_offers(user_id)


@router.post("/offers/{offer_id}/accept")
async def accept_offer(offer_id: str, user_id: str = Depends(get_current_user)):
    """STUDENT — take the seat.

    A refusal comes back as `{accepted: false, reason}` with a 200 for the
    reasons a student can act on — full, expired, already answered — and only an
    unknown offer is a 404. "This group filled while you were deciding" is not
    an error, it is the outcome the soft hold exists to make survivable.
    """
    result = await offer_service.accept(user_id, offer_id)
    if not result.get("accepted") and result.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail="Offer not found")
    if result.get("accepted") and result.get("cohort_status") == "active":
        # Plan 3.5: the pod is live — placement emails to whoever hasn't had one
        # (everyone, when this accept is the one that activated it). Bounded and
        # non-fatal; the daily job catches anything this misses.
        from app.core.followup import run_followup
        from app.services import placement_service
        await run_followup(placement_service.notify_cohort(result["cohort_id"]),
                           label=f"placement notices after accept in pod {result['cohort_id']}")
    return result


@router.post("/offers/{offer_id}/decline")
async def decline_offer(offer_id: str, user_id: str = Depends(get_current_user)):
    """STUDENT — say no to one offer. The others stand."""
    result = await offer_service.decline(user_id, offer_id)
    if not result.get("declined") and result.get("reason") == "not_open":
        raise HTTPException(status_code=404, detail="Offer not open")
    return result


@router.get("/status")
async def my_matching_status(user_id: str = Depends(get_current_user)):
    """STUDENT — am I matched, offered, or waiting, and what is missing?

    One call for the whole screen, because the three answers share a surface and
    a client that asks three questions will render a fourth state while they
    disagree.
    """
    offers = await offer_service.list_offers(user_id)
    readiness = intake_service.readiness(user_id)
    return {**offers, "readiness": readiness}


# ── Admin ────────────────────────────────────────────────────────────────────

@router.post("/cycles/dry-run")
async def dry_run(
    cycle_date: date | None = Query(default=None),
    notes: str | None = Query(default=None),
    _admin: str = Depends(require_admin),
):
    """ADMIN — run the matcher and write nothing but the proposal.

    Plan §4 and Phase 3: the bands and weights are tuned here, against real or
    synthetic pools, before a single student is placed. The run records one
    `app.intake_cycle` row with `mode='dry_run'`, its config snapshot, and the
    cohorts it *would* have formed.
    """
    return await intake_service.run_cycle(
        mode="dry_run", cycle_date=cycle_date, notes=notes)


@router.post("/cycles/run")
async def run_live(
    cycle_date: date | None = Query(default=None),
    _admin: str = Depends(require_admin),
):
    """ADMIN — run a live cycle now, out of band.

    The scheduled path is `/tasks/daily` on the 1st and 15th. This exists for
    the first real cycle and for recovery, and it is guarded by the same partial
    unique index: one live cycle per date, so calling it twice is refused rather
    than doubled.
    """
    return await intake_service.run_cycle(mode="live", cycle_date=cycle_date)


@router.post("/cycles/close")
async def close(cycle_id: str | None = Query(default=None),
                _admin: str = Depends(require_admin)):
    """ADMIN — sweep: dissolve under-filled cohorts, defer whoever is left."""
    return await offer_service.close_cycle(cycle_id)


@router.post("/offers/expire")
async def expire(_admin: str = Depends(require_admin)):
    """ADMIN — force the hold-expiry sweep."""
    return await offer_service.expire_due()


@router.get("/deferrals")
async def deferral_brief(
    limit: int = Query(default=200, ge=1, le=1000),
    _admin: str = Depends(require_admin),
):
    """ADMIN — the recruitment brief.

    Plan §4b: "the deferral table is the supply plan. Six NO_TUTOR_FOR_LEVEL at
    level 3 is a hiring brief with a number on it." Grouped by reason, because
    the individual rows are the student's explanation and the counts are the
    business's.
    """
    return intake_service.deferral_summary(limit)
