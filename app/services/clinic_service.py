"""Phase 0 clinics (enrolment plan 1.5, migration §105).

Playbook §Phase 0 clinics: choral shadowing — the tutor models, learners repeat
with microphones muted; up to 15 learners, about 30 minutes; mixed tiers are
fine because everyone is on the Fidel. At least one weekend and one weekday
clinic per band each week, so a learner's first clinic falls within a week of
enrolling. A clinic with a single attendee becomes a 15-minute 1:1 check-in
(the database records it as `delivered_minutes = 15` when it completes).

WHO DOES WHAT
  * An ops admin schedules clinics (one, or the same time weekly for N weeks)
    with a tutor of rung >= 2 (§2k: clinics are session-tutor work).
  * A learner in Phase 0 books from their band's upcoming clinics, within their
    tier's allowance: Learner 1, Tutor 2, Intensive 2, Free 0 (playbook table;
    "at least" — an admin can always add someone by hand). A booking that was
    attended OR missed counts; a cancelled one doesn't.
  * The clinic's tutor (or ops) marks attended / absent once it has started.

WHAT THE DATABASE ENFORCES (§105): capacity under a row lock, booking only
before the start, marking only after it, completion and the 15-minute fallback.

DROP-INS (plan 3.7, §115) are the same machinery with `kind = 'drop_in'`: a
50-minute open session for up to 10 learners of any level in a band, for every
paying learner who isn't in a pod (wait pool, confirmed seat, holding an offer;
not paused, not Free), ONE A WEEK (Monday-Sunday in the band's anchor zone).
They never use session credits and don't count towards the month's included
sessions. A drop-in completes at its full 50 minutes however many came (plan
4.8). Never a cohort; the matcher never sees one (§1b.4).

ROOMS reuse the make-up path: a `video_session` for the tutor, created when the
clinic is scheduled, and a `video_session_participant` row per booking, so the
app's `/video-call` page joins it. That is tauka-python writing tauka-flutter's
video tables, extending CONFLICT-055 from make-ups to clinics.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import calling_service, email_service
from app.services import hierarchy_service as hs
from app.services.makeup_delivery import _email_of, _zones, local_time
from app.services.makeup_service import _names, _ts
from app.services.slot_service import ANCHOR
from zoneinfo import ZoneInfo
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

ALLOWANCE = {"learner_tier": 1, "tutor_tier": 2, "intensive_tier": 2}
PAID = tuple(ALLOWANCE)
KINDS = {
    "phase0": {"minutes": 30, "capacity": 15, "title": "Phase 0 clinic"},
    "drop_in": {"minutes": 50, "capacity": 10, "title": "Drop-in session"},
}
TUTOR_MIN_RUNG = 2
HORIZON = timedelta(days=21)        # how far ahead a learner sees clinics
RECENT = timedelta(days=14)         # how far back a tutor's list reaches
MAX_REPEAT_WEEKS = 12
BANDS = ("americas", "europe", "ea_gulf", "asia")
HELD = ("booked", "attended", "absent")


class ClinicRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clinic(clinic_id: str) -> dict:
    rows = (_t("clinic").select("*").eq("id", clinic_id).limit(1).execute()).data or []
    if not rows:
        raise ClinicRefused("no_such_clinic")
    return rows[0]


def _bookings(clinic_ids: list[str]) -> list[dict]:
    if not clinic_ids:
        return []
    return (_t("clinic_booking").select("clinic_id, student_id, status, reminder_sent_at")
            .in_("clinic_id", clinic_ids).execute()).data or []


# ── rooms ────────────────────────────────────────────────────────────────────

async def provision_room(clinic_id: str) -> str | None:
    """A video session for the clinic's tutor, once. Best effort: a clinic
    without a Daily room gets an empty link the tutor fills in from the app."""
    c = _clinic(clinic_id)
    if c["status"] != "scheduled" or c.get("video_session_id"):
        return c.get("video_session_id")
    start = _ts(c["starts_at"])
    end = start + timedelta(minutes=int(c["duration_minutes"]))
    url = await calling_service.create_daily_room(
        f"cl-{clinic_id.replace('-', '')}", int((end + timedelta(hours=1)).timestamp()))
    try:
        # app.video_session (tauka-flutter, video): CONFLICT-055.
        made = _t("video_session").insert({
            "tutor_id": c["tutor_id"], "title": KINDS[c.get("kind") or "phase0"]["title"],
            "scheduled_at": start.isoformat(), "duration_minutes": int(c["duration_minutes"]),
            "video_call_url": url,
        }).execute()
        vs_id = (made.data or [{}])[0].get("id")
        if not vs_id:
            return None
        held = [b for b in _bookings([clinic_id]) if b["status"] in HELD]
        if held:
            _t("video_session_participant").insert(
                [{"session_id": vs_id, "student_id": b["student_id"]} for b in held]).execute()
        _t("clinic").update({"video_session_id": vs_id}).eq("id", clinic_id).execute()
        return vs_id
    except Exception as exc:  # noqa: BLE001
        logger.error("video room for clinic %s failed: %s", clinic_id, exc)
        return None


def _join_room(c: dict, student_id: str, joined: bool) -> None:
    """Add or remove the learner from the clinic's video session. Best effort."""
    vs = c.get("video_session_id")
    if not vs:
        return
    try:
        if joined:
            _t("video_session_participant").insert({"session_id": vs, "student_id": student_id}).execute()
        else:
            _t("video_session_participant").delete().eq("session_id", vs).eq("student_id", student_id).execute()
    except Exception as exc:  # noqa: BLE001
        # A duplicate participant row on a re-book is harmless.
        logger.info("clinic room participant %s for %s: %s", "add" if joined else "remove", student_id, exc)


