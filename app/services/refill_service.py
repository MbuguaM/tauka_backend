"""Seat refills (enrolment plan 3.6, migration §112).

A live pod with an empty seat — formed with three, or a member moved at a
checkpoint, paused (§111) or left — is offered to a waiting learner who fits:

  * the same LEVEL;
  * a catalogue slot they chose that sits at the pod's time (the same instant
    test as the slot picker's seat counts, plan 3.1);
  * their lesson keeps the pod's spread within `match.lesson_band`.

WHEN: during the first 14 days after a formation day (Ethiopic day 1-14). For
a new pod that is "fourth_seat_closes_at = formation + 14 days"; every later
month it is §2c's "the seat is refilled at the next checkpoint". Outside that
window a seat waits, so a pod isn't joined by a stranger in its last fortnight.

WHO, in order: the wait pool (first priority, §102 / plan 3.7), then learners
with a confirmed seat aimed at a later formation; within each, the longest
waiting, then a favourite slot, then the nearest lesson. Never someone holding
an offer, in a pod, paused, or in a friend group or private pod (a group is
seated together at a formation, plan 3.2).

HOW: ordinary `match_offer` rows under a daily `refill` cycle, so accepting is
the formation's accept (capacity checked against confirmed members; the
placement email follows, plan 3.5). At most one offer per open seat plus
`match.overbook_slack` per pod, one per learner. An offer moves the enrolment to
`matched`; a decline or expiry leaves the learner in the wait pool, still first
in line. The refill cycle is closed here once its offers have all resolved.

NOT HERE: the plan's "start a pod of three if the tutor-pay floor is met".
Pods already go live at `match.cohort_min` = 3, and a tutor is paid per session
(rate x minutes / 50), not per learner, so pod size never changes what a tutor
earns. The floor is a margin question for pricing (E-03), not a matching rule.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from app.core import ethiopic_calendar as ec
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

WINDOW_DAYS = 14
SCORE = 50   # match_offer.score is 0-100; a refill isn't scored against rivals.


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def in_window(today: date) -> bool:
    """Ethiopic day 1-14 of months 1-12 (Pagume has no formation)."""
    e = ec.from_gregorian(today)
    return e.month != 13 and e.day <= WINDOW_DAYS


def _open_offer_counts() -> tuple[dict[str, int], set[str]]:
    rows = (_t("match_offer").select("cohort_id, student_id").eq("status", "offered").execute()).data or []
    per_pod: dict[str, int] = {}
    for r in rows:
        per_pod[r["cohort_id"]] = per_pod.get(r["cohort_id"], 0) + 1
    return per_pod, {r["student_id"] for r in rows}


def open_pods(cfg) -> list[dict]:
    """Active pods with an empty seat, each with its members' lessons."""
    from app.services.intake_service import _current_lesson_map
    # app.cohorts (tauka-flutter): level, seats, slot in the tutor's zone.
    pods = (_t("cohorts").select("id, name, tutor_id, level, max_students, slot_day_of_week, slot_time, "
                                 "slot_timezone, status")
            .eq("status", "active").execute()).data or []
    pods = [p for p in pods if p.get("level") is not None and p.get("slot_timezone")
            and p.get("slot_time") is not None and p.get("slot_day_of_week") is not None]
    if not pods:
        return []
    members = (_t("cohort_memberships").select("cohort_id, user_id")
               .in_("cohort_id", [p["id"] for p in pods]).eq("status", "active").execute()).data or []
    by_pod: dict[str, list[str]] = {}
    for m in members:
        by_pod.setdefault(m["cohort_id"], []).append(m["user_id"])
    lessons = _current_lesson_map(sorted({m["user_id"] for m in members})) if members else {}
    out = []
    for p in pods:
        ids = by_pod.get(p["id"], [])
        seats = int(p.get("max_students") or cfg.cohort_max) - len(ids)
        if seats > 0 and ids:
            out.append({**p, "open_seats": seats, "lessons": [lessons.get(i, 1) for i in ids]})
    return out


