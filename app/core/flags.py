"""Server-side feature flags — `app.feature_flag` (platform_config, tauka-python).

The web reads `src/config/featureStatus.js` to show or hide a feature; this is
the same switch on the server, so a feature that waits on a legal or business
decision is refused by the API too, not just hidden. A missing row, a read
error or any status but 'ready' means OFF: these gates fail closed.

Seeded 'coming' by §131 for the items built ahead of their blocker
(enrolment plan, "build the rest", 2026-10-19).
"""

import logging

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


def is_ready(key: str) -> bool:
    try:
        rows = (db.schema("app").table("feature_flag").select("status").eq("key", key).limit(1).execute()).data
    except Exception as exc:  # noqa: BLE001
        logger.warning("feature flag %s unreadable: %s", key, exc)
        return False
    return bool(isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get("status") == "ready")
