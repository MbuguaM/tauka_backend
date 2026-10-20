"""The founding-pod waitlist (enrolment plan H2, §95; double opt-in §97).

`POST /waitlist` is public and unauthenticated: a visitor joins without an
account. It answers `{ok: true}` whatever happened — new address, known address,
confirmed or not, or a failed write — and sends any confirmation email in the
background so every outcome also takes the same time. See `enrolment_service`.

`POST /waitlist/confirm` and `POST /waitlist/remove` take the token from the
confirmation email. They are POSTs on purpose: the email links open website
pages with a button, because mail scanners prefetch GET links.
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.models.schemas import WaitlistRequest, WaitlistTokenRequest
from app.routes.auth import _ip_hash
from app.services import enrolment_service
from app.services.rate_limit_service import check_anonymous_rate_limit

router = APIRouter()


def _limit(request: Request, endpoint: str, max_requests: int) -> None:
    allowed, reason = check_anonymous_rate_limit(
        _ip_hash(request), endpoint, max_requests=max_requests
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)


@router.post("")
async def join_waitlist(req: WaitlistRequest, request: Request, background: BackgroundTasks):
    """PUBLIC — add or refresh a waitlist entry and mail a confirmation link."""
    _limit(request, "waitlist", 5)
    if req.child_age_band is not None:
        # Plan 1.8: refused until the developer turns the family waitlist on (L-012).
        from app.core.flags import is_ready
        if not is_ready("family_waitlist"):
            raise HTTPException(status_code=403, detail={"reason": "family_waitlist_closed"})

    token = enrolment_service.join_waitlist(
        email=str(req.email),
        goal=req.goal,
        band=req.band,
        pace_hours=req.pace_hours,
        email_opt_in=req.email_opt_in,
        source=req.source,
        child_age_band=req.child_age_band,
        banner_arm=req.banner_arm,
    )
    if token:
        background.add_task(enrolment_service.send_confirmation, str(req.email), token)
    return {"ok": True}


@router.post("/confirm")
async def confirm_waitlist(req: WaitlistTokenRequest, request: Request):
    """PUBLIC — confirm an entry. `ok` says whether the token was valid: it is
    32 random bytes, so saying so tells a guesser nothing."""
    _limit(request, "waitlist_token", 20)
    return {"ok": enrolment_service.confirm_waitlist(req.token)}


@router.post("/remove")
async def remove_waitlist(req: WaitlistTokenRequest, request: Request):
    """PUBLIC — delete an entry ("not you?" / "remove me")."""
    _limit(request, "waitlist_token", 20)
    return {"ok": enrolment_service.remove_waitlist(req.token)}