# ── admin ────────────────────────────────────────────────────────────────────

async def create(actor: str, band: str, tutor_id: str, starts_at: str,
                 repeat_weeks: int = 1, capacity: int | None = None, kind: str = "phase0") -> list[dict]:
    if not hs.admin_can(actor, "ops"):
        raise ClinicRefused("not_allowed")
    if kind not in KINDS:
        raise ClinicRefused("invalid_kind")
    capacity = capacity or KINDS[kind]["capacity"]
    if not 1 <= capacity <= KINDS[kind]["capacity"]:
        raise ClinicRefused("invalid_capacity")
    if band not in BANDS:
        raise ClinicRefused("invalid_band")
    if not 1 <= repeat_weeks <= MAX_REPEAT_WEEKS:
        raise ClinicRefused("invalid_repeat")
    if (hs.rung_of(tutor_id) or 0) < TUTOR_MIN_RUNG:
        raise ClinicRefused("tutor_rung")
    tutor = (_t("tutor").select("id, status, deleted_at").eq("id", tutor_id).limit(1).execute()).data or []
    if not tutor or tutor[0].get("status") != "active" or tutor[0].get("deleted_at"):
        raise ClinicRefused("tutor_inactive")
    start = _ts(starts_at)
    if start is None or start.tzinfo is None:
        raise ClinicRefused("invalid_time")
    if start <= _now():
        raise ClinicRefused("in_the_past")
    # Weekly repeats keep the WALL-CLOCK time in the band's anchor zone, like
    # catalogue slots: a clinic at 10:00 London stays 10:00 London across a
    # clock change (adding 7 days to the UTC instant would drift an hour).
    zone = ZoneInfo(ANCHOR[band])
    local = start.astimezone(zone)
    rows = [{"band": band, "tutor_id": tutor_id, "capacity": capacity, "created_by": actor,
             "kind": kind, "duration_minutes": KINDS[kind]["minutes"],
             "starts_at": datetime.combine(local.date() + timedelta(weeks=i), local.timetz().replace(tzinfo=None),
                                           tzinfo=zone).astimezone(timezone.utc).isoformat()}
            for i in range(repeat_weeks)]
    made = (_t("clinic").insert(rows).execute()).data or []
    for c in made:
        await provision_room(c["id"])
    return made


def cancel(actor: str, clinic_id: str, reason: str = "admin") -> dict:
    if not hs.admin_can(actor, "ops"):
        raise ClinicRefused("not_allowed")
    c = _clinic(clinic_id)
    if c["status"] != "scheduled":
        raise ClinicRefused("closed")
    now = _now().isoformat()
    _t("clinic_booking").update({"status": "cancelled", "cancelled_at": now}) \
        .eq("clinic_id", clinic_id).eq("status", "booked").execute()
    _t("clinic").update({"status": "cancelled", "cancelled_reason": reason}).eq("id", clinic_id).execute()
    return {**c, "status": "cancelled", "cancelled_reason": reason}


