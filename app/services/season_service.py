"""Season channel and intake welcomes — enrolment plan 6.3 (and 3.5's channel
invite), §133, CONFLICT-061.

A cohort is a SEASON ("Bega 2019"): every pod formed in it. tauka-python posts,
as `app.broadcast` rows the app already shows:

  * a WELCOME to each pod when it goes live (target_type 'cohort', the pod),
  * one SEASON post per formation (target_type 'season', the season id),
    readable by everyone with an active seat in a pod formed in that season
    (the additive RLS policy in §133).

Once each, recorded in `notice_log` (user_id = the pod or intake id; that
column carries no foreign key for this reason). Daily, after the placement
notices. No names: counts and the season only.
"""

import logging
from datetime import datetime, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)


def _t(name: str):
    return db.schema("app").table(name)


def _once(owner_id: str, kind: str) -> bool:
    try:
        _t("notice_log").insert({"user_id": owner_id, "kind": kind, "ref": owner_id, "channels": ["broadcast"]}).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _post(content: str, target_type: str, target_id: str) -> None:
    # CONFLICT-061: tauka-python writes broadcast rows (author: none, the platform).
    _t("broadcast").insert({"content": content, "target_type": target_type, "target_id": target_id,
                            "published_at": datetime.now(timezone.utc).isoformat()}).execute()


def _season_of_pod(pod: dict) -> dict | None:
    if not pod.get("start_date"):
        return None
    rows = (_t("intake").select("id, season_id, season:season_id(display_name)")
            .eq("formation_date", str(pod["start_date"])[:10]).limit(1).execute()).data or []
    return rows[0] if rows else None


def welcome_pods() -> int:
    """Live pods with no welcome yet."""
    n = 0
    for pod in (_t("cohorts").select("id, name, status, start_date").eq("status", "active").execute()).data or []:
        if not _once(pod["id"], "pod_welcome"):
            continue
        season = _season_of_pod(pod)
        name = ((season or {}).get("season") or {}).get("display_name")
        text = (f"Welcome to {pod.get('name') or 'your pod'}" + (f", part of the {name} cohort" if name else "") +
                ". Say hello here before your first session.")
        try:
            _post(text, "cohort", pod["id"])
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("pod welcome for %s failed: %s", pod["id"], exc)
            _t("notice_log").delete().eq("user_id", pod["id"]).eq("kind", "pod_welcome").execute()
    return n


def season_posts() -> int:
    """One post to the season channel per formed intake."""
    n = 0
    for intake in (_t("intake").select("id, season_id, formation_date, status, mode, season:season_id(display_name)")
                   .eq("status", "formed").execute()).data or []:
        if intake.get("mode") == "rehearsal" or not intake.get("season_id"):
            continue
        if not _once(intake["id"], "season_formation"):
            continue
        pods = (_t("cohorts").select("id, status").eq("start_date", str(intake["formation_date"])[:10])
                .execute()).data or []
        live = sum(1 for p in pods if p.get("status") == "active")
        name = (intake.get("season") or {}).get("display_name") or "This season"
        try:
            _post(f"{name}: {live} new pod{'s' if live != 1 else ''} formed on {str(intake['formation_date'])[:10]}. "
                  "Welcome, everyone.", "season", intake["season_id"])
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("season post for %s failed: %s", intake["id"], exc)
            _t("notice_log").delete().eq("user_id", intake["id"]).eq("kind", "season_formation").execute()
    return n


def daily() -> dict:
    return {"pod_welcomes": welcome_pods(), "season_posts": season_posts()}