def candidates(holding: set[str]) -> list[dict]:
    """Waiting learners: wait pool, then confirmed seats. Each with level,
    lesson, chosen slots, and how many cycles they've waited."""
    from app.services.cohort_matcher import LEVEL_FROM_CEFR
    from app.services.group_service import pool_groups
    from app.services.intake_service import _current_lesson_map, _cycles_waited_map

    rows = (_t("enrolment_state").select("student_id, state")
            .in_("state", ["wait_pool", "seat_confirmed"]).execute()).data or []
    state = {}
    for r in rows:
        # wait_pool outranks seat_confirmed if a learner has both (two courses).
        if state.get(r["student_id"]) != "wait_pool":
            state[r["student_id"]] = r["state"]
    ids = sorted(s for s in state if s not in holding)
    if not ids:
        return []
    seated = {r["user_id"] for r in (_t("cohort_memberships").select("user_id")
              .in_("user_id", ids).eq("status", "active").execute()).data or []}
    paused = {r["student_id"] for r in (_t("enrolment_pause").select("student_id")
              .in_("student_id", ids).eq("status", "active").execute()).data or []}
    groups, private = pool_groups(ids)
    grouped = {sid for sid, (_g, size) in groups.items() if size >= 2} | set(private)
    ids = [i for i in ids if i not in seated | paused | grouped]
    if not ids:
        return []
    students = {r["id"]: r for r in (_t("student").select("id, assessed_level, tier")
                .in_("id", ids).is_("deleted_at", "null").execute()).data or []}
    choices: dict[str, list[dict]] = {}
    for c in (_t("student_slot_choice").select("student_id, slot_id, favourite")
              .in_("student_id", ids).execute()).data or []:
        choices.setdefault(c["student_id"], []).append(c)
    lessons = _current_lesson_map(ids)
    waited = _cycles_waited_map(ids)
    out = []
    for sid in ids:
        st = students.get(sid)
        if not st or (st.get("tier") or "free_tier") == "free_tier" or not choices.get(sid):
            continue
        level = LEVEL_FROM_CEFR.get((st.get("assessed_level") or "").upper())
        if level is None:
            continue
        out.append({"student_id": sid, "state": state[sid], "level": level, "lesson": lessons.get(sid, 1),
                    "choices": choices[sid], "waited": waited.get(sid, 0)})
    return out