def admin_list(actor: str) -> list[dict]:
    if not hs.admin_can(actor, "ops"):
        raise ClinicRefused("not_allowed")
    rows = (_t("clinic").select("id, kind, band, tutor_id, starts_at, capacity, status, video_session_id")
            .eq("status", "scheduled").gte("starts_at", (_now() - timedelta(hours=1)).isoformat())
            .order("starts_at").execute()).data or []
    held = [b for b in _bookings([r["id"] for r in rows]) if b["status"] in HELD]
    names = _names(r["tutor_id"] for r in rows)
    return [{**r, "tutor_name": names.get(r["tutor_id"]),
             "booked": sum(1 for b in held if b["clinic_id"] == r["id"])} for r in rows]


# ── learner ──────────────────────────────────────────────────────────────────

def _learner(student_id: str) -> dict:
    """tier, band and whether they are still in Phase 0."""
    st = (_t("student").select("tier").eq("id", student_id).limit(1).execute()).data or []
    es = (_t("enrolment_state").select("state, band, phase0_completed_at")
          .eq("student_id", student_id).execute()).data or []
    in_phase0 = any(r["state"] in ("phase0", "paid") and not r.get("phase0_completed_at") for r in es)
    band = next((r["band"] for r in es if r.get("band")), None)
    return {"tier": (st[0] if st else {}).get("tier"), "band": band, "in_phase0": in_phase0}


def _held_by_kind(student_id: str) -> list[dict]:
    """The learner's held bookings, each with its clinic's kind and start."""
    held = [b for b in (_t("clinic_booking").select("clinic_id, status").eq("student_id", student_id)
                        .execute()).data or [] if b["status"] in HELD]
    if not held:
        return []
    clinics = {c["id"]: c for c in (_t("clinic").select("id, kind, starts_at, band")
               .in_("id", [b["clinic_id"] for b in held]).execute()).data or []}
    return [{**b, **{k: clinics.get(b["clinic_id"], {}).get(k) for k in ("kind", "starts_at", "band")}}
            for b in held]


def _used(student_id: str) -> int:
    """Phase 0 clinics only: drop-ins have their own weekly rule (§115)."""
    return sum(1 for b in _held_by_kind(student_id) if (b.get("kind") or "phase0") == "phase0")


def for_learner(student_id: str, band: str | None = None) -> dict:
    me = _learner(student_id)
    band = band if band in BANDS else me["band"]
    allowed = ALLOWANCE.get(me["tier"], 0)
    used = _used(student_id)
    now = _now()

    mine = (_t("clinic_booking").select("clinic_id, status").eq("student_id", student_id)
            .in_("status", ["booked", "attended", "absent"]).execute()).data or []
    my_ids = {b["clinic_id"]: b["status"] for b in mine}
    my_clinics = (_t("clinic").select("id, kind, band, tutor_id, starts_at, duration_minutes, status, video_session_id")
                  .in_("id", list(my_ids)).execute()).data or [] if my_ids else []
    my_clinics = [c for c in my_clinics if (c.get("kind") or "phase0") == "phase0"]

    open_ = []
    if band:
        open_ = (_t("clinic").select("id, kind, band, tutor_id, starts_at, duration_minutes, capacity, status")
                 .eq("band", band).eq("status", "scheduled").gte("starts_at", now.isoformat())
                 .order("starts_at").execute()).data or []
        open_ = [c for c in open_ if _ts(c["starts_at"]) <= now + HORIZON
                 and (c.get("kind") or "phase0") == "phase0"]
    held = [b for b in _bookings([c["id"] for c in open_]) if b["status"] in HELD]
    names = _names([c["tutor_id"] for c in open_ + my_clinics])

    def seats(c):
        return c["capacity"] - sum(1 for b in held if b["clinic_id"] == c["id"])

    booked = sorted(
        ({"id": c["id"], "starts_at": c["starts_at"], "duration_minutes": c["duration_minutes"],
          "tutor_name": names.get(c["tutor_id"]), "status": my_ids[c["id"]],
          "video_session_id": c.get("video_session_id")}
         for c in my_clinics if c["status"] != "cancelled"),
        key=lambda x: x["starts_at"])
    return {
        "band": band,
        "in_phase0": me["in_phase0"],
        "allowance": {"allowed": allowed, "used": used, "left": max(0, allowed - used)},
        "booked": booked,
        "open": [{"id": c["id"], "starts_at": c["starts_at"], "duration_minutes": c["duration_minutes"],
                  "tutor_name": names.get(c["tutor_id"]), "seats_left": max(0, seats(c))}
                 for c in open_ if c["id"] not in my_ids],
    }


