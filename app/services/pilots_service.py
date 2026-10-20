"""Track 10 pilots and the plan's last remainders (migration §140).

  holidays      holiday mini-lessons for Explore, in the two weeks before an
                Ethiopian holiday and three days after; dates computed.
  gatherings    city meetups and season gatherings: ops schedules, learners
                RSVP within capacity, the host is paid when it's marked done.
  mid-month     a band-only intake on Ethiopic day 15 (twice-monthly formation).
  co-tutors     two-tutor clinics: ops adds a second tutor; both are paid.
  clinic-mates  learners who attended 2+ clinics together, as a nudge to make
                a friend group (first names only).
  mentoring     a monthly peer session pairing a heritage learner (level 3+,
                opted in to mentor) with a beginner (level 1) in the same band.
  second review 1.4: a 20% sample of rung-1 Fidel reviews gets a second,
                rung >= 2 reviewer; calibration only, never the learner's verdict.
"""

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone

from app.core import ethiopic_calendar as ec
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

HOLIDAY_BEFORE = timedelta(days=14)
HOLIDAY_AFTER = timedelta(days=3)
SECOND_REVIEW_SHARE = 5          # 1 in 5 = 20%
MENTOR_LEVEL = 3


class PilotRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise PilotRefused("not_allowed")


# ── holidays ────────────────────────────────────────────────────────────────