def plan(pods: list[dict], people: list[dict], slots: dict[str, dict], cfg, now: datetime,
         offers_per_pod: dict[str, int]) -> list[tuple[str, dict]]:
    """Pure: which learner is offered which pod. Returns [(student_id, pod)]."""
    from app.services.slot_service import next_at

    def pod_at(p: dict) -> datetime:
        return next_at({"anchor_timezone": p["slot_timezone"], "day_of_week": p["slot_day_of_week"],
                        "start_time": p["slot_time"]}, now)

    order = sorted(people, key=lambda c: (c["state"] != "wait_pool", -c["waited"], c["student_id"]))
    taken: set[str] = set()
    out = []
    for p in sorted(pods, key=lambda p: p["id"]):
        room = p["open_seats"] + cfg.overbook_slack - offers_per_pod.get(p["id"], 0)
        if room <= 0:
            continue
        at = pod_at(p)
        median = sorted(p["lessons"])[len(p["lessons"]) // 2]
        fits = []
        for c in order:
            if c["student_id"] in taken or c["level"] != p["level"]:
                continue
            lessons = p["lessons"] + [c["lesson"]]
            if max(lessons) - min(lessons) > cfg.lesson_band:
                continue
            matching = [ch for ch in c["choices"] if ch["slot_id"] in slots
                        and next_at(slots[ch["slot_id"]], now) == at]
            if not matching:
                continue
            fav = any(ch.get("favourite") for ch in matching)
            fits.append((c["state"] != "wait_pool", -c["waited"], not fav, abs(c["lesson"] - median),
                         c["student_id"]))
        for *_k, sid in sorted(fits)[:room]:
            taken.add(sid)
            out.append((sid, p))
    return out


def _refill_cycle(today: date, cfg) -> str | None:
    rows = (_t("intake_cycle").select("id").eq("mode", "refill").eq("cycle_date", today.isoformat())
            .limit(1).execute()).data or []
    if rows:
        return rows[0]["id"]
    try:
        created = (_t("intake_cycle").insert({
            "cycle_date": today.isoformat(), "mode": "refill", "status": "offered",
            "config": cfg.as_dict(),
            "notes": "seat refills into live pods (plan 3.6, §112)",
        }).execute()).data or []
        return created[0]["id"] if created else None
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return _refill_cycle(today, cfg)
        raise


def close_resolved() -> int:
    """Close refill cycles whose offers have all been answered or expired.
    Whoever didn't take a seat goes back to the wait pool — still first in
    line. No deferral row: nothing about them was judged.

    Private-pod cycles (§128, plan 3.2) close the same way, and a private pod
    still `forming` then (someone declined or let it expire) is dissolved: its
    accepted members leave it and go back to the wait pool too."""
    from app.services import phase0_service
    closed = 0
    for c in (_t("intake_cycle").select("id, mode").in_("mode", ["refill", "private"]).eq("status", "offered")
              .execute()).data or []:
        offers = (_t("match_offer").select("student_id, status, cohort_id").eq("cycle_id", c["id"])
                  .execute()).data or []
        if any(o["status"] == "offered" for o in offers):
            continue
        if c.get("mode") == "private":
            for pod_id in {o["cohort_id"] for o in offers}:
                pod = (_t("cohorts").select("status").eq("id", pod_id).limit(1).execute()).data or []
                if pod and pod[0]["status"] == "forming":
                    seated = [m["user_id"] for m in (_t("cohort_memberships").select("user_id")
                              .eq("cohort_id", pod_id).eq("status", "active").execute()).data or []]
                    # CONFLICT-048: status, left_at, removal_reason only.
                    _t("cohort_memberships").update({
                        "status": "dropped", "left_at": _now().isoformat(),
                        "removal_reason": "private pod not formed: not every member accepted (plan 3.2)",
                    }).eq("cohort_id", pod_id).eq("status", "active").execute()
                    _t("cohorts").update({"status": "dissolved"}).eq("id", pod_id).eq("status", "forming").execute()
                    for sid in seated:
                        phase0_service.advance(sid, ("in_pod",), "wait_pool")
        for o in offers:
            if o["status"] != "accepted":
                phase0_service.advance(o["student_id"], ("matched",), "wait_pool")
        _t("intake_cycle").update({"status": "closed", "closed_at": _now().isoformat()}) \
            .eq("id", c["id"]).eq("status", "offered").execute()
        closed += 1
    return closed


def run(now: datetime | None = None) -> dict:
    """The daily refill: close finished refill cycles, then — inside the
    window — offer empty seats."""
    from app.services import phase0_service
    from app.services.intake_service import load_config
    from app.services.slot_service import catalogue

    now = now or _now()
    out = {"closed": close_resolved(), "offers": 0, "in_window": in_window(now.date())}
    if not out["in_window"]:
        return out
    cfg = load_config()
    pods = open_pods(cfg)
    if not pods:
        return out
    per_pod, holding = _open_offer_counts()
    people = candidates(holding)
    if not people:
        return out
    slots = {s["id"]: s for s in catalogue(None, now)}
    picks = plan(pods, people, slots, cfg, now, per_pod)
    if not picks:
        return out
    cycle_id = _refill_cycle(now.date(), cfg)
    if not cycle_id:
        return out
    expires = now + timedelta(hours=cfg.offer_ttl_hours)
    by_id = {c["student_id"]: c for c in people}
    for sid, p in picks:
        try:
            _t("match_offer").insert({
                "cycle_id": cycle_id, "student_id": sid, "cohort_id": p["id"], "tutor_id": p.get("tutor_id"),
                "rank": 1, "score": SCORE,
                "score_breakdown": {"kind": "refill", "summary": "An open seat in a running pod at your level and time"},
                "priority": by_id[sid]["state"] == "wait_pool",
                "status": "offered", "expires_at": expires.isoformat(),
            }).execute()
        except Exception as exc:  # noqa: BLE001
            # match_offer_unique (cycle, student, cohort): offered already today.
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                logger.error("refill offer for %s failed: %s", sid, exc)
            continue
        phase0_service.advance(sid, ("seat_confirmed", "wait_pool"), "matched")
        out["offers"] += 1
    return out