def _not_lapsed(student_id: str) -> None:
    # A missed payment past its grace pauses bookings (plan 2.3a, §126).
    from app.services.grace_service import is_lapsed
    if is_lapsed(student_id):
        raise ClinicRefused("payment_lapsed")


def book(student_id: str, clinic_id: str) -> dict:
    _not_lapsed(student_id)
    c = _clinic(clinic_id)
    if (c.get("kind") or "phase0") == "drop_in":
        return book_drop_in(student_id, clinic_id)
    me = _learner(student_id)
    if not me["in_phase0"]:
        raise ClinicRefused("not_in_phase0")
    allowed = ALLOWANCE.get(me["tier"], 0)
    if allowed == 0:
        raise ClinicRefused("not_included")
    if _used(student_id) >= allowed:
        raise ClinicRefused("allowance_used")
    if me["band"] and c["band"] != me["band"]:
        raise ClinicRefused("other_band")
    _seat(student_id, c)
    return for_learner(student_id, c["band"])


def _seat(student_id: str, c: dict) -> None:
    """Take (or retake) a seat; the database decides full / closed."""
    clinic_id = c["id"]
    existing = (_t("clinic_booking").select("status").eq("clinic_id", clinic_id)
                .eq("student_id", student_id).limit(1).execute()).data or []
    try:
        if existing:
            if existing[0]["status"] != "cancelled":
                raise ClinicRefused("already_booked")
            _t("clinic_booking").update({"status": "booked", "cancelled_at": None, "booked_at": _now().isoformat()}) \
                .eq("clinic_id", clinic_id).eq("student_id", student_id).execute()
        else:
            _t("clinic_booking").insert({"clinic_id": clinic_id, "student_id": student_id}).execute()
    except ClinicRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "full" in text:
            raise ClinicRefused("full") from exc
        if "not open" in text:
            raise ClinicRefused("closed") from exc
        raise
    _join_room(c, student_id, True)


# ── drop-ins (plan 3.7, §115) ────────────────────────────────────────────────

def _week_of(at: datetime, band: str) -> tuple[int, int]:
    """ISO (year, week) in the band's anchor zone: Monday-Sunday."""
    iso = at.astimezone(ZoneInfo(ANCHOR[band])).isocalendar()
    return iso[0], iso[1]


def _drop_in_learner(student_id: str) -> dict:
    """Paying, not in a pod, not paused — and their band."""
    st = (_t("student").select("tier").eq("id", student_id).limit(1).execute()).data or []
    tier = (st[0] if st else {}).get("tier")
    seated = bool((_t("cohort_memberships").select("cohort_id").eq("user_id", student_id)
                   .eq("status", "active").limit(1).execute()).data)
    paused = bool((_t("enrolment_pause").select("id").eq("student_id", student_id)
                   .eq("status", "active").limit(1).execute()).data)
    es = (_t("enrolment_state").select("band").eq("student_id", student_id).execute()).data or []
    band = next((r["band"] for r in es if r.get("band")), None)
    reason = ("not_paying" if tier not in PAID else "in_a_pod" if seated else "paused" if paused else None)
    return {"eligible": reason is None, "reason": reason, "band": band}


def drop_ins_for_learner(student_id: str, band: str | None = None) -> dict:
    """GET /clinics/drop-ins — the band's upcoming drop-ins, the learner's own,
    and whether this week's one is used."""
    me = _drop_in_learner(student_id)
    band = band if band in BANDS else me["band"]
    now = _now()
    mine = [b for b in _held_by_kind(student_id) if b.get("kind") == "drop_in"]
    my_ids = {b["clinic_id"]: b["status"] for b in mine}
    open_ = []
    if band and me["eligible"]:
        open_ = (_t("clinic").select("id, kind, band, tutor_id, starts_at, duration_minutes, capacity, status")
                 .eq("kind", "drop_in").eq("band", band).eq("status", "scheduled")
                 .gte("starts_at", now.isoformat()).order("starts_at").execute()).data or []
        open_ = [c for c in open_ if _ts(c["starts_at"]) <= now + HORIZON]
    held = [b for b in _bookings([c["id"] for c in open_]) if b["status"] in HELD]
    weeks = {_week_of(_ts(b["starts_at"]), b["band"]) for b in mine if b.get("starts_at") and b.get("band")}
    my_clinics = (_t("clinic").select("id, band, tutor_id, starts_at, duration_minutes, status, video_session_id")
                  .in_("id", list(my_ids)).execute()).data or [] if my_ids else []
    names = _names([c["tutor_id"] for c in open_ + my_clinics])
    return {
        "band": band, "eligible": me["eligible"], "reason": me["reason"],
        "booked": sorted(({"id": c["id"], "starts_at": c["starts_at"], "duration_minutes": c["duration_minutes"],
                           "tutor_name": names.get(c["tutor_id"]), "status": my_ids[c["id"]],
                           "video_session_id": c.get("video_session_id")}
                          for c in my_clinics if c["status"] != "cancelled" and _ts(c["starts_at"]) >= now - RECENT),
                         key=lambda x: x["starts_at"]),
        "open": [{"id": c["id"], "starts_at": c["starts_at"], "duration_minutes": c["duration_minutes"],
                  "tutor_name": names.get(c["tutor_id"]),
                  "seats_left": max(0, c["capacity"] - sum(1 for b in held if b["clinic_id"] == c["id"])),
                  "week_used": _week_of(_ts(c["starts_at"]), c["band"]) in weeks}
                 for c in open_ if c["id"] not in my_ids],
    }


