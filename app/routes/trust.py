"""Trust — plan Phase 5a, spec §1.5, decision D-12.

**One route, and it records no personal data at all.**

§1.5 asks for silent chat scanning with flags in an admin dashboard. D-12 chose
plan §12.4's alternative: warn the sender at compose time and store nothing about
them. The reasoning is legal (L-008) — enrolment-implies-consent was argued for
behavioural telemetry and does not stretch to reading message *content* for a
purpose *adverse* to both people in the conversation, one of whom is a
contractor.

So there is no `trust_flag` here and no `/admin/trust` table. There is a counter
that says "a warning of this class was shown N times today and ignored M times",
which is exactly enough to answer whether the warning works — and not enough to
identify anybody.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user, require_admin
from app.models.schemas import TrustWarningRequest
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/warning")
async def record_warning(
    req: TrustWarningRequest,
    user_id: str = Depends(get_current_user),
):
    """Record that a contact-pattern warning was shown, and whether it was ignored.

    **`user_id` is required and deliberately never stored.** The caller is
    authenticated so the counter cannot be spammed by anyone off the street;
    that identity then goes no further than this function signature. §17.3:
    *"Anonymous counters only — warnings shown / proceeded-anyway, with no user
    id."*

    Best-effort: a counter that fails to increment must never be visible to the
    person typing a message. The client fires it unawaited for the same reason.
    """
    try:
        db.schema("app").rpc("record_trust_warning", {
            "p_class": req.pattern_class,
            "p_proceeded": req.proceeded,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust warning counter failed: %s", exc)
        # 204 either way. The client has nothing useful to do with a failure
        # here, and surfacing one would put an error in front of someone who was
        # only trying to send a message.
    return {"recorded": True}


@router.get("/warnings")
async def warning_stats(admin_id: str = Depends(require_admin)):
    """ADMIN — the anonymous counters.

    The one question these exist to answer: **is warn-at-send working, or is the
    silent flagging §12.2 says we cannot currently justify actually needed?**

    A high `proceeded` share for `payment_handle` would be the signal that people
    are being warned and going ahead anyway. A low one means the guardrail is
    doing its job and nothing further is warranted.
    """
    try:
        result = db.schema("app").table("trust_warning_stat").select("*").order(
            "day", desc=True).limit(180).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust warning stats read failed: %s", exc)
        raise HTTPException(status_code=503, detail="Counters unavailable")

    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(row["pattern_class"], {"shown": 0, "proceeded": 0})
        entry["shown"] += int(row.get("shown") or 0)
        entry["proceeded"] += int(row.get("proceeded") or 0)
    for cls, entry in totals.items():
        entry["proceeded_share"] = (
            round(entry["proceeded"] / entry["shown"], 2) if entry["shown"] else 0.0)

    return {"by_class": totals, "days": rows}
