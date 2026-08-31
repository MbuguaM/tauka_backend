"""Tutor feedback triage — plan Phase 6, spec §1.6 third bullet.

*"A structured endpoint (not just support email) for tutors to submit
curriculum/product feedback, routed to an internal review queue — worth scoping
as simple structured input first (category + text) rather than a full
feature-request system."*

**Submission is not in this module.** A tutor inserts their own row directly
under `tutor_feedback_insert_own`, exactly as `app.course_correction` and
`app.content_approval_requests` work — the two closest precedents, both "a tutor
submits something for admin review", both written from Flutter. Adding a
service-role write path would give one table two writers for no gain, because
the RLS policy already expresses the rule (`tutor_id = auth.uid() AND
app.is_tutor() AND status = 'new'`) more precisely than a Python check could.

What is here is the **admin half**: the queue the feedback is routed *to*, and
the verdict that closes an item. Both are admin-only and the §3.4 dashboard is
the consumer.

WHY THE TUTOR CAN READ THE VERDICT.

`tutor_feedback_select_own` returns the whole row, `admin_note` included. That is
deliberate and it is the entire difference between this and a support email:
§1.6's objection to email is not that it is unstructured, it is that it goes
somewhere the sender cannot see.
"""

import logging
from datetime import datetime, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

STATUSES = ("new", "triaged", "actioned", "declined")
CATEGORIES = ("curriculum", "product", "bug", "student_wellbeing", "other")


def queue(status: str = "new", limit: int = 50) -> list[dict]:
    """The review queue, oldest first within a status.

    **Oldest first, not newest.** A queue sorted newest-first is a stack, and a
    stack starves its bottom: the piece of feedback nobody got to in March stays
    unread for ever while March's is read twice.
    """
    try:
        query = db.schema("app").table("tutor_feedback").select("*")
        if status != "all":
            query = query.eq("status", status)
        result = query.order("created_at").limit(limit).execute()
        return result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback queue read failed: %s", exc)
        return []


def triage(feedback_id: str, admin_id: str, status: str,
           admin_note: str | None = None) -> dict:
    """Record a verdict. Raises `ValueError` for anything a caller got wrong.

    `new` is not an accepted verdict: the CHECK constraint
    `tutor_feedback_triaged_state` ties `status = 'new'` to a NULL `triaged_at`,
    so moving a row *back* to new would need the timestamp cleared, and "this
    was un-triaged on the 14th" is not a fact worth being able to record.
    """
    if status not in STATUSES or status == "new":
        raise ValueError(f"status must be one of {STATUSES[1:]}")

    now = datetime.now(timezone.utc).isoformat()
    try:
        result = db.schema("app").table("tutor_feedback").update({
            "status": status,
            "admin_note": admin_note,
            "triaged_at": now,
            "triaged_by": admin_id,
        }).eq("id", feedback_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("feedback triage failed for %s: %s", feedback_id, exc)
        raise ValueError(f"could not triage: {exc}") from exc

    row = (result.data or [None])[0]
    if not row:
        raise ValueError("no such feedback")
    logger.info("Feedback %s triaged as %s", feedback_id, status)
    return row


def summary() -> dict:
    """Counts per status and per category — the dashboard's header row.

    Counted rather than fetched: the queue can be long and the header only needs
    numbers. Falls back to zeroes rather than raising, because a header that
    cannot render must not take the queue down with it.
    """
    out = {"by_status": {s: 0 for s in STATUSES},
           "by_category": {c: 0 for c in CATEGORIES}}
    try:
        result = db.schema("app").table("tutor_feedback").select(
            "status, category").execute()
        for row in result.data or []:
            if row.get("status") in out["by_status"]:
                out["by_status"][row["status"]] += 1
            if row.get("category") in out["by_category"]:
                out["by_category"][row["category"]] += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback summary failed: %s", exc)
    return out
