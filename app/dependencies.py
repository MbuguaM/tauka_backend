import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.config import settings

_bearer = HTTPBearer()

# Supabase signs user tokens either with the project's shared secret (HS256, the
# legacy scheme production uses today) or with an asymmetric signing key
# (ES256/RS256) published at the project's JWKS endpoint — the default for new
# projects, for the local stack (`supabase start`), and for any project after
# Supabase migrates it to signing keys. Accepting only HS256 401'd every call in
# the local demo, and would do the same in production on that migration.
_ASYMMETRIC_ALGS = ("ES256", "RS256")
_jwks_client: jwt.PyJWKClient | None = None


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=600,
        )
    return _jwks_client


def _verification_key(token: str) -> tuple[object, list[str]]:
    """The key and the single algorithm a token must verify with.

    The algorithm comes from the token header but is never trusted on its own:
    HS256 is only ever checked against the shared secret, and an asymmetric
    token only against the public key the JWKS publishes for its `kid`, whose
    own algorithm must match. A token cannot pick a weaker pairing.
    """
    alg = jwt.get_unverified_header(token).get("alg")
    if alg == "HS256":
        if not settings.supabase_jwt_secret:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_JWT_SECRET is not configured",
            )
        return settings.supabase_jwt_secret, ["HS256"]
    if alg in _ASYMMETRIC_ALGS:
        if not settings.supabase_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_URL is not configured",
            )
        try:
            signing_key = _jwks().get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError:
            # Fail closed, and say why: an unreachable JWKS is not a bad token.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not fetch signing keys",
            )
        if signing_key.algorithm_name != alg:
            raise jwt.InvalidAlgorithmError("token alg does not match its key")
        return signing_key.key, [alg]
    raise jwt.InvalidAlgorithmError(f"unsupported alg {alg!r}")


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    Verify the Supabase-issued JWT and return the user's UUID (sub claim).
    Raises 401 if the token is missing, expired, or has an invalid signature.
    """
    try:
        key, algorithms = _verification_key(credentials.credentials)
        payload = jwt.decode(
            credentials.credentials,
            key,
            algorithms=algorithms,
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
    except (jwt.InvalidTokenError, jwt.PyJWKClientError):
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