def book_drop_in(student_id: str, clinic_id: str) -> dict:
    _not_lapsed(student_id)
    c = _clinic(clinic_id)
    if c.get("kind") != "drop_in":
        raise ClinicRefused("not_a_drop_in")
    me = _drop_in_learner(student_id)
    if not me["eligible"]:
        raise ClinicRefused(me["reason"])
    if me["band"] and c["band"] != me["band"]:
        raise ClinicRefused("other_band")
    week = _week_of(_ts(c["starts_at"]), c["band"])
    if any(b.get("kind") == "drop_in" and b["clinic_id"] != clinic_id and b.get("starts_at")
           and _week_of(_ts(b["starts_at"]), b.get("band") or c["band"]) == week
           for b in _held_by_kind(student_id)):
        raise ClinicRefused("week_used")
    _seat(student_id, c)
    return drop_ins_for_learner(student_id, c["band"])


def cancel_booking(student_id: str, clinic_id: str) -> dict:
    c = _clinic(clinic_id)
    if _ts(c["starts_at"]) <= _now():
        raise ClinicRefused("started")
    done = (_t("clinic_booking").update({"status": "cancelled", "cancelled_at": _now().isoformat()})
            .eq("clinic_id", clinic_id).eq("student_id", student_id).eq("status", "booked").execute()).data
    if not done:
        raise ClinicRefused("not_booked")
    _join_room(c, student_id, False)
    if c.get("kind") == "drop_in":
        return drop_ins_for_learner(student_id, c["band"])
    return for_learner(student_id, c["band"])


# ── tutor ────────────────────────────────────────────────────────────────────

def for_tutor(tutor_id: str) -> list[dict]:
    now = _now()
    cols = "id, kind, band, starts_at, duration_minutes, status, delivered_minutes, video_session_id"
    rows = (_t("clinic").select(cols)
            .eq("tutor_id", tutor_id).in_("status", ["scheduled", "completed"])
            .gte("starts_at", (now - RECENT).isoformat()).order("starts_at").execute()).data or []
    # Track 10 (§140): two-tutor clinics show for the co-tutor too.
    co = (_t("clinic").select(cols).eq("co_tutor_id", tutor_id).in_("status", ["scheduled", "completed"])
          .gte("starts_at", (now - RECENT).isoformat()).order("starts_at").execute()).data or []
    rows = sorted({r["id"]: r for r in rows + co}.values(), key=lambda r: str(r["starts_at"]))
    held = [b for b in _bookings([r["id"] for r in rows]) if b["status"] in HELD]
    names = _names(b["student_id"] for b in held)
    video = {}
    vids = [r["video_session_id"] for r in rows if r.get("video_session_id")]
    if vids:
        video = {v["id"]: v for v in (_t("video_session").select("id, video_call_url")
                                      .in_("id", vids).execute()).data or []}
    out = []
    for r in rows:
        seats = [b for b in held if b["clinic_id"] == r["id"]]
        out.append({
            "id": r["id"], "kind": r.get("kind") or "phase0", "band": r["band"], "starts_at": r["starts_at"],
            "duration_minutes": r["duration_minutes"], "status": r["status"],
            "delivered_minutes": r.get("delivered_minutes"), "video_session_id": r.get("video_session_id"),
            "has_room": bool((video.get(r.get("video_session_id")) or {}).get("video_call_url")),
            "can_mark": _ts(r["starts_at"]) <= now and r["status"] == "scheduled",
            "learners": [{"student_id": b["student_id"], "name": names.get(b["student_id"]),
                          "status": b["status"]} for b in seats],
        })
    return out


