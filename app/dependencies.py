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
    """Raises 403 if the authenticated user does not have the tutor role."""
    from app.services.supabase_client import supabase_admin as db
    try:
        user_result = db.auth.admin.get_user_by_id(user_id)
        app_meta = (user_result.user.app_metadata or {}) if user_result and user_result.user else {}
        role = app_meta.get("role", "")
        if role != "tutor":
            raise HTTPException(status_code=403, detail="Tutor access required")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Could not verify tutor role")
    return user_id


async def require_admin(user_id: str = Depends(get_current_user)) -> str:
    """Raises 403 if the authenticated user does not have the admin role."""
    from app.services.supabase_client import supabase_admin as db
    try:
        user_result = db.auth.admin.get_user_by_id(user_id)
        app_meta = (user_result.user.app_metadata or {}) if user_result and user_result.user else {}
        role = app_meta.get("role", "")
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Could not verify admin role")
    return user_id
