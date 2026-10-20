"""The slot catalogue and learners' slot choices (enrolment plan 3.1, E-12, §104).

Learners choose from published pod slots instead of naming free times. Each
band has one anchor zone (New York, London, Nairobi, Shanghai) and every slot is
a weekday and time in it; the tutor side absorbs clock changes. A learner ticks
every slot they could make and stars favourites. **Three or more earns the
matching guarantee** (playbook) — shown, and stored as the count; eligibility to
be matched needs only `match.min_catalogue_slots` (default 1).

The matcher reads choices through `windows_for()`: each chosen slot becomes an
exact weekly window of one pod session in the band's anchor zone, favourites
ranked 1 and the rest 2. Because a band shares one zone, the matcher's time-band
check keeps bands apart without knowing they exist.

`next_at` on each slot is its next occurrence as a real UTC instant, so a client
shows it in the viewer's own local time without doing zone arithmetic.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

BANDS = ("americas", "europe", "ea_gulf", "asia")
GUARANTEE_SLOTS = 3

# Same mapping the §104 CHECK enforces.
ANCHOR = {
    "americas": "America/New_York",
    "europe": "Europe/London",
    "ea_gulf": "Africa/Nairobi",
    "asia": "Asia/Shanghai",
}

# Gulf and Levant zones whose region prefix ("Asia/") would otherwise read as
# the Asia band. Playbook: East Africa + Gulf covers UTC+2..+4 including the
# Gulf states and Israel.
_EA_GULF_ASIA = {
    "Asia/Riyadh", "Asia/Dubai", "Asia/Qatar", "Asia/Kuwait", "Asia/Bahrain", "Asia/Muscat",
    "Asia/Aden", "Asia/Baghdad", "Asia/Jerusalem", "Asia/Tel_Aviv", "Asia/Amman",
    "Asia/Beirut", "Asia/Damascus", "Asia/Tbilisi", "Asia/Yerevan", "Asia/Baku",
}


class SlotRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def band_for_timezone(tz: str | None) -> str | None:
    """A default band from an IANA zone. Only a default: the learner picks."""
    if not tz:
        return None
    if tz.startswith("America/") or tz.startswith("US/") or tz.startswith("Canada/"):
        return "americas"
    if tz.startswith("Europe/") or tz.startswith("Atlantic/"):
        return "europe"
    if tz.startswith("Africa/") or tz.startswith("Indian/") or tz in _EA_GULF_ASIA:
        return "ea_gulf"
    if tz.startswith("Asia/") or tz.startswith("Australia/") or tz.startswith("Pacific/"):
        return "asia"
    return None


def _start(raw) -> time:
    if isinstance(raw, time):
        return raw
    hh, mm = str(raw).split(":")[:2]
    return time(int(hh), int(mm))


def next_at(slot: dict, now: datetime) -> datetime:
    """The slot's next start at or after `now`, as an aware UTC instant (DST-safe:
    the wall-clock time is fixed in the anchor zone)."""
    zone = ZoneInfo(slot["anchor_timezone"])
    local_now = now.astimezone(zone)
    days = (int(slot["day_of_week"]) - local_now.weekday()) % 7
    d: date = local_now.date() + timedelta(days=days)
    at = datetime.combine(d, _start(slot["start_time"]), tzinfo=zone)
    if at < local_now:
        at = datetime.combine(d + timedelta(days=7), _start(slot["start_time"]), tzinfo=zone)
    return at.astimezone(timezone.utc)


def catalogue(band: str | None = None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    q = _t("slot_catalogue").select("id, band, anchor_timezone, day_of_week, start_time, kind, sort_order") \
        .eq("active", True)
    if band:
        q = q.eq("band", band)
    rows = q.order("band").order("sort_order").execute().data or []
    return [{**r, "start_time": str(r["start_time"])[:5], "next_at": next_at(r, now).isoformat()} for r in rows]


def choices(student_id: str) -> list[dict]:
    return (_t("student_slot_choice").select("slot_id, favourite")
            .eq("student_id", student_id).execute()).data or []


def _learner_level(student_id: str) -> int | None:
    from app.services.cohort_matcher import LEVEL_FROM_CEFR
    rows = (_t("student").select("assessed_level").eq("id", student_id).limit(1).execute()).data or []
    return LEVEL_FROM_CEFR.get(((rows[0] if rows else {}).get("assessed_level") or "").upper())


def seats_by_slot(slots: list[dict], level: int | None, now: datetime | None = None) -> dict[str, int]:
    """Open seats in live pods sitting in each catalogue slot, at `level` — the
    playbook's "Sat 10:00 Americas, level 1: 2 seats left" (plan 3.1).

    A pod stores its slot as weekday + time in its TUTOR's zone (`app.cohorts`
    slot_* columns), not as a catalogue id, so a pod sits in a catalogue slot
    when both next occur at the same instant. No new column on tauka-flutter's
    table is needed. Only forming/active pods with room count; a slot with no
    such pod is absent from the result (the app shows nothing rather than 0).
    """
    if level is None or not slots:
        return {}
    now = now or datetime.now(timezone.utc)
    # app.cohorts (tauka-flutter): status, level, max_students, slot_* (tutor's zone).
    pods = (_t("cohorts").select("id, level, max_students, slot_day_of_week, slot_time, slot_timezone, status")
            .in_("status", ["forming", "active"]).eq("level", level).execute()).data or []
    pods = [p for p in pods if p.get("slot_timezone") and p.get("slot_time") is not None
            and p.get("slot_day_of_week") is not None]
    if not pods:
        return {}
    members = (_t("cohort_memberships").select("cohort_id").in_("cohort_id", [p["id"] for p in pods])
               .eq("status", "active").execute()).data or []
    taken: dict[str, int] = {}
    for m in members:
        taken[m["cohort_id"]] = taken.get(m["cohort_id"], 0) + 1
    by_instant: dict[datetime, int] = {}
    for p in pods:
        left = int(p.get("max_students") or 4) - taken.get(p["id"], 0)
        if left <= 0:
            continue
        at = next_at({"anchor_timezone": p["slot_timezone"], "day_of_week": p["slot_day_of_week"],
                      "start_time": p["slot_time"]}, now)
        by_instant[at] = by_instant.get(at, 0) + left
    out = {}
    for s in slots:
        n = by_instant.get(next_at(s, now))
        if n:
            out[s["id"]] = n
    return out


def view(student_id: str, band: str | None, tz: str | None = None) -> dict:
    """GET /enrolment/slots — the band's catalogue with the learner's choices,
    and open seats in live pods at the learner's level where there are any."""
    band = band if band in BANDS else band_for_timezone(tz)
    mine = {c["slot_id"]: c["favourite"] for c in choices(student_id)}
    slots = catalogue(band) if band else []
    try:
        seats = seats_by_slot(slots, _learner_level(student_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("seat counts failed for %s: %s", student_id, exc)
        seats = {}
    return {
        "band": band,
        "slots": [{**s, "chosen": s["id"] in mine, "favourite": bool(mine.get(s["id"])),
                   "seats_left": seats.get(s["id"])} for s in slots],
        "chosen_count": len(mine),
        "guarantee_slots": GUARANTEE_SLOTS,
        "guaranteed": len(mine) >= GUARANTEE_SLOTS,
    }


def save(student_id: str, band: str, picked: list[dict]) -> dict:
    """Replace the learner's choices. All slots must be active and in one band."""
    if band not in BANDS:
        raise SlotRefused("invalid_band")
    ids = [p["slot_id"] for p in picked]
    if len(set(ids)) != len(ids):
        raise SlotRefused("duplicate_slot")
    if ids:
        known = {r["id"]: r for r in (_t("slot_catalogue").select("id, band, active")
                                      .in_("id", ids).execute()).data or []}
        if any(i not in known or not known[i]["active"] for i in ids):
            raise SlotRefused("unknown_slot")
        if any(known[i]["band"] != band for i in ids):
            raise SlotRefused("mixed_bands")
    _t("student_slot_choice").delete().eq("student_id", student_id).execute()
    if picked:
        _t("student_slot_choice").insert([
            {"student_id": student_id, "slot_id": p["slot_id"], "favourite": bool(p.get("favourite"))}
            for p in picked
        ]).execute()
    # The band follows the slots (enrolment_state.band, used by the protocol).
    try:
        _t("enrolment_state").update({"band": band}).eq("student_id", student_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("band update failed for %s: %s", student_id, exc)
    return view(student_id, band)


def windows_for(student_ids: list[str], session_minutes: int) -> dict[str, tuple[str, list[tuple]]]:
    """For the matcher: student -> (anchor zone, [(day, start_minute, end_minute, rank)]).

    Only learners who chose slots appear; everyone else keeps their free windows.
    A learner whose choices somehow span bands keeps only the band of their first
    favourite (or first choice) — never two zones, which would mix bands.
    """
    if not student_ids:
        return {}
    rows = (_t("student_slot_choice").select(
        "student_id, favourite, slot:slot_id(band, anchor_timezone, day_of_week, start_time, active)"
    ).in_("student_id", student_ids).execute()).data or []
    by_student: dict[str, list[dict]] = {}
    for r in rows:
        s = r.get("slot") or {}
        if s.get("active"):
            by_student.setdefault(r["student_id"], []).append({**s, "favourite": r["favourite"]})
    out = {}
    for sid, slots in by_student.items():
        lead = next((s for s in slots if s["favourite"]), slots[0])
        same = [s for s in slots if s["band"] == lead["band"]]
        wins = []
        for s in same:
            st = _start(s["start_time"])
            m = st.hour * 60 + st.minute
            wins.append((int(s["day_of_week"]), m, m + session_minutes, 1 if s["favourite"] else 2))
        out[sid] = (lead["anchor_timezone"], wins)
    return out


# ── admin: the catalogue editor (plan 3.1) ──────────────────────────────────
#
# Ops only. A slot's band, day and time never change once created: learners
# chose THAT time, and editing it would silently move their choice. To move a
# slot, add the new one and retire the old (`active = false`); retired slots
# stay referenced by past choices (ON DELETE RESTRICT) and drop out of matching.

def _ops(actor: str) -> None:
    from app.services import hierarchy_service as hs
    if not hs.admin_can(actor, "ops"):
        raise SlotRefused("not_allowed")


def admin_list(actor: str) -> list[dict]:
    _ops(actor)
    rows = (_t("slot_catalogue").select("id, band, anchor_timezone, day_of_week, start_time, kind, active, sort_order")
            .order("band").order("sort_order").execute()).data or []
    counts: dict[str, int] = {}
    for c in (_t("student_slot_choice").select("slot_id").execute()).data or []:
        counts[c["slot_id"]] = counts.get(c["slot_id"], 0) + 1
    now = datetime.now(timezone.utc)
    return [{**r, "start_time": str(r["start_time"])[:5], "chosen_by": counts.get(r["id"], 0),
             "next_at": next_at(r, now).isoformat()} for r in rows]


def admin_create(actor: str, band: str, day_of_week: int, start_time: str, kind: str,
                 sort_order: int = 0) -> dict:
    _ops(actor)
    if band not in BANDS:
        raise SlotRefused("invalid_band")
    if not 0 <= day_of_week <= 6 or kind not in ("weekend", "weekday"):
        raise SlotRefused("invalid_slot")
    try:
        _start(start_time)
    except Exception as exc:  # noqa: BLE001
        raise SlotRefused("invalid_time") from exc
    try:
        made = _t("slot_catalogue").insert({
            "band": band, "anchor_timezone": ANCHOR[band], "day_of_week": day_of_week,
            "start_time": start_time, "kind": kind, "sort_order": sort_order,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise SlotRefused("exists") from exc
        raise
    return (made.data or [{}])[0]


def admin_update(actor: str, slot_id: str, active: bool | None = None, sort_order: int | None = None,
                 kind: str | None = None) -> dict:
    """Retire/restore, reorder or relabel weekend/weekday. Never the time."""
    _ops(actor)
    patch = {k: v for k, v in (("active", active), ("sort_order", sort_order), ("kind", kind)) if v is not None}
    if kind is not None and kind not in ("weekend", "weekday"):
        raise SlotRefused("invalid_slot")
    if not patch:
        raise SlotRefused("nothing_to_change")
    done = (_t("slot_catalogue").update(patch).eq("id", slot_id).execute()).data
    if not done:
        raise SlotRefused("no_such_slot")
    return done[0]
