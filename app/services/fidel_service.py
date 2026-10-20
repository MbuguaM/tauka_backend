"""Fidel approval (enrolment plan 1.4, migration §107).

After clearing an alphabet exercise group, a learner reads that group's
characters aloud and submits the take; a reviewer approves it or asks for a redo
with what to practise. Playbook: "first Fidel orders with strict async approval,
especially ejectives (ጠ, ጨ, ቀ, ጰ, ፀ) and gemination." §2d: it is **feedback,
not a gate** — finishing Phase 0 is what makes a learner ready.

THE AUDIO travels the existing speech pipeline: the app uploads a zip to the
`voice-recordings` bucket and inserts an `app.speech_submission` row
(`section_kind = 'fidelReading'`), which the 90-day sweep removes. This module
records the verdict in `app.fidel_approval`, which outlives the audio.

CONSENT FIRST (L-006; developer 2026-10-18). Before the first submission the
learner agrees, in the app, to their recording going to a Tauka reviewer —
`consent_record` purpose `tutor_voice_review`, source `in_app`. No submission is
accepted without a current grant.

REVIEWERS are active tutors at rung >= 1 (§2k: rung 1 is the Phase 0 reviewer)
or ops admins. A reviewer need not be the learner's own tutor — that is the
widening the consent sentence names — so they hear the take through a
short-lived signed URL issued here, not through bucket access.
"""

import logging
from datetime import datetime, timezone

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CONSENT_PURPOSE = "tutor_voice_review"
# Bump when the consent sentence in the app changes (fidel_models.dart).
CONSENT_VERSION = "2026-10-18"
GAP_CODES = ("fidel_recognition", "vowel_orders", "ejectives", "gemination", "reading_fluency")
REVIEWER_MIN_RUNG = 1
SIGNED_URL_SECONDS = 600
BUCKET = "voice-recordings"


class FidelRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── consent ──────────────────────────────────────────────────────────────────

def has_consent(student_id: str) -> bool:
    """The latest answer for the purpose; no row = no."""
    rows = (_t("consent_record").select("granted, at").eq("subject_id", student_id)
            .eq("purpose", CONSENT_PURPOSE).order("at", desc=True).limit(1).execute()).data or []
    return bool(rows) and bool(rows[0].get("granted"))


def set_consent(student_id: str, granted: bool) -> bool:
    """Append an answer (the ledger is append-only; withdrawing is a new row)."""
    _t("consent_record").insert({
        "subject_id": student_id, "purpose": CONSENT_PURPOSE, "granted": granted,
        "policy_version": CONSENT_VERSION, "source": "in_app",
    }).execute()
    return granted


# ── learner ──────────────────────────────────────────────────────────────────

def submit(student_id: str, unit_id: str, group_index: int, group_title: str | None,
           storage_path: str) -> dict:
    if not has_consent(student_id):
        raise FidelRefused("consent_required")
    # app.speech_submission (tauka-flutter): the row the app inserted after uploading.
    rows = (_t("speech_submission").select("id, user_id, section_kind")
            .eq("storage_path", storage_path).eq("user_id", student_id).limit(1).execute()).data or []
    if not rows or rows[0]["section_kind"] != "fidelReading":
        raise FidelRefused("no_such_submission")
    # A new reading replaces one still waiting for this group.
    _t("fidel_approval").update({"status": "superseded"}).eq("student_id", student_id) \
        .eq("unit_id", unit_id).eq("group_index", group_index).eq("status", "pending").execute()
    made = _t("fidel_approval").insert({
        "student_id": student_id, "unit_id": unit_id, "group_index": group_index,
        "group_title": (group_title or "")[:120] or None, "submission_id": rows[0]["id"],
    }).execute()
    return (made.data or [{}])[0]


def mine(student_id: str, unit_id: str) -> dict:
    """The latest verdict per group (superseded ones are history)."""
    rows = (_t("fidel_approval").select("id, group_index, status, gap_codes, created_at, reviewed_at")
            .eq("student_id", student_id).eq("unit_id", unit_id).order("created_at", desc=True)
            .execute()).data or []
    latest: dict[int, dict] = {}
    for r in rows:
        if r["status"] != "superseded" and r["group_index"] not in latest:
            latest[r["group_index"]] = r
    return {"consented": has_consent(student_id),
            "groups": [latest[k] for k in sorted(latest)]}


# ── reviewer ─────────────────────────────────────────────────────────────────

def is_reviewer(actor: str) -> bool:
    if hs.admin_can(actor, "ops"):
        return True
    if (hs.rung_of(actor) or 0) < REVIEWER_MIN_RUNG:
        return False
    rows = (_t("tutor").select("status, deleted_at").eq("id", actor).limit(1).execute()).data or []
    return bool(rows) and rows[0].get("status") == "active" and not rows[0].get("deleted_at")


def _signed_url(path: str) -> str | None:
    try:
        res = db.storage.from_(BUCKET).create_signed_url(path, SIGNED_URL_SECONDS)
        return (res or {}).get("signedURL") or (res or {}).get("signedUrl")
    except Exception as exc:  # noqa: BLE001
        logger.warning("signed url for %s failed: %s", path, exc)
        return None


def queue(actor: str) -> list[dict]:
    """Readings waiting for review, oldest first, each with a short-lived link
    to its audio. A pending reading whose audio was swept is closed `expired`."""
    if not is_reviewer(actor):
        raise FidelRefused("not_allowed")
    rows = (_t("fidel_approval").select("id, student_id, unit_id, group_index, group_title, submission_id, created_at")
            .eq("status", "pending").order("created_at").execute()).data or []
    gone = [r["id"] for r in rows if not r.get("submission_id")]
    for rid in gone:
        _t("fidel_approval").update({"status": "expired"}).eq("id", rid).eq("status", "pending").execute()
    rows = [r for r in rows if r.get("submission_id")]
    if not rows:
        return []
    paths = {s["id"]: s["storage_path"] for s in (_t("speech_submission").select("id, storage_path")
             .in_("id", [r["submission_id"] for r in rows]).execute()).data or []}
    from app.services.makeup_service import _names
    names = _names(r["student_id"] for r in rows)
    return [{**r, "student_name": names.get(r["student_id"]),
             "audio_url": _signed_url(paths[r["submission_id"]]) if r["submission_id"] in paths else None}
            for r in rows]


def review(actor: str, approval_id: str, approved: bool, gap_codes: list[str]) -> dict:
    if not is_reviewer(actor):
        raise FidelRefused("not_allowed")
    codes = sorted(set(gap_codes or []))
    if any(c not in GAP_CODES for c in codes):
        raise FidelRefused("invalid_gap_code")
    if approved and codes:
        raise FidelRefused("codes_only_on_redo")
    rows = (_t("fidel_approval").select("id, student_id, status").eq("id", approval_id).limit(1).execute()).data or []
    if not rows:
        raise FidelRefused("no_such_reading")
    if rows[0]["status"] != "pending":
        raise FidelRefused("decided")
    if rows[0]["student_id"] == actor:
        raise FidelRefused("not_allowed")
    done = (_t("fidel_approval").update({
        "status": "approved" if approved else "redo", "gap_codes": [] if approved else codes,
        "reviewed_by": actor, "reviewed_at": _now(),
    }).eq("id", approval_id).eq("status", "pending").execute()).data
    if not done:
        raise FidelRefused("decided")
    # Plan 1.4 (§140): a 20% sample of rung-1 reviews gets a second look (calibration).
    try:
        from app.services.pilots_service import mark_for_second_review
        mark_for_second_review(approval_id, actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("second-review sample failed for %s: %s", approval_id, exc)
    return done[0]
