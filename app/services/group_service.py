"""Friend groups and private pods (enrolment plan 3.2, migration §108).

Playbook paths 2 and 3:
  * FRIENDS — one person creates a group, the others join with its code; the
    matcher seats the group as one unit (2 or 3 seats) or not at all. Members
    at different levels are told up front (a pod has one level). A member who
    isn't ready yet holds the others back to the next formation they can all
    make — "the faster friend waits for the slower one". Leaving is always
    allowed; whoever stays is matched as before.
  * PRIVATE POD — up to four people who want their own pod. They are left out
    of the automatic matcher and listed for ops. Ops forms it (§128): a tutor
    and a catalogue slot -> one `forming` pod sized to the group and an
    ordinary 24-hour offer each, under a `private` cycle. It goes live when
    every member has accepted; a decline or an expiry dissolves it and the
    members go back to the wait pool (refill_service.close_resolved).

No discounts or premiums: prices are the pricing page's (E-03, plan 2.5).
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I, O, 0, 1
SIZES = {"friends": 3, "private_pod": 4}
READY_STATES = ("seat_confirmed", "wait_pool")


class GroupRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _active_membership(student_id: str) -> dict | None:
    rows = (_t("enrolment_group_member").select("group_id").eq("student_id", student_id)
            .is_("left_at", "null").limit(1).execute()).data or []
    return rows[0] if rows else None


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def create(student_id: str, kind: str, course_id: str) -> dict:
    if kind not in SIZES:
        raise GroupRefused("invalid_kind")
    if not (_t("student").select("id").eq("id", student_id).limit(1).execute()).data:
        raise GroupRefused("not_a_learner")
    if _active_membership(student_id):
        raise GroupRefused("already_in_group")
    for _ in range(5):
        try:
            made = _t("enrolment_group").insert({
                "kind": kind, "code": _new_code(), "course_id": course_id,
                "created_by": student_id, "max_size": SIZES[kind],
            }).execute()
            break
        except Exception as exc:  # noqa: BLE001
            if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
                raise
    else:
        raise GroupRefused("try_again")
    group = (made.data or [{}])[0]
    _t("enrolment_group_member").insert({"group_id": group["id"], "student_id": student_id}).execute()
    return mine(student_id)


def join(student_id: str, code: str) -> dict:
    code = (code or "").strip().upper()
    rows = (_t("enrolment_group").select("id, status").eq("code", code).limit(1).execute()).data or []
    if not rows:
        raise GroupRefused("no_such_group")
    if rows[0]["status"] != "open":
        raise GroupRefused("closed")
    if _active_membership(student_id):
        raise GroupRefused("already_in_group")
    try:
        existing = (_t("enrolment_group_member").select("student_id").eq("group_id", rows[0]["id"])
                    .eq("student_id", student_id).limit(1).execute()).data
        if existing:   # re-joining a group they left
            _t("enrolment_group_member").update({"left_at": None}).eq("group_id", rows[0]["id"]) \
                .eq("student_id", student_id).execute()
        else:
            _t("enrolment_group_member").insert({"group_id": rows[0]["id"], "student_id": student_id}).execute()
    except Exception as exc:  # noqa: BLE001
        if "full" in str(exc):
            raise GroupRefused("full") from exc
        if "not open" in str(exc):
            raise GroupRefused("closed") from exc
        raise
    return mine(student_id)


def leave(student_id: str) -> dict:
    m = _active_membership(student_id)
    if not m:
        raise GroupRefused("not_in_group")
    from datetime import datetime, timezone
    _t("enrolment_group_member").update({"left_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("group_id", m["group_id"]).eq("student_id", student_id).is_("left_at", "null").execute()
    left = (_t("enrolment_group_member").select("student_id").eq("group_id", m["group_id"])
            .is_("left_at", "null").execute()).data or []
    if not left:
        _t("enrolment_group").update({"status": "dissolved"}).eq("id", m["group_id"]).eq("status", "open").execute()
    return {"group": None}


def _members_view(group_id: str) -> list[dict]:
    from app.services.cohort_matcher import LEVEL_FROM_CEFR
    from app.services.makeup_service import _names
    ids = [r["student_id"] for r in (_t("enrolment_group_member").select("student_id")
           .eq("group_id", group_id).is_("left_at", "null").execute()).data or []]
    if not ids:
        return []
    levels = {r["id"]: LEVEL_FROM_CEFR.get((r.get("assessed_level") or "").upper())
              for r in (_t("student").select("id, assessed_level").in_("id", ids).execute()).data or []}
    states = {}
    for r in (_t("enrolment_state").select("student_id, state").in_("student_id", ids).execute()).data or []:
        states[r["student_id"]] = r["state"]
    names = _names(ids)
    return [{"student_id": i, "first_name": (names.get(i) or "A learner").split(" ")[0],
             "level": levels.get(i), "ready": states.get(i) in READY_STATES} for i in ids]


def _view(group: dict) -> dict:
    members = _members_view(group["id"])
    levels = {m["level"] for m in members if m["level"] is not None}
    return {
        "id": group["id"], "kind": group["kind"], "code": group["code"], "status": group["status"],
        "max_size": group["max_size"], "members": members,
        # A pod has one level: any difference means they can't be seated together.
        "level_gap": len(levels) > 1,
        "all_ready": bool(members) and all(m["ready"] for m in members),
    }


def mine(student_id: str) -> dict:
    m = _active_membership(student_id)
    if not m:
        return {"group": None}
    g = (_t("enrolment_group").select("*").eq("id", m["group_id"]).limit(1).execute()).data or []
    if not g:
        return {"group": None}
    view = _view(g[0])
    # Learners see first names and readiness — not each other's ids or levels.
    view["members"] = [{"first_name": mem["first_name"], "ready": mem["ready"],
                        "is_you": mem["student_id"] == student_id} for mem in view["members"]]
    return {"group": view}


def admin_list(actor: str) -> list[dict]:
    if not hs.admin_can(actor, "ops"):
        raise GroupRefused("not_allowed")
    rows = (_t("enrolment_group").select("*").eq("status", "open").order("created_at").execute()).data or []
    return [_view(g) for g in rows]


def pool_groups(student_ids: list[str]) -> tuple[dict[str, tuple[str, int]], set[str]]:
    """For the matcher: {student: (group_id, active size)} for open friend
    groups, and the set of private-pod members to leave out of the pool."""
    if not student_ids:
        return {}, set()
    mem = (_t("enrolment_group_member").select("group_id, student_id").in_("student_id", student_ids)
           .is_("left_at", "null").execute()).data or []
    if not mem:
        return {}, set()
    gids = sorted({m["group_id"] for m in mem})
    groups = {g["id"]: g for g in (_t("enrolment_group").select("id, kind, status").in_("id", gids)
                                   .execute()).data or []}
    sizes: dict[str, int] = {}
    for r in (_t("enrolment_group_member").select("group_id").in_("group_id", gids)
              .is_("left_at", "null").execute()).data or []:
        sizes[r["group_id"]] = sizes.get(r["group_id"], 0) + 1
    friends, private = {}, set()
    for m in mem:
        g = groups.get(m["group_id"])
        if not g or g["status"] != "open":
            continue
        if g["kind"] == "private_pod":
            private.add(m["student_id"])
        elif sizes.get(g["id"], 0) > 1:   # a group of one is just a single
            friends[m["student_id"]] = (g["id"], sizes[g["id"]])
    return friends, private


# ── forming a private pod (plan 3.2, §128) ─────────────────────────────────

async def form_private_pod(actor: str, group_id: str, tutor_id: str, slot_id: str,
                           now: datetime | None = None) -> dict:
    """Ops: one pod for the group, at a catalogue slot, with this tutor."""
    from app.services import phase0_service
    from app.services.cohort_matcher import CADENCE_CLASS_FOR_TIER, SESSIONS_PER_MONTH
    from app.services.intake_service import DAY_NAMES, _course_for, load_config

    if not hs.admin_can(actor, "ops"):
        raise GroupRefused("not_allowed")
    now = now or datetime.now(timezone.utc)
    g = (_t("enrolment_group").select("*").eq("id", group_id).limit(1).execute()).data or []
    if not g:
        raise GroupRefused("no_such_group")
    g = g[0]
    if g["kind"] != "private_pod" or g["status"] != "open":
        raise GroupRefused("not_formable")
    view = _view(g)
    members = view["members"]
    if len(members) < 2:
        raise GroupRefused("too_few")
    if not view["all_ready"]:
        raise GroupRefused("not_ready")
    if view["level_gap"] or any(m["level"] is None for m in members):
        raise GroupRefused("level_gap")
    ids = [m["student_id"] for m in members]
    tiers = {r["id"]: r.get("tier") for r in (_t("student").select("id, tier").in_("id", ids).execute()).data or []}
    cadences = {CADENCE_CLASS_FOR_TIER.get(tiers.get(i)) for i in ids}
    if len(cadences) != 1 or None in cadences:
        raise GroupRefused("mixed_plans")
    cadence = cadences.pop()
    tutor = (_t("tutor").select("id, status, deleted_at").eq("id", tutor_id).limit(1).execute()).data or []
    if not tutor or tutor[0].get("status") != "active" or tutor[0].get("deleted_at"):
        raise GroupRefused("tutor_inactive")
    slot = (_t("slot_catalogue").select("id, band, anchor_timezone, day_of_week, start_time, active")
            .eq("id", slot_id).limit(1).execute()).data or []
    if not slot or not slot[0].get("active"):
        raise GroupRefused("no_such_slot")
    slot = slot[0]
    level = members[0]["level"]
    cfg = load_config()
    hhmm = str(slot["start_time"])[:5]
    cycle = (_t("intake_cycle").insert({
        "cycle_date": now.date().isoformat(), "mode": "private", "status": "offered", "config": cfg.as_dict(),
        "cohorts_formed": 1, "notes": f"private pod for group {group_id} (plan 3.2, §128)",
    }).execute()).data[0]
    pod = (_t("cohorts").insert({
        # CONFLICT-048: tauka-python inserts pods at formation.
        "name": f"Private · Level {level} · {DAY_NAMES[int(slot['day_of_week'])]} {hhmm} ({slot['anchor_timezone']})",
        "tutor_id": tutor_id, "course_id": _course_for(ids), "level": level, "cadence_class": cadence,
        "sessions_per_month": SESSIONS_PER_MONTH[cadence], "slot_day_of_week": int(slot["day_of_week"]),
        "slot_time": f"{hhmm}:00", "slot_timezone": slot["anchor_timezone"], "status": "forming",
        # Sized to the group: it goes live when every member has accepted.
        "max_students": len(ids), "intake_cycle": now.date().isoformat(), "start_date": now.date().isoformat(),
    }).execute()).data[0]
    expires = now + timedelta(hours=cfg.offer_ttl_hours)
    for sid in ids:
        _t("match_offer").insert({
            "cycle_id": cycle["id"], "student_id": sid, "cohort_id": pod["id"], "tutor_id": tutor_id, "rank": 1,
            "score": 100, "score_breakdown": {"kind": "private_pod", "summary": "Your private pod"},
            "priority": True, "status": "offered", "expires_at": expires.isoformat(),
        }).execute()
        phase0_service.advance(sid, ("seat_confirmed", "wait_pool"), "matched")
    _t("enrolment_group").update({"status": "matched"}).eq("id", group_id).eq("status", "open").execute()
    return {"cohort_id": pod["id"], "cycle_id": cycle["id"], "offered": len(ids)}
