"""Recording each person's own side of exchange calls (§159; exchange-extension-plan XE-21).

Each device records ITS OWN microphone and uploads it after the call to the private
`exchange-recordings` bucket. Nobody's device ever records anyone else, so one person
switching recording off removes only their own voice.

Defaults (developer decision D-1, 2026-11-06): ON for partners, OFF for learners and guests,
until counsel answers L-004 / gate G3. Anyone can change it in settings, and switch it off for
one call in the lobby or mid-call; switching off mid-call deletes that call's partial upload.

Kept 30 days, then deleted, unless a safety case holds it (legal_hold). Only safeguarding
opens a recording, only for a case that names the session, and every opening is logged
(`exchange_recording_access`). Recaps and feedback use a recording only with a separate
opt-in (`recap_opt_in`), which nothing reads yet.

Behind `exchange_recording`; with it off nothing is recorded and every call says so.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

BUCKET = "exchange-recordings"
KEEP = timedelta(days=30)
POLICY = "exchange-recording-1"


class RecordingRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _is_partner(user_id: str) -> bool:
    return bool((_t("exchange_partner").select("user_id").eq("user_id", user_id).limit(1).execute()).data)


def prefs(user_id: str) -> dict:
    rows = (_t("exchange_match_pref").select("*").eq("user_id", user_id).limit(1).execute()).data or []
    p = rows[0] if rows else {}
    own = p.get("record_own_side")
    default = _is_partner(user_id)
    return {"enabled": is_ready("exchange_recording"), "record_own_side": default if own is None else bool(own),
            "default": default, "recap_opt_in": bool(p.get("recap_opt_in")),
            "wifi_only_upload": p.get("wifi_only_upload") is not False}


def set_prefs(user_id: str, record_own_side: bool | None, recap_opt_in: bool | None, wifi_only_upload: bool | None,
              now: datetime | None = None) -> dict:
    now = now or _now()
    before = prefs(user_id)
    row = {"user_id": user_id, "updated_at": now.isoformat()}
    if record_own_side is not None:
        row["record_own_side"] = bool(record_own_side)
    if recap_opt_in is not None:
        row["recap_opt_in"] = bool(recap_opt_in)
    if wifi_only_upload is not None:
        row["wifi_only_upload"] = bool(wifi_only_upload)
    _t("exchange_match_pref").upsert(row).execute()
    after = prefs(user_id)
    for purpose, key in (("exchange_recording", "record_own_side"), ("exchange_recap", "recap_opt_in")):
        if after[key] != before[key]:
            try:
                _t("consent_record").insert({"subject_id": user_id, "purpose": purpose, "granted": after[key],
                                             "policy_version": POLICY, "source": "settings"}).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning("recording consent record failed: %s", exc)
    return after


def _participant(user_id: str, kind: str, session_id: str) -> None:
    if kind == "pair":
        rows = (_t("exchange_session").select("learner_id, partner_id").eq("id", session_id).limit(1).execute()).data or []
        if rows and user_id in (rows[0]["learner_id"], rows[0]["partner_id"]):
            return
    elif kind == "group":
        if (_t("exchange_group_member").select("id").eq("group_id", session_id).eq("user_id", user_id).limit(1).execute()).data:
            return
    raise RecordingRefused("not_your_session")


def start(user_id: str, kind: str, session_id: str, now: datetime | None = None) -> dict:
    """At the start of a call: whether to record this person's side, and where to upload it."""
    now = now or _now()
    if kind not in ("pair", "group"):
        raise RecordingRefused("bad_value")
    _participant(user_id, kind, session_id)
    p = prefs(user_id)
    if not p["enabled"] or not p["record_own_side"]:
        return {"record": False, "enabled": p["enabled"]}
    path = f"{kind}/{session_id}/{user_id}.webm"
    try:
        _t("exchange_recording").upsert({"session_kind": kind, "session_id": session_id, "user_id": user_id,
                                         "storage_path": path, "status": "pending",
                                         "expires_at": (now + KEEP).isoformat()},
                                        on_conflict="session_kind,session_id,user_id").execute()
        up = db.storage.from_(BUCKET).create_signed_upload_url(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("recording upload link failed: %s", exc)
        return {"record": False, "enabled": True}
    return {"record": True, "enabled": True, "path": path, "upload_url": up.get("signed_url") or up.get("signedUrl"),
            "token": up.get("token"), "wifi_only": p["wifi_only_upload"]}


def complete(user_id: str, kind: str, session_id: str, duration_s: int | None) -> dict:
    _participant(user_id, kind, session_id)
    rows = (_t("exchange_recording").update({"status": "uploaded", "duration_s": duration_s})
            .eq("session_kind", kind).eq("session_id", session_id).eq("user_id", user_id).execute()).data or []
    return {"ok": bool(rows)}


def discard(user_id: str, kind: str, session_id: str) -> dict:
    """Switched off mid-call: delete this call's recording of their side."""
    _participant(user_id, kind, session_id)
    rows = (_t("exchange_recording").select("*").eq("session_kind", kind).eq("session_id", session_id)
            .eq("user_id", user_id).execute()).data or []
    for r in rows:
        if r.get("legal_hold"):
            continue
        try:
            db.storage.from_(BUCKET).remove([r["storage_path"]])
        except Exception as exc:  # noqa: BLE001
            logger.warning("recording delete failed: %s", exc)
        _t("exchange_recording").update({"status": "deleted"}).eq("id", r["id"]).execute()
    return {"ok": True}


def hold_for_case(session_id: str) -> int:
    """A case about a session keeps its recordings past 30 days."""
    rows = (_t("exchange_recording").update({"legal_hold": True}).eq("session_id", session_id).execute()).data or []
    return len(rows)


def open_for_case(actor: str, recording_id: str, concern_id: str) -> str | None:
    """Safeguarding only, for a case that names the recording's session. Logged."""
    if not hs.admin_can(actor, "safeguarding"):
        raise RecordingRefused("not_allowed")
    rec = (_t("exchange_recording").select("*").eq("id", recording_id).limit(1).execute()).data or []
    case = (_t("concern_report").select("id, session_id").eq("id", concern_id).limit(1).execute()).data or []
    if not rec or not case or case[0].get("session_id") != rec[0]["session_id"]:
        raise RecordingRefused("not_linked")
    _t("exchange_recording_access").insert({"recording_id": recording_id, "actor": actor, "concern_id": concern_id}).execute()
    try:
        res = db.storage.from_(BUCKET).create_signed_url(rec[0]["storage_path"], 600)
        return (res or {}).get("signedURL") or (res or {}).get("signedUrl")
    except Exception as exc:  # noqa: BLE001
        logger.warning("recording link failed: %s", exc)
        return None


def for_case(actor: str, concern_id: str) -> list[dict]:
    if not hs.admin_can(actor, "safeguarding"):
        raise RecordingRefused("not_allowed")
    case = (_t("concern_report").select("session_id").eq("id", concern_id).limit(1).execute()).data or []
    if not case or not case[0].get("session_id"):
        return []
    return (_t("exchange_recording").select("id, user_id, duration_s, status, expires_at, legal_hold")
            .eq("session_id", case[0]["session_id"]).neq("status", "deleted").execute()).data or []


def purge(now: datetime | None = None) -> int:
    """Daily: recordings past 30 days and not held are deleted."""
    now = now or _now()
    n = 0
    for r in (_t("exchange_recording").select("*").neq("status", "deleted").execute()).data or []:
        if r.get("legal_hold") or _ts(r["expires_at"]) > now:
            continue
        try:
            db.storage.from_(BUCKET).remove([r["storage_path"]])
        except Exception as exc:  # noqa: BLE001
            logger.warning("recording purge failed for %s: %s", r["id"], exc)
            continue
        _t("exchange_recording").update({"status": "deleted"}).eq("id", r["id"]).execute()
        n += 1
    return n
