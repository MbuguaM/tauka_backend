import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.config import settings

_bearer = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    Verify the Supabase-issued JWT and return the user's UUID (sub claim).
    Raises 401 if the token is missing, expired, or has an invalid signature.
    """
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_JWT_SECRET is not configured",
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            # REQUIRED. Supabase issues user tokens with aud="authenticated".
            # PyJWT 2.x raises InvalidAudienceError when a token carries an
            # `aud` claim and the caller does NOT declare an expected audience
            # — so omitting this rejected every genuine user token with
            # "Invalid token", 401-ing every authenticated route in the API.
            # It went unnoticed because the test fixture minted tokens with no
            # `aud` claim, which is the one shape that passed.
            audience="authenticated",
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload["sub"]


async def require_tutor(user_id: str = Depends(get_current_user)) -> str:
    """Raises 403 if the authenticated user is not a tutor.

    Source of truth is `app.tutor` — the same table `app.is_tutor()` reads, so
    the API gate and every RLS policy agree on who a tutor is. It is NOT
    `app_metadata.role`: no account in `auth.users` carries that key, so the
    metadata check this replaced refused every caller including the one real
    tutor. See O-08 in `openbacklog.md`.

    `deleted_at IS NULL` is checked here even though `app.is_tutor()` omits it.
    The divergence is deliberate and one-directional: the API is stricter than
    the policies, so a soft-deleted tutor is refused rather than admitted.
    `status` is deliberately NOT checked — a suspended tutor must still be able
    to read their own standing and payout settings.
    """
    from app.services.supabase_client import supabase_admin as db
    try:
        result = (
            db.schema("app")
            .table("tutor")
            .select("id")
            .eq("id", user_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Fail closed. An unreachable database is not a grant.
        raise HTTPException(status_code=403, detail="Could not verify tutor role")
    if not (result.data or []):
        raise HTTPException(status_code=403, detail="Tutor access required")
    return user_id


async def require_admin(user_id: str = Depends(get_current_user)) -> str:
    """Raises 403 if the authenticated user is not an admin.

    Source of truth is `app.admin_users` — the same table `app.is_admin()`
    reads, and therefore the same table every admin RLS policy already enforces.

    This replaced a check on `app_metadata.role == 'admin'`, which matched no
    account in `auth.users` and so returned 403 to every caller of all 22
    admin-gated endpoints while the database recognised one admin (O-08).

    Note that the backlog entry described the old check as reading
    `user_metadata`; it read `app_metadata`. The distinction is load-bearing —
    `user_metadata` is client-writable, so a role check against it would let any
    user promote themselves. Neither is used now.
    """
    from app.services.supabase_client import supabase_admin as db
    try:
        result = (
            db.schema("app")
            .table("admin_users")
            .select("id")
            .eq("id", user_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Fail closed. An unreachable database is not a grant.
        raise HTTPException(status_code=403, detail="Could not verify admin role")
    if not (result.data or []):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id
