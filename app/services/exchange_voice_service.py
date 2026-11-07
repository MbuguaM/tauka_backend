"""Async voice-note review (§159; exchange-extension-plan XE-19; exchange doc 01 §5 A1, §11).

A learner records up to 90 seconds on a prompt for their band (to their own
`voice-recordings/speech/{uid}/` folder, as the English sample already does). A partner with
the 'reviewer' or 'pap' role claims the oldest note in the queue for a band they can review
and replies within 48 hours with a voice reply (at least 30 seconds) and three written parts:
the corrected version, one or two key fixes, one natural alternative. The learner accepts or
rejects it with a reason; it is accepted by itself after 72 hours. Reviews are sampled for
quality (10% in a reviewer's first month, then 3%).

Pay for a review (paid partners only, after gate G1) is accrued on acceptance by
exchange_pay_service. Behind `exchange_voice_review`.
"""

import logging
import random
import re
from datetime import datetime, timedelta, timezone

from app.core.flags import is_ready
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

REVIEW_WITHIN = timedelta(hours=48)
AUTO_ACCEPT = timedelta(hours=72)
DAILY_REVIEWS = 40                         # doc 01 §7.4
ALLOWANCE = {"free_tier": 1, "learner_tier": 4, "tutor_tier": 8, "intensive_tier": 16}   # a month (doc 02 §7)
SAMPLE_NEW, SAMPLE = 0.10, 0.03


class VoiceRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _on() -> None:
    from app.services.exchange_service import enabled
    if not enabled() or not is_ready("exchange_voice_review"):
        raise VoiceRefused("not_enabled")


def _own_path(user_id: str, path: str) -> None:
    if not re.fullmatch(rf"speech/{re.escape(user_id)}/[A-Za-z0-9._-]{{1,80}}", path or ""):
        raise VoiceRefused("bad_path")


def _signed(path: str | None) -> str | None:
    if not path:
        return None
    try:
        res = db.storage.from_("voice-recordings").create_signed_url(path, 900)
        return (res or {}).get("signedURL") or (res or {}).get("signedUrl")
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice note link failed: %s", exc)
        return None


def _reviewer(user_id: str) -> dict:
    rows = (_t("exchange_partner").select("user_id, status, roles").eq("user_id", user_id).limit(1).execute()).data or []
    if not rows or rows[0]["status"] != "active" or not ({"reviewer", "pap"} & set(rows[0].get("roles") or [])):
        raise VoiceRefused("not_a_reviewer")
    return rows[0]


# ── learner ─────────────────────────────────────────────────────────────────

def prompts(user_id: str) -> dict:
    _on()
    from app.services.exchange_group_service import _band_am
    from app.services.exchange_service import _tier
    band = _band_am(user_id) or "foundation"
    rows = (_t("exchange_voice_prompt").select("id, band, prompt").eq("band", band).eq("active", True).execute()).data or []
    return {"band": band, "prompts": rows, "left": _left(user_id, _tier(user_id))}


def _left(user_id: str, tier: str) -> int:
    since = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    used = sum(1 for r in (_t("exchange_voice_note").select("id, created_at, status").eq("learner_id", user_id)
               .execute()).data or [] if str(r.get("created_at") or "9999") >= since)
    return max(0, ALLOWANCE.get(tier, 0) - used)


def submit(user_id: str, prompt_id: str, audio_path: str) -> dict:
    _on()
    from app.services.exchange_service import _tier, eligible
    if not eligible(user_id):
        raise VoiceRefused("not_eligible")
    _own_path(user_id, audio_path)
    if _left(user_id, _tier(user_id)) <= 0:
        raise VoiceRefused("allowance_used")
    p = (_t("exchange_voice_prompt").select("id, band, active").eq("id", prompt_id).limit(1).execute()).data or []
    if not p or not p[0]["active"]:
        raise VoiceRefused("bad_prompt")
    return (_t("exchange_voice_note").insert({"learner_id": user_id, "prompt_id": prompt_id, "band": p[0]["band"],
                                              "audio_path": audio_path, "status": "queued"}).execute()).data[0]


def mine(user_id: str) -> list[dict]:
    rows = (_t("exchange_voice_note").select("*").eq("learner_id", user_id).execute()).data or []
    prompts_ = {p["id"]: p["prompt"] for p in (_t("exchange_voice_prompt").select("id, prompt").execute()).data or []}
    return [{"id": r["id"], "prompt": prompts_.get(r["prompt_id"]), "status": r["status"], "created_at": r.get("created_at"),
             "review_text": r.get("review_text") if r["status"] in ("reviewed", "accepted", "rejected") else None,
             "review_audio": _signed(r.get("review_audio_path")) if r["status"] in ("reviewed", "accepted") else None,
             "audio": _signed(r["audio_path"])}
            for r in sorted(rows, key=lambda r: str(r.get("created_at") or "9999"), reverse=True)[:30]]


def verdict(user_id: str, note_id: str, accept: bool, reason: str | None, now: datetime | None = None) -> dict:
    now = now or _now()
    rows = (_t("exchange_voice_note").select("*").eq("id", note_id).limit(1).execute()).data or []
    if not rows or rows[0]["learner_id"] != user_id or rows[0]["status"] != "reviewed":
        raise VoiceRefused("not_open")
    if not accept and not (reason or "").strip():
        raise VoiceRefused("reason_needed")
    return _decide(rows[0], accept, reason, now)