def orthodox_easter(year: int) -> date:
    """Fasika: the Julian-calendar Easter (Meeus), +13 days to Gregorian (1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    return date(year, month, day) + timedelta(days=13)


def holiday_dates(year: int) -> dict[str, date]:
    """The five holidays falling in Gregorian `year`."""
    ny = ec.from_gregorian(date(year, 9, 15)).year             # the E.C. year starting in this September
    prev = ny - 1
    return {
        "enkutatash": ec.to_gregorian(ec.EthiopicDate(ny, 1, 1)),
        "meskel": ec.to_gregorian(ec.EthiopicDate(ny, 1, 17)),
        "genna": date(year, 1, 7),
        "timket": ec.to_gregorian(ec.EthiopicDate(prev, 5, 11)),
        "fasika": orthodox_easter(year),
    }


def current_holiday(today: date) -> tuple[str, date] | None:
    """The holiday whose window holds today; when two overlap (Genna and Timket),
    the coming one wins over the one just past."""
    hits = [(k, d) for y in (today.year - 1, today.year, today.year + 1) for k, d in holiday_dates(y).items()
            if d - HOLIDAY_BEFORE <= today <= d + HOLIDAY_AFTER]
    if not hits:
        return None
    ahead = sorted((h for h in hits if h[1] >= today), key=lambda h: h[1])
    return ahead[0] if ahead else max(hits, key=lambda h: h[1])


def holiday_lesson(today: date | None = None) -> dict | None:
    today = today or _now().date()
    cur = current_holiday(today)
    if not cur:
        return None
    rows = (_t("holiday_lesson").select("id, holiday, title, body, created_at").eq("holiday", cur[0])
            .eq("active", True).execute()).data or []
    if not rows:
        return None
    r = max(rows, key=lambda x: str(x.get("created_at")))
    return {**r, "date": cur[1].isoformat()}


def add_holiday_lesson(actor: str, holiday: str, title: str, body: str) -> dict:
    _ops(actor)
    if holiday not in ("enkutatash", "meskel", "genna", "timket", "fasika"):
        raise PilotRefused("bad_holiday")
    return (_t("holiday_lesson").insert({"holiday": holiday, "title": title.strip()[:120], "body": body.strip()[:4000],
                                         "created_by": actor, "active": True}).execute()).data[0]


# ── gatherings ──────────────────────────────────────────────────────────────

def create_gathering(actor: str, kind: str, title: str, city: str, starts_at: datetime, capacity: int,
                     host_tutor_id: str | None = None, season_id: str | None = None) -> dict:
    _ops(actor)
    if kind not in ("meetup", "gathering") or capacity < 2:
        raise PilotRefused("bad_gathering")
    g = (_t("gathering").insert({"kind": kind, "title": title.strip()[:120], "city": city.strip()[:80],
                                 "starts_at": starts_at.isoformat(), "capacity": capacity, "host_tutor_id": host_tutor_id,
                                 "season_id": season_id, "status": "scheduled", "created_by": actor}).execute()).data[0]
    if season_id:
        try:
            # CONFLICT-061: the season channel hears about it.
            _t("broadcast").insert({"content": f"{title} · {city} · {starts_at:%a %d %b, %H:%M} UTC. RSVP in the app.",
                                    "target_type": "season", "target_id": season_id,
                                    "published_at": _now().isoformat()}).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gathering post failed: %s", exc)
    return g


def gatherings(student_id: str, now: datetime | None = None) -> list[dict]:
    now = now or _now()
    rows = [g for g in (_t("gathering").select("*").eq("status", "scheduled").execute()).data or []
            if _ts(g["starts_at"]) >= now]
    rsvps = (_t("gathering_rsvp").select("gathering_id, student_id, status").in_("gathering_id", [g["id"] for g in rows] or ["00000000-0000-0000-0000-000000000000"])
             .eq("status", "going").execute()).data or []
    out = []
    for g in sorted(rows, key=lambda g: str(g["starts_at"])):
        going = [r for r in rsvps if r["gathering_id"] == g["id"]]
        out.append({k: g[k] for k in ("id", "kind", "title", "city", "starts_at", "capacity")} |
                   {"going": len(going), "mine": any(r["student_id"] == student_id for r in going)})
    return out


def rsvp(student_id: str, gathering_id: str, going: bool) -> dict:
    g = (_t("gathering").select("*").eq("id", gathering_id).limit(1).execute()).data or []
    if not g or g[0]["status"] != "scheduled":
        raise PilotRefused("no_such_gathering")
    if going:
        n = len((_t("gathering_rsvp").select("student_id").eq("gathering_id", gathering_id).eq("status", "going")
                 .execute()).data or [])
        if n >= int(g[0]["capacity"]):
            raise PilotRefused("full")
    _t("gathering_rsvp").upsert({"gathering_id": gathering_id, "student_id": student_id,
                                 "status": "going" if going else "cancelled"}).execute()
    return {"going": going}


def finish_gathering(actor: str, gathering_id: str) -> dict:
    _ops(actor)
    got = (_t("gathering").update({"status": "done"}).eq("id", gathering_id).eq("status", "scheduled").execute()).data or []
    if not got:
        raise PilotRefused("not_open")
    g = got[0]
    if g.get("host_tutor_id"):
        try:
            _t("tutor_work_item").insert({"tutor_id": g["host_tutor_id"], "kind": "gathering", "quantity": 1,
                                          "occurred_on": str(g["starts_at"])[:10], "ref_code": f"gathering:{g['id']}",
                                          "added_by": actor}).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gathering pay item failed: %s", exc)
    return g


# ── a mid-month intake for one band ─────────────────────────────────────────

def open_mid_month(actor: str, band: str, ec_year: int, ec_month: int, today: date | None = None) -> dict:
    """Ethiopic day 15 of that month, for one band; the lesson deadline 9 days before."""
    _ops(actor)
    if band not in ("americas", "europe", "ea_gulf", "asia") or not 1 <= ec_month <= 12:
        raise PilotRefused("bad_intake")
    formation = ec.to_gregorian(ec.EthiopicDate(ec_year, ec_month, 15))
    today = today or _now().date()
    if formation <= today + timedelta(days=14):
        raise PilotRefused("too_soon")
    base = (_t("intake").select("season_id, ethiopic_month").eq("ec_year", ec_year).execute()).data or []
    season = next((b.get("season_id") for b in base if b.get("ethiopic_month") == ec.MONTHS[ec_month - 1]), None)
    return (_t("intake").insert({
        "ethiopic_month": ec.MONTHS[ec_month - 1], "ec_year": ec_year, "formation_date": formation.isoformat(),
        "cutoff_opens": (formation - timedelta(days=14)).isoformat(),
        "cutoff_closes": (formation - timedelta(days=9)).isoformat(),
        "season_id": season, "tier": "standard", "status": "scheduled", "mode": "live", "band": band,
    }).execute()).data[0]


# ── two-tutor clinics ───────────────────────────────────────────────────────

def set_co_tutor(actor: str, clinic_id: str, tutor_id: str | None) -> dict:
    _ops(actor)
    c = (_t("clinic").select("id, tutor_id, status").eq("id", clinic_id).limit(1).execute()).data or []
    if not c or c[0]["status"] != "scheduled":
        raise PilotRefused("not_open")
    if tutor_id and (tutor_id == c[0]["tutor_id"] or (hs.rung_of(tutor_id) or 0) < 2):
        raise PilotRefused("bad_tutor")
    return (_t("clinic").update({"co_tutor_id": tutor_id}).eq("id", clinic_id).execute()).data[0]


# ── clinic-mates ────────────────────────────────────────────────────────────

def clinic_mates(student_id: str, min_shared: int = 2) -> list[dict]:
    from app.services.makeup_service import _names
    mine = {b["clinic_id"] for b in (_t("clinic_booking").select("clinic_id, status").eq("student_id", student_id)
            .eq("status", "attended").execute()).data or []}
    if not mine:
        return []
    counts: dict[str, int] = {}
    for b in (_t("clinic_booking").select("clinic_id, student_id, status").in_("clinic_id", list(mine))
              .eq("status", "attended").execute()).data or []:
        if b["student_id"] != student_id:
            counts[b["student_id"]] = counts.get(b["student_id"], 0) + 1
    mates = [s for s, n in counts.items() if n >= min_shared]
    names = _names(mates) if mates else {}
    return sorted(({"first_name": (names.get(s) or "A learner").split(" ")[0], "clinics_together": counts[s]}
                   for s in mates), key=lambda m: -m["clinics_together"])


# ── heritage-beginner mentoring ─────────────────────────────────────────────

def set_mentor(student_id: str, on: bool) -> dict:
    _t("peer_optin").upsert({"student_id": student_id, "opted_in": True, "mentor": bool(on),
                             "updated_at": _now().isoformat()}).execute()
    return {"mentor": bool(on)}


def pair_mentoring(now: datetime | None = None) -> dict:
    """First Monday of the month: mentors (level 3+) with beginners (level 1),
    same band, via the peer-session machinery (kind 'mentoring')."""
    from app.core.flags import is_ready
    from app.services import peer_service as ps
    now = now or _now()
    if not is_ready("peer_sessions") or now.weekday() != 0 or now.day > 7:
        return {"paired": 0}
    opt = {o["student_id"]: o for o in (_t("peer_optin").select("student_id, opted_in, mentor").execute()).data or []}
    pool = ps.pool(now)
    prof = ps._profile(pool)
    mentors = sorted(s for s in prof if opt.get(s, {}).get("mentor") and (prof[s]["level"] or 0) >= MENTOR_LEVEL)
    beginners = sorted(s for s in prof if (prof[s]["level"] or 0) == 1 and not opt.get(s, {}).get("mentor"))
    blocked = ps._blocked()
    taken: set[str] = set()
    paired = 0
    for m in mentors:
        for b in beginners:
            if b in taken or prof[b]["band"] != prof[m]["band"] or frozenset((m, b)) in blocked:
                continue
            at = now + timedelta(days=5)
            s = (_t("peer_session").insert({"kind": "mentoring", "starts_at": at.isoformat(), "minutes": 20,
                                            "level": 1, "band": prof[m]["band"], "season_id": prof[m]["season"],
                                            "status": "scheduled"}).execute()).data[0]
            _t("peer_session_participant").insert([{"session_id": s["id"], "student_id": m, "role": "a"},
                                                   {"session_id": s["id"], "student_id": b, "role": "b"}]).execute()
            taken.add(b)
            paired += 1
            break
    return {"paired": paired}


# ── 1.4: the second-review sample ───────────────────────────────────────────

def sampled(approval_id: str) -> bool:
    """Deterministic 1-in-5: the same reading is always in or out."""
    return int(hashlib.sha256(approval_id.encode()).hexdigest(), 16) % SECOND_REVIEW_SHARE == 0


def mark_for_second_review(approval_id: str, reviewer: str) -> bool:
    if (hs.rung_of(reviewer) or 0) != 1 or not sampled(approval_id):
        return False
    _t("fidel_approval").update({"second_status": "pending"}).eq("id", approval_id).execute()
    return True


def second_queue(actor: str) -> list[dict]:
    if (hs.rung_of(actor) or 0) < 2 and not hs.admin_can(actor, "ops"):
        raise PilotRefused("not_allowed")
    rows = [r for r in (_t("fidel_approval").select("id, status, gap_codes, reviewed_by, group_title, submission_id")
            .eq("second_status", "pending").execute()).data or [] if r.get("reviewed_by") != actor]
    subs = [r["submission_id"] for r in rows if r.get("submission_id")]
    paths = {}
    if subs:
        paths = {x["id"]: x["storage_path"] for x in (_t("speech_submission").select("id, storage_path")
                 .in_("id", subs).execute()).data or []}
    from app.services.fidel_service import _signed_url
    # The first verdict is not shown: the second reviewer listens blind.
    return [{"id": r["id"], "group_title": r.get("group_title"),
             "audio_url": _signed_url(paths[r["submission_id"]]) if r.get("submission_id") in paths else None}
            for r in rows]


def second_review(actor: str, approval_id: str, approved: bool) -> dict:
    if (hs.rung_of(actor) or 0) < 2 and not hs.admin_can(actor, "ops"):
        raise PilotRefused("not_allowed")
    r = (_t("fidel_approval").select("id, status, reviewed_by, second_status").eq("id", approval_id).limit(1)
         .execute()).data or []
    if not r or r[0].get("second_status") != "pending" or r[0].get("reviewed_by") == actor:
        raise PilotRefused("not_open")
    agree = (r[0]["status"] == "approved") == bool(approved)
    return (_t("fidel_approval").update({"second_reviewer_id": actor, "second_status": "agree" if agree else "disagree",
                                         "second_reviewed_at": _now().isoformat()})
            .eq("id", approval_id).execute()).data[0]


def calibration(actor: str) -> list[dict]:
    """Per rung-1 reviewer: second-reviewed readings and how often the second agreed."""
    _ops(actor)
    from app.services.makeup_service import _names
    rows = [r for r in (_t("fidel_approval").select("reviewed_by, second_status").execute()).data or []
            if r.get("second_status") in ("agree", "disagree")]
    by: dict[str, list[int]] = {}
    for r in rows:
        by.setdefault(r["reviewed_by"], []).append(1 if r["second_status"] == "agree" else 0)
    names = _names(list(by)) if by else {}
    return sorted(({"tutor_id": t, "name": names.get(t), "checked": len(v), "agreement": round(sum(v) / len(v), 2)}
                   for t, v in by.items()), key=lambda x: x["agreement"])
