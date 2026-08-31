"""Account provisioning — the server half of "signup happens on the web".

**Architecture change, 2026-08-28 (developer decision).** All signup now happens
in tauka-react-web. The Flutter clients sign in only; a visitor without an
account is shown a message pointing them at the website. This route is what the
web calls once Supabase has created the `auth.users` row.

THE SPLIT, AND WHY IT IS WHERE IT IS.

| Step | Who | Why |
|---|---|---|
| Create the `auth.users` row | tauka-react-web → Supabase | The password goes from the browser to Supabase and never touches this service |
| Create `app.user_profiles` + `app.student` | **here**, service role | Both are tauka-flutter's tables and the web may not write them. `app.student` also carries `tier` and the Stripe columns, which only this service may ever write |
| Claim the tutor referral | here, via `tutor_referral_service` | Attribution is claim-on-first-signup and immutable; it belongs in the same call that creates the account |

This supersedes the arrangement where tauka-flutter's `_ensureUserProfile` and
`upsertStudent` created both rows during onboarding. That path stays as a repair
for accounts that predate this change — it is idempotent and only ever runs for
an already-authenticated user — but it is no longer how an account begins.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import get_current_user
from app.models.schemas import ProvisionAccountRequest, ProvisionAccountResponse
from app.services import provisioning_service
from app.services.rate_limit_service import check_anonymous_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()


def _ip_hash(request: Request) -> str:
    import hashlib
    ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


@router.post("/provision", response_model=ProvisionAccountResponse)
async def provision_account(
    req: ProvisionAccountRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Create the account rows for the caller. Idempotent.

    **The caller is taken from the verified JWT, never from the body.** There is
    no user id or email parameter on this route for exactly that reason: an
    endpoint that creates an account row for an id it was handed is an endpoint
    that creates an account row for anybody.

    **Safe to call more than once, and it is expected to be.** Email
    confirmation is enabled on this project, so `supabase.auth.signUp()` in the
    browser often returns no session — the user confirms by mail minutes later
    and arrives back signed in. The web therefore calls this after signup when a
    session exists, and again on first sign-in when one did not. A second call
    finds the rows already there and reports `already_provisioned`.

    Rate-limited by hashed IP despite being authenticated: a valid JWT is cheap
    to obtain here, since anyone can sign themselves up on the website.
    """
    allowed, reason = check_anonymous_rate_limit(
        _ip_hash(request), "account_provision", max_requests=20
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    try:
        return await provisioning_service.provision(
            user_id,
            first_name=req.first_name,
            age_confirmed=req.age_confirmed,
            referral_code=req.referral_code,
            source_platform=req.source_platform,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("provision failed for %s: %s", user_id, exc)
        # The auth user exists at this point and the rows do not, which is the
        # one state worth being explicit about: retrying is correct and safe.
        raise HTTPException(
            status_code=500,
            detail="Your sign-in was created but your profile was not. Try again.",
        )