def mark(actor: str, clinic_id: str, marks: dict[str, str]) -> list[dict]:
    c = _clinic(clinic_id)
    if actor not in (c["tutor_id"], c.get("co_tutor_id")) and not hs.admin_can(actor, "ops"):
        raise ClinicRefused("not_allowed")
    if any(v not in ("attended", "absent") for v in marks.values()):
        raise ClinicRefused("invalid_mark")
    if _ts(c["starts_at"]) > _now():
        raise ClinicRefused("not_started")
    held = {b["student_id"] for b in _bookings([clinic_id]) if b["status"] in HELD}
    if any(s not in held for s in marks):
        raise ClinicRefused("not_in_clinic")
    for sid, status in marks.items():
        _t("clinic_booking").update({"status": status}).eq("clinic_id", clinic_id).eq("student_id", sid).execute()
    return for_tutor(c["tutor_id"])


# ── daily ────────────────────────────────────────────────────────────────────

async def daily(now: datetime | None = None) -> dict:
    """Rooms for any scheduled clinic missing one; a reminder email per booking
    within the day before, once (`clinic_booking.reminder_sent_at`)."""
    now = now or _now()
    rooms = reminders = 0
    emptied = cancel_empty(now)
    upcoming = (_t("clinic").select("id, kind, starts_at, duration_minutes, video_session_id")
                .eq("status", "scheduled").gte("starts_at", now.isoformat()).execute()).data or []
    for c in upcoming:
        if not c.get("video_session_id") and await provision_room(c["id"]):
            rooms += 1
    due = [c for c in upcoming if _ts(c["starts_at"]) - now <= timedelta(hours=24)]
    for c in due:
        pending = [b for b in _bookings([c["id"]]) if b["status"] == "booked" and not b.get("reminder_sent_at")]
        zones = _zones([b["student_id"] for b in pending]) if pending else {}
        for b in pending:
            to = _email_of(b["student_id"])
            if to:
                drop_in = c.get("kind") == "drop_in"
                await email_service.send_template_email(to, "clinic", {
                    "subject": "Your drop-in session is tomorrow" if drop_in else "Your Phase 0 clinic is tomorrow",
                    "kind": c.get("kind") or "phase0",
                    "when": local_time(_ts(c["starts_at"]), zones.get(b["student_id"], "UTC")),
                    "minutes": c["duration_minutes"], "web_base_url": settings.web_base_url,
                }, tags=["clinic", "reminder"])
            _t("clinic_booking").update({"reminder_sent_at": now.isoformat()}) \
                .eq("clinic_id", c["id"]).eq("student_id", b["student_id"]).execute()
            reminders += 1
    return {"rooms": rooms, "reminders": reminders, "cancelled_empty": emptied}


# ── unmarked and empty ───────────────────────────────────────────────────────

def unmarked(hours: int = 24, now: datetime | None = None) -> list[dict]:
    """Clinics that ended more than `hours` ago with a booking still unmarked:
    unpaid, and the learners' allowance shows them as used regardless."""
    now = now or _now()
    rows = (_t("clinic").select("id, tutor_id, starts_at, duration_minutes, status")
            .eq("status", "scheduled").execute()).data or []
    late = [r for r in rows
            if _ts(r["starts_at"]) + timedelta(minutes=int(r["duration_minutes"]), hours=hours) <= now]
    open_ = {b["clinic_id"] for b in _bookings([r["id"] for r in late]) if b["status"] == "booked"}
    return sorted((r for r in late if r["id"] in open_), key=lambda r: r["starts_at"])


def cancel_empty(now: datetime | None = None) -> int:
    """A clinic whose start has passed with nobody holding a seat can never
    complete (completion follows marking), so it is cancelled `no_bookings`
    rather than left `scheduled` for ever."""
    now = now or _now()
    rows = (_t("clinic").select("id, starts_at").eq("status", "scheduled").execute()).data or []
    started = [r for r in rows if _ts(r["starts_at"]) <= now]
    held = {b["clinic_id"] for b in _bookings([r["id"] for r in started]) if b["status"] in HELD}
    n = 0
    for r in started:
        if r["id"] not in held:
            _t("clinic").update({"status": "cancelled", "cancelled_reason": "no_bookings"}) \
                .eq("id", r["id"]).eq("status", "scheduled").execute()
            n += 1
    return n
