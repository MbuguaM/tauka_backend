"""Session reminders, 24 hours and 1 hour before — enrolment plan 6.1 / 6.2.

Run by `GET /tasks/hourly` (an external hourly cron; Vercel's own cron here is
daily). For every pod session, make-up, clinic and booked exchange starting

    in [now + 23.5 h, now + 24.5 h)  -> session_24h
    in [now + 30 min, now + 90 min)  -> session_1h

each learner in it and its tutor get one push / WhatsApp reminder in their own
time zone (`notify_service`, once per person per session per kind). Nothing goes
out until a channel is switched on (PUSH_ENABLED, or the WhatsApp flag plus an
opt-in), so this is safe to schedule today.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

WINDOWS = {"session_24h": (timedelta(hours=23, minutes=30), timedelta(hours=24, minutes=30)),
           "session_1h": (timedelta(minutes=30), timedelta(minutes=90))}


def _t(name: str):
    return db.schema("app").table(name)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def upcoming(lo: datetime, hi: datetime) -> list[dict]:
    """[{kind, id, starts_at, name, people:[ids], link}] starting in [lo, hi)."""
    out = []
    a, b = lo.isoformat(), hi.isoformat()
    for s in (_t("cohort_session").select("id, cohort_id, scheduled_at, status, lead_tutor_id")
              .eq("status", "scheduled").gte("scheduled_at", a).lt("scheduled_at", b).execute()).data or []:
        people = [m["user_id"] for m in (_t("cohort_memberships").select("user_id").eq("cohort_id", s["cohort_id"])
                  .eq("status", "active").execute()).data or []]
        out.append({"kind": "pod", "id": s["id"], "starts_at": _ts(s["scheduled_at"]), "name": "your pod session",
                    "people": people + ([s["lead_tutor_id"]] if s.get("lead_tutor_id") else []), "link": "/learn"})
    for s in (_t("makeup_session").select("id, scheduled_at, status, tutor_id").eq("status", "scheduled")
              .gte("scheduled_at", a).lt("scheduled_at", b).execute()).data or []:
        people = [p["student_id"] for p in (_t("makeup_session_participant").select("student_id, status")
                  .eq("makeup_session_id", s["id"]).execute()).data or [] if p.get("status") != "withdrawn"]
        out.append({"kind": "makeup", "id": s["id"], "starts_at": _ts(s["scheduled_at"]), "name": "your make-up session",
                    "people": people + ([s["tutor_id"]] if s.get("tutor_id") else []), "link": "/learn"})
    for c in (_t("clinic").select("id, starts_at, status, tutor_id").eq("status", "scheduled")
              .gte("starts_at", a).lt("starts_at", b).execute()).data or []:
        people = [k["student_id"] for k in (_t("clinic_booking").select("student_id").eq("clinic_id", c["id"])
                  .eq("status", "booked").execute()).data or []]
        out.append({"kind": "clinic", "id": c["id"], "starts_at": _ts(c["starts_at"]), "name": "your clinic",
                    "people": people + ([c["tutor_id"]] if c.get("tutor_id") else []), "link": "/phase0"})
    # Exchanges (features addendum A9): booked ones only; both people.
    try:
        for x in (_t("exchange_session").select("id, scheduled_at, status, learner_id, partner_id").eq("status", "booked")
                  .gte("scheduled_at", a).lt("scheduled_at", b).execute()).data or []:
            out.append({"kind": "exchange", "id": x["id"], "starts_at": _ts(x["scheduled_at"]),
                        "name": "your exchange session", "people": [x["learner_id"], x["partner_id"]],
                        "link": "/exchange"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange reminders skipped: %s", exc)
    return out


async def seat_alerts() -> int:
    """An open pod offer (formation, refill or private pod): tell the learner
    at once on push / WhatsApp too — offers last only 24 hours (E-13)."""
    from app.services.notify_service import notify
    n = 0
    for o in (_t("match_offer").select("id, student_id, cohort_id, status").eq("status", "offered").execute()).data or []:
        pod = (_t("cohorts").select("name").eq("id", o["cohort_id"]).limit(1).execute()).data or [{}]
        name = pod[0].get("name") or "a pod"
        if await notify(o["student_id"], "seat_offer", o["id"], "You've been offered a pod",
                        f"{name}. Answer within 24 hours in the app.", "/matching/offers",
                        whatsapp=("seat_offer", [name])):
            n += 1
    return n


async def hourly(now: datetime | None = None) -> dict:
    from app.services.makeup_delivery import _zones, local_time
    from app.services.notify_service import notify
    now = now or datetime.now(timezone.utc)
    out = {"session_24h": 0, "session_1h": 0}
    try:
        out["seat_offers"] = await seat_alerts()
    except Exception as exc:  # noqa: BLE001
        logger.error("seat alerts failed: %s", exc)
    for kind, (lo, hi) in WINDOWS.items():
        for s in upcoming(now + lo, now + hi):
            zones = _zones(s["people"]) if s["people"] else {}
            for uid in s["people"]:
                when = local_time(s["starts_at"], zones.get(uid, "UTC"))
                title = "Tomorrow: " + s["name"] if kind == "session_24h" else "In an hour: " + s["name"]
                template = "session_reminder_24h" if kind == "session_24h" else "session_reminder_1h"
                try:
                    sent = await notify(uid, kind, s["id"], title, when, s["link"],
                                        whatsapp=(template, [s["name"], when]))
                    out[kind] += 1 if sent else 0
                except Exception as exc:  # noqa: BLE001
                    logger.error("reminder %s for %s failed: %s", kind, uid, exc)
    return out
