"""Gender as a matching preference or requirement — plan B8, 3.3, 5.2, §132.

Behind the `gender_matching` flag (L-010 OPEN): with the flag off, `mine`
reports `enabled: false`, writes are refused, and the matcher reads nothing.

A learner states their gender (self-described, or "prefer not to say") and,
for a tutor and for a pod, none / preference / requirement. A tutor states
their own gender. A requirement needs a stated gender.
"""

from datetime import datetime, timezone

from app.core.flags import is_ready
from app.services.supabase_client import supabase_admin as db

GENDERS = ("woman", "man", "nonbinary", "prefer_not")
LEVELS = ("none", "preference", "requirement")


class GenderRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t():
    return db.schema("app").table("gender_preference")


def enabled() -> bool:
    return is_ready("gender_matching")


def mine(user_id: str) -> dict:
    if not enabled():
        return {"enabled": False}
    rows = (_t().select("gender, same_gender_tutor, same_gender_pod").eq("user_id", user_id).limit(1)
            .execute()).data or []
    row = rows[0] if rows else {}
    return {"enabled": True, "gender": row.get("gender"),
            "same_gender_tutor": row.get("same_gender_tutor") or "none",
            "same_gender_pod": row.get("same_gender_pod") or "none"}


def set_mine(user_id: str, gender: str | None, tutor: str, pod: str) -> dict:
    if not enabled():
        raise GenderRefused("not_enabled")
    if (gender is not None and gender not in GENDERS) or tutor not in LEVELS or pod not in LEVELS:
        raise GenderRefused("bad_value")
    if "requirement" in (tutor, pod) and gender not in ("woman", "man", "nonbinary"):
        raise GenderRefused("gender_needed")
    _t().upsert({"user_id": user_id, "gender": gender, "same_gender_tutor": tutor, "same_gender_pod": pod,
                 "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    return mine(user_id)


def for_matching(user_ids: list[str]) -> dict[str, dict]:
    """{user_id: {gender, same_gender_tutor, same_gender_pod}}; empty when off."""
    if not user_ids or not enabled():
        return {}
    rows = (_t().select("user_id, gender, same_gender_tutor, same_gender_pod").in_("user_id", list(user_ids))
            .execute()).data or []
    return {r["user_id"]: r for r in rows}
