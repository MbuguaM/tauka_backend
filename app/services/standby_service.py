"""Standby roster and reliability — enrolment plan 8.6 (migration §125).

A tutor commits weekly STANDBY windows (in their own zone). When a learner
reports that their tutor hasn't come (`cover_service.report_no_show`), tutors
on standby at that moment are offered the session first, then the pod's
guests, then any free tutor in every learner's pool.

The reliability score is for ops to read, never a gate: nothing decides pay
or matching from it.

    score = 100 - 10 per late_drop - 25 per no_show + 5 per cover_taken
            (last 90 days, clamped 0..100)
"""

import logging
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MAX_WINDOWS = 14
SCORE_DAYS = 90
WEIGHTS = {"late_drop": -10, "no_show": -25, "cover_taken": 5, "cover_given": 0}
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class StandbyRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hhmm(v) -> time:
    s = str(v)[:5]
    return time(int(s[:2]), int(s[3:5]))


def _zone(tutor_id: str) -> str:
    rows = (_t("user_profiles").select("timezone").eq("id", tutor_id).limit(1).execute()).data or []
    tz = (rows[0].get("timezone") if rows else None) or "UTC"
    try:
        ZoneInfo(tz)
        return tz
    except Exception:  # noqa: BLE001
        return "UTC"


def _require_tutor(tutor_id: str) -> None:
    rows = (_t("tutor").select("status, deleted_at").eq("id", tutor_id).limit(1).execute()).data or []
    if not rows or rows[0].get("deleted_at") or rows[0].get("status") == "offboarded":
        raise StandbyRefused("not_a_tutor")


# ── the tutor's windows ─────────────────────────────────────────────────────

def windows(tutor_id: str) -> list[dict]:
    rows = (_t("tutor_standby_window").select("id, day_of_week, start_time, end_time, timezone")
            .eq("tutor_id", tutor_id).eq("active", True).execute()).data or []
    return sorted(({**r, "start_time": str(r["start_time"])[:5], "end_time": str(r["end_time"])[:5]} for r in rows),
                  key=lambda r: (r["day_of_week"], r["start_time"]))


def add_window(tutor_id: str, day: int, start: str, end: str) -> dict:
    _require_tutor(tutor_id)
    if not 0 <= int(day) <= 6 or not _HHMM.match(start or "") or not _HHMM.match(end or ""):
        raise StandbyRefused("bad_window")
    if not (end > start or end == "00:00"):
        raise StandbyRefused("bad_window")
    if len(windows(tutor_id)) >= MAX_WINDOWS:
        raise StandbyRefused("too_many")
    row = (_t("tutor_standby_window").insert({"tutor_id": tutor_id, "day_of_week": int(day), "start_time": start,
                                              "end_time": end, "timezone": _zone(tutor_id),
                                              "active": True}).execute()).data[0]
    return row


def remove_window(tutor_id: str, window_id: str) -> None:
    got = (_t("tutor_standby_window").update({"active": False}).eq("id", window_id).eq("tutor_id", tutor_id)
           .eq("active", True).execute()).data or []
    if not got:
        raise StandbyRefused("no_such_window")


def on_standby(tutor_ids: list[str], start: datetime) -> set[str]:
    """Who has a standby window holding this instant."""
    if not tutor_ids:
        return set()
    out = set()
    for w in (_t("tutor_standby_window").select("tutor_id, day_of_week, start_time, end_time, timezone")
              .in_("tutor_id", list(tutor_ids)).eq("active", True).execute()).data or []:
        try:
            local = start.astimezone(ZoneInfo(w.get("timezone") or "UTC"))
        except Exception:  # noqa: BLE001
            continue
        if int(w["day_of_week"]) != local.weekday():
            continue
        lo, hi = _hhmm(w["start_time"]), _hhmm(w["end_time"])
        lo_m, hi_m = lo.hour * 60 + lo.minute, (hi.hour * 60 + hi.minute) or 1440
        if lo_m <= local.hour * 60 + local.minute < hi_m:
            out.add(w["tutor_id"])
    return out


# ── reliability ─────────────────────────────────────────────────────────────

def reliability(tutor_ids: list[str], now: datetime | None = None) -> dict[str, dict]:
    """{tutor_id: {score, late_drop, no_show, cover_taken, cover_given}} over 90 days."""
    if not tutor_ids:
        return {}
    since = ((now or _now()) - timedelta(days=SCORE_DAYS)).isoformat()
    out = {t: {"score": 100, **{k: 0 for k in WEIGHTS}} for t in tutor_ids}
    for e in (_t("tutor_reliability_event").select("tutor_id, kind, created_at").in_("tutor_id", list(tutor_ids))
              .gte("created_at", since).execute()).data or []:
        r = out.get(e["tutor_id"])
        if r is not None and e.get("kind") in WEIGHTS:
            r[e["kind"]] += 1
    for r in out.values():
        r["score"] = max(0, min(100, 100 + sum(WEIGHTS[k] * r[k] for k in WEIGHTS)))
    return out