def _decide(note: dict, accept: bool, reason: str | None, now: datetime) -> dict:
    patch = {"status": "accepted" if accept else "rejected", "decided_at": now.isoformat(),
             "rejection_reason": None if accept else (reason or "").strip()[:300]}
    if not accept:
        patch["sampled"] = True                 # every rejection goes to sampling (doc 02 §4.7)
    row = (_t("exchange_voice_note").update(patch).eq("id", note["id"]).execute()).data[0]
    if accept:
        try:
            from app.services import exchange_pay_service as pay
            pay.accrue_review(row, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("review pay for %s failed: %s", note["id"], exc)
    return row


# ── reviewer ────────────────────────────────────────────────────────────────

def claim(user_id: str, now: datetime | None = None) -> dict | None:
    """The oldest queued note. One open claim at a time; 40 reviews a day at most."""
    _on()
    _reviewer(user_id)
    now = now or _now()
    mine_ = (_t("exchange_voice_note").select("*").eq("reviewer_id", user_id).eq("status", "claimed").execute()).data or []
    if mine_:
        return _for_reviewer(mine_[0])
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    done = sum(1 for r in (_t("exchange_voice_note").select("reviewed_at").eq("reviewer_id", user_id).execute()).data or []
               if r.get("reviewed_at") and str(r["reviewed_at"]) >= day)
    if done >= DAILY_REVIEWS:
        raise VoiceRefused("daily_cap")
    queue = sorted((r for r in (_t("exchange_voice_note").select("*").eq("status", "queued").execute()).data or []
                    if r["learner_id"] != user_id), key=lambda r: str(r.get("created_at") or "9999"))
    for r in queue:
        got = (_t("exchange_voice_note").update({"status": "claimed", "reviewer_id": user_id, "claimed_at": now.isoformat(),
                                                 "due_at": (now + REVIEW_WITHIN).isoformat()})
               .eq("id", r["id"]).eq("status", "queued").execute()).data or []
        if got:                                # someone else may have claimed it first
            return _for_reviewer(got[0])
    return None


def _for_reviewer(r: dict) -> dict:
    p = (_t("exchange_voice_prompt").select("prompt").eq("id", r["prompt_id"]).limit(1).execute()).data or []
    return {"id": r["id"], "band": r["band"], "prompt": p[0]["prompt"] if p else None, "audio": _signed(r["audio_path"]),
            "due_at": r.get("due_at")}


def review(user_id: str, note_id: str, audio_path: str, corrected: str, fixes: list[str], alternative: str,
           now: datetime | None = None) -> dict:
    _on()
    _reviewer(user_id)
    now = now or _now()
    _own_path(user_id, audio_path)
    fixes = [f.strip()[:300] for f in (fixes or []) if f and f.strip()][:2]
    if not corrected.strip() or not fixes or not alternative.strip():
        raise VoiceRefused("incomplete")
    rows = (_t("exchange_voice_note").select("*").eq("id", note_id).limit(1).execute()).data or []
    if not rows or rows[0]["reviewer_id"] != user_id or rows[0]["status"] != "claimed":
        raise VoiceRefused("not_yours")
    first_month = not (_t("exchange_voice_note").select("id, reviewed_at").eq("reviewer_id", user_id)
                       .lte("reviewed_at", (now - timedelta(days=30)).isoformat()).limit(1).execute()).data
    return (_t("exchange_voice_note").update({
        "status": "reviewed", "review_audio_path": audio_path, "reviewed_at": now.isoformat(),
        "review_text": {"corrected": corrected.strip()[:1000], "fixes": fixes, "alternative": alternative.strip()[:500]},
        "sampled": random.random() < (SAMPLE_NEW if first_month else SAMPLE)}).eq("id", note_id).execute()).data[0]


def reviewer_state(user_id: str) -> dict:
    try:
        _reviewer(user_id)
    except VoiceRefused:
        return {"reviewer": False}
    open_ = (_t("exchange_voice_note").select("*").eq("reviewer_id", user_id).eq("status", "claimed").execute()).data or []
    queued = len((_t("exchange_voice_note").select("id").eq("status", "queued").execute()).data or [])
    return {"reviewer": True, "enabled": is_ready("exchange_voice_review"), "queued": queued,
            "claimed": _for_reviewer(open_[0]) if open_ else None}


# ── ops / tutors: the sample ───────────────────────────────────────────────

def sample(actor: str) -> list[dict]:
    from app.services import hierarchy_service as hs
    if not hs.admin_can(actor, "ops"):
        raise VoiceRefused("not_allowed")
    rows = (_t("exchange_voice_note").select("*").eq("sampled", True).in_("status", ["reviewed", "accepted", "rejected"])
            .execute()).data or []
    return [{**_for_reviewer(r), "status": r["status"], "review_text": r.get("review_text"),
             "review_audio": _signed(r.get("review_audio_path")), "rejection_reason": r.get("rejection_reason"),
             "reviewer_id": r.get("reviewer_id")} for r in rows[:50]]


# ── hourly ──────────────────────────────────────────────────────────────────

def hourly(now: datetime | None = None) -> dict:
    """Claims past 48 h go back to the queue; reviews unanswered for 72 h are accepted."""
    now = now or _now()
    out = {"requeued": 0, "accepted": 0}
    if not is_ready("exchange_voice_review"):
        return out
    for r in (_t("exchange_voice_note").select("*").eq("status", "claimed").execute()).data or []:
        if r.get("due_at") and _ts(r["due_at"]) < now:
            _t("exchange_voice_note").update({"status": "queued", "reviewer_id": None, "claimed_at": None, "due_at": None}) \
                .eq("id", r["id"]).execute()
            out["requeued"] += 1
    for r in (_t("exchange_voice_note").select("*").eq("status", "reviewed").execute()).data or []:
        if r.get("reviewed_at") and _ts(r["reviewed_at"]) + AUTO_ACCEPT < now:
            _decide(r, True, None, now)
            out["accepted"] += 1
    return out
