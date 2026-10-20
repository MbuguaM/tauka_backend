"""Offers, holds, acceptance and the cycle sweep — plan §3 Stage 5 / Stage 7.

An offer is a **soft hold with a deadline**, not a notification. Plan §3
Stage 5: presenting 2-3 options and letting the student choose oversells seats
when taken literally — offer one 4-seat cohort to nine people and five are told
"no longer available" after being promised a place. So:

  * a cohort accepts at most `capacity + overbook_slack` holds;
  * **accept** converts soft -> confirmed, first come first served;
  * when a cohort reaches capacity its remaining holds are invalidated and
    **those students are re-offered immediately from their remaining
    candidates** — the difference between a queue and a disappointment;
  * **expiry** at `offer_ttl` releases the seat and triggers the same
    immediate re-offer;
  * a cohort becomes `active` at `cohort_min` confirmed members; a `forming`
    cohort that has not reached it by cycle close dissolves, and its members
    return to the pool with `cycles_waited + 1`.

WHY ACCEPTANCE IS AN API CALL AND NOT AN UPDATE. There is deliberately no
client write policy on `app.match_offer` (§86.4). Accepting has to check live
capacity, invalidate the student's other holds, create the teaching link through
the rules `matching_service` owns, and re-offer the seats it releases. A client
that could write `status` would skip all four, and the first symptom would be a
five-member cohort.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.services import matching_service
from app.services.intake_service import DAY_NAMES, load_config
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

OPEN_OFFER = "offered"


# ── Reading a student's offers ───────────────────────────────────────────────

async def list_offers(student_id: str) -> dict:
    """The offer screen's payload.

    ASSEMBLED HERE RATHER THAN READ BY THE CLIENT, for the reason
    `matching_service._tutor_display` already records: a student is entitled to
    their prospective tutor's name and face, not their phone number, and RLS is
    row-level so a policy granting SELECT hands over the whole row. The same
    applies to the cohort — `app.can_read_cohort()` covers members, and a
    student holding an offer is not a member yet.

    Cohort-mates are ANONYMISED to a count and a level, exactly as plan §3
    Stage 5 specifies: "2 other students at your level". Naming three strangers
    to someone who has not joined would be a disclosure the enrolment consent
    basis does not cover (`legal.md`).
    """
    try:
        offers = db.schema("app").table("match_offer").select(
            "id, cohort_id, tutor_id, rank, score, score_breakdown, priority, "
            "status, expires_at, created_at"
        ).eq("student_id", student_id).eq("status", OPEN_OFFER).order("rank").execute()
        rows = offers.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("offer read failed for %s: %s", student_id, exc)
        return {"offers": [], "deferral": None}

    if not rows:
        return {"offers": [], "deferral": await latest_deferral(student_id)}

    cohort_ids = sorted({r["cohort_id"] for r in rows})
    tutor_ids = sorted({r["tutor_id"] for r in rows if r.get("tutor_id")})

    try:
        cohorts = db.schema("app").table("cohorts").select(
            "id, level, cadence_class, sessions_per_month, slot_day_of_week, "
            "slot_time, slot_timezone, max_students, status, band_lesson_low, "
            "band_lesson_high"
        ).in_("id", cohort_ids).execute()
        cohort_of = {c["id"]: c for c in cohorts.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.error("cohort read failed: %s", exc)
        cohort_of = {}

    display = matching_service._tutor_display(tutor_ids)  # noqa: SLF001
    try:
        profiles = db.schema("app").table("tutor_teaching_profile").select(
            "tutor_id, specialisations, teaching_style, years_experience, intro_clip_path"
        ).in_("tutor_id", tutor_ids).execute()
        teaching_of = {p["tutor_id"]: p for p in profiles.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("teaching profile read failed: %s", exc)
        teaching_of = {}

    confirmed = _confirmed_counts(cohort_ids)

    out = []
    for r in rows:
        cohort = cohort_of.get(r["cohort_id"], {})
        tutor = {**display.get(r["tutor_id"], {}), **teaching_of.get(r["tutor_id"], {})}
        breakdown = r.get("score_breakdown") or {}
        seats = cohort.get("max_students") or 4
        peers = confirmed.get(r["cohort_id"], 0)
        out.append({
            "offer_id": r["id"],
            "cohort_id": r["cohort_id"],
            "rank": r["rank"],
            "score": r["score"],
            "priority": r["priority"],
            "expires_at": r["expires_at"],
            "summary": breakdown.get("summary"),
            "level": cohort.get("level"),
            "cadence_class": cohort.get("cadence_class"),
            "sessions_per_month": cohort.get("sessions_per_month"),
            # Rendered in the STUDENT's own timezone by the client from these
            # three fields. Sending a formatted string would bake the cohort's
            # zone into someone else's evening.
            "slot_day_of_week": cohort.get("slot_day_of_week"),
            "slot_time": cohort.get("slot_time"),
            "slot_timezone": cohort.get("slot_timezone"),
            "slot_label": _slot_label(cohort),
            "seats": seats,
            "confirmed_members": peers,
            "cohort_mates": _mates_label(peers, cohort.get("level")),
            "tutor": {
                "id": r.get("tutor_id"),
                "name": tutor.get("name") or " ".join(
                    p for p in (tutor.get("first_name"), tutor.get("last_name")) if p
                ) or None,
                "avatar_url": tutor.get("avatar_url"),
                "bio": tutor.get("bio"),
                "rating": tutor.get("rating"),
                "specialisations": tutor.get("specialisations") or [],
                "teaching_style": tutor.get("teaching_style"),
                "years_experience": tutor.get("years_experience"),
                "intro_clip_path": tutor.get("intro_clip_path"),
            },
        })
    return {"offers": out, "deferral": None}


def _slot_label(cohort: dict) -> str | None:
    dow = cohort.get("slot_day_of_week")
    if dow is None or not cohort.get("slot_time"):
        return None
    return f"{DAY_NAMES[dow]} {str(cohort['slot_time'])[:5]} ({cohort.get('slot_timezone')})"


def _mates_label(confirmed: int, level: int | None) -> str:
    if confirmed <= 0:
        return "You would be the first to join"
    noun = "student" if confirmed == 1 else "students"
    at_level = f" at level {level}" if level else ""
    return f"{confirmed} other {noun}{at_level}"


def _confirmed_counts(cohort_ids: list[str]) -> dict[str, int]:
    if not cohort_ids:
        return {}
    try:
        rows = db.schema("app").table("cohort_memberships").select(
            "cohort_id, status").in_("cohort_id", cohort_ids).eq(
            "status", "active").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("membership count failed: %s", exc)
        return {}
    out: dict[str, int] = {}
    for r in rows.data or []:
        out[r["cohort_id"]] = out.get(r["cohort_id"], 0) + 1
    return out


async def latest_deferral(student_id: str) -> dict | None:
    """What the student is told when they have no offers.

    Plan §4b Stage 1: they get the next cycle date and "a plain-language reason
    from the deferral enum, not 'no match found'". The reason code travels with
    the sentence so the client can act on it — TOO_FEW_PREFERENCES is a link to
    a form, NO_TUTOR_FOR_LEVEL is not.
    """
    try:
        rows = db.schema("app").table("deferred_student").select(
            "reason, detail, cycles_waited, bands_relaxed, created_at"
        ).eq("student_id", student_id).order("created_at", desc=True).limit(1).execute()
        row = (rows.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("deferral read failed for %s: %s", student_id, exc)
        return None
    if not row:
        return None
    return {**row, "message": DEFERRAL_MESSAGES.get(
        row["reason"], "We could not place you this time. We will try again next cycle.")}


# The student-facing half of the deferral enum. Written to be true rather than
# soothing: a student who is told "no match found" learns nothing, and a student
# told the wrong reason acts on it.
DEFERRAL_MESSAGES = {
    "NO_TIMEZONE": "We need your timezone before we can find a group that meets at a reasonable hour for you.",
    "TOO_FEW_PREFERENCES": "Pick a few more times you could make — we need more to find you a group.",
    "NO_LEVEL": "Take the placement test and we can match you with students at your level.",
    "NO_TUTOR_FOR_LEVEL": "We do not yet have a tutor certified for your level. We are recruiting.",
    "NO_SLOT_OVERLAP": "None of your available times line up with a tutor's schedule yet.",
    "TZ_ISOLATE": "There are students at your level, but none close enough to your timezone for a sensible shared time.",
    "TOO_FEW_PEERS_AT_LEVEL": "We need a couple more students at your level before a group can open.",
    "TUTORS_AT_CAPACITY": "Our tutors are all at capacity. We are recruiting, and you are near the front of the queue.",
    "DECLINED_ALL_OFFERS": "You turned down this round's groups. We will look again next cycle.",
    "COHORT_DID_NOT_FILL": "Your group did not reach three members in time. You are first in line next cycle.",
    "OFFERS_EXPIRED": "Your offers ran out of time. We will send new ones next cycle.",
    "GROUP_WAITING": "You're waiting for a friend in your group to finish Phase 0. You'll be matched together at the next formation they make.",
    "GROUP_LEVEL_GAP": "You and your friends placed at different levels, and a pod has one level. Catch up together, or leave the group to be matched on your own.",
}


# ── Accepting and declining ──────────────────────────────────────────────────

async def accept(student_id: str, offer_id: str) -> dict:
    """Convert a soft hold into a seat.

    FIRST COME FIRST SERVED, AND CAPACITY IS CHECKED AGAINST CONFIRMED MEMBERS
    RATHER THAN HOLDS. Holds are deliberately overbooked; seats are not. The
    unique partial index `uq_match_offer_one_accept` is the guard that survives
    two accepts racing, and it is a guard rather than the check because an index
    violation is a worse error message than a refusal.
    """
    cfg = load_config()
    try:
        rows = db.schema("app").table("match_offer").select("*").eq(
            "id", offer_id).eq("student_id", student_id).limit(1).execute()
        offer = (rows.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.error("offer lookup failed: %s", exc)
        return {"accepted": False, "reason": "lookup_failed"}

    if not offer:
        return {"accepted": False, "reason": "not_found"}
    if offer["status"] != OPEN_OFFER:
        return {"accepted": False, "reason": f"offer is {offer['status']}"}
    if _expired(offer["expires_at"]):
        await expire_due()
        return {"accepted": False, "reason": "expired"}

    cohort_id = offer["cohort_id"]
    try:
        cohorts = db.schema("app").table("cohorts").select(
            "id, status, max_students, tutor_id, band_lesson_low, band_lesson_high"
        ).eq("id", cohort_id).limit(1).execute()
        cohort = (cohorts.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.error("cohort lookup failed: %s", exc)
        return {"accepted": False, "reason": "lookup_failed"}

    if not cohort or cohort["status"] in ("dissolved", "completed"):
        await _supersede(offer_id, "cohort is no longer forming")
        return {"accepted": False, "reason": "cohort_unavailable"}

    seats = cohort.get("max_students") or cfg.cohort_max
    taken = _confirmed_counts([cohort_id]).get(cohort_id, 0)
    if taken >= seats:
        await _fill_and_reoffer(cohort_id)
        return {"accepted": False, "reason": "full"}

    # The membership. `joined_at_lesson` is recorded because a mid-level joiner
    # is not at lesson 1, and the drift monitor needs the starting point.
    lesson = _current_lesson(student_id) or cohort.get("band_lesson_low") or 1
    try:
        db.schema("app").table("cohort_memberships").insert({
            "cohort_id": cohort_id,
            "user_id": student_id,
            "status": "active",
            "joined_at_lesson": lesson,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            logger.info("membership already exists for %s in %s", student_id, cohort_id)
        else:
            logger.error("membership insert failed: %s", exc)
            return {"accepted": False, "reason": "membership_failed"}

    now = datetime.now(timezone.utc).isoformat()
    try:
        db.schema("app").table("match_offer").update({
            "status": "accepted", "responded_at": now,
        }).eq("id", offer_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("offer accept update failed: %s", exc)

    # The enrolment record follows the seat (§102): matched -> in_pod.
    from app.services import phase0_service
    phase0_service.advance(student_id, ("matched",), "in_pod")

    # The student's other holds are released the moment one is taken — that is
    # what returns the seats to the people waiting behind them.
    released = await _release_other_holds(student_id, offer["cycle_id"], offer_id)
    # A friend group is seated together (plan 3.2, FC-041): once one friend takes
    # this pod, the others' offers for OTHER pods in this cycle go.
    released += await _align_group(student_id, offer["cycle_id"], cohort_id)

    link = await _ensure_teaching_link(student_id, cohort.get("tutor_id"))
    activated = await _activate_if_viable(cohort_id, cfg)

    return {
        "accepted": True,
        "cohort_id": cohort_id,
        "cohort_status": "active" if activated else "forming",
        "confirmed_members": taken + 1,
        "seats": seats,
        "released_holds": released,
        "teaching_link": link,
    }


async def decline(student_id: str, offer_id: str) -> dict:
    """Say no to one offer. The others stand.

    Declining is NOT deferring: a student with a rank-2 offer still has one, and
    only the cycle sweep decides that someone has run out of options. Keeping
    those two apart is what makes DECLINED_ALL_OFFERS a real scoring signal
    rather than a bucket.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = db.schema("app").table("match_offer").update({
            "status": "declined", "responded_at": now,
        }).eq("id", offer_id).eq("student_id", student_id).eq(
            "status", OPEN_OFFER).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("decline failed: %s", exc)
        return {"declined": False, "reason": "update_failed"}

    if not (result.data or []):
        return {"declined": False, "reason": "not_open"}

    cohort_id = result.data[0]["cohort_id"]
    await _fill_and_reoffer(cohort_id)
    remaining = _open_offer_count(student_id)
    return {"declined": True, "offers_remaining": remaining}


# ── Holds, releases and re-offers ────────────────────────────────────────────

def _expired(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except ValueError:
        return False


def _open_offer_count(student_id: str) -> int:
    try:
        rows = db.schema("app").table("match_offer").select("id").eq(
            "student_id", student_id).eq("status", OPEN_OFFER).execute()
        return len(rows.data or [])
    except Exception:  # noqa: BLE001
        return 0


def _current_lesson(student_id: str) -> int | None:
    from app.services.intake_service import _current_lesson_map  # local: avoids a cycle
    return _current_lesson_map([student_id]).get(student_id)


async def _supersede(offer_id: str, reason: str) -> None:
    try:
        db.schema("app").table("match_offer").update({
            "status": "superseded",
            "responded_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", offer_id).eq("status", OPEN_OFFER).execute()
        logger.info("offer %s superseded: %s", offer_id, reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("supersede failed: %s", exc)


async def _release_other_holds(student_id: str, cycle_id: str, keep_id: str) -> int:
    try:
        rows = db.schema("app").table("match_offer").select("id, cohort_id").eq(
            "student_id", student_id).eq("cycle_id", cycle_id).eq(
            "status", OPEN_OFFER).execute()
        others = [r for r in rows.data or [] if r["id"] != keep_id]
    except Exception as exc:  # noqa: BLE001
        logger.error("hold release read failed: %s", exc)
        return 0

    for r in others:
        await _supersede(r["id"], "student accepted elsewhere")
    # Every cohort that just got a seat back may now have room for someone whose
    # hold was invalidated when it looked full.
    for cohort_id in sorted({r["cohort_id"] for r in others}):
        await _promote_waiting(cohort_id)
    return len(others)


async def _align_group(student_id: str, cycle_id: str, cohort_id: str) -> int:
    """Withdraw the student's friends' open offers for other cohorts in this
    cycle. Their offer for THIS cohort stays — the matcher only ever offers a
    group seats in the same candidate, so each friend holds one."""
    try:
        mine = db.schema("app").table("enrolment_group_member").select("group_id").eq(
            "student_id", student_id).is_("left_at", "null").limit(1).execute().data or []
        if not mine:
            return 0
        friends = [r["student_id"] for r in (db.schema("app").table("enrolment_group_member")
                   .select("student_id").eq("group_id", mine[0]["group_id"]).is_("left_at", "null")
                   .execute().data or []) if r["student_id"] != student_id]
        if not friends:
            return 0
        rows = db.schema("app").table("match_offer").select("id, cohort_id").in_(
            "student_id", friends).eq("cycle_id", cycle_id).eq("status", OPEN_OFFER).execute().data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("group alignment read failed: %s", exc)
        return 0
    other = [r for r in rows if r["cohort_id"] != cohort_id]
    for r in other:
        await _supersede(r["id"], "a friend in your group accepted a different pod")
    for cid in sorted({r["cohort_id"] for r in other}):
        await _promote_waiting(cid)
    return len(other)


async def _fill_and_reoffer(cohort_id: str) -> None:
    """When a cohort reaches capacity, invalidate its surplus holds and re-offer
    those students immediately from their remaining candidates.

    "Their remaining candidates" are their other live offers — ranks 2 and 3
    from the same cycle. Promoting one to rank 1 and extending its deadline is
    the immediate re-offer plan §3 Stage 5 asks for; it does not re-run the
    matcher, because the alternatives were already computed under the frozen
    pool and recomputing against a pool that has since changed would produce an
    offer the cycle cannot explain.
    """
    cfg = load_config()
    try:
        cohorts = db.schema("app").table("cohorts").select(
            "id, max_students, status").eq("id", cohort_id).limit(1).execute()
        cohort = (cohorts.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.error("cohort read failed: %s", exc)
        return
    if not cohort:
        return

    seats = cohort.get("max_students") or cfg.cohort_max
    taken = _confirmed_counts([cohort_id]).get(cohort_id, 0)
    if taken < seats:
        return

    try:
        rows = db.schema("app").table("match_offer").select(
            "id, student_id").eq("cohort_id", cohort_id).eq(
            "status", OPEN_OFFER).execute()
        surplus = rows.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("surplus hold read failed: %s", exc)
        return

    for r in surplus:
        await _supersede(r["id"], "cohort reached capacity")
        await _promote_next(r["student_id"], cfg)


async def _promote_next(student_id: str, cfg) -> None:
    """Move the student's best remaining offer to rank 1 and reset its clock.

    The clock reset is the point. A rank-2 offer created 47 hours ago is not a
    replacement for the rank-1 that was just taken away — the student would have
    an hour to decide about a group they were never really shown.
    """
    try:
        rows = db.schema("app").table("match_offer").select(
            "id, rank, score").eq("student_id", student_id).eq(
            "status", OPEN_OFFER).order("rank").limit(1).execute()
        nxt = (rows.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.error("promotion read failed: %s", exc)
        return
    if not nxt:
        return
    expires = datetime.now(timezone.utc) + timedelta(hours=cfg.offer_ttl_hours)
    try:
        db.schema("app").table("match_offer").update({
            "rank": 1, "expires_at": expires.isoformat(),
        }).eq("id", nxt["id"]).execute()
        logger.info("promoted offer %s to rank 1 for %s", nxt["id"], student_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("promotion failed: %s", exc)


async def _promote_waiting(cohort_id: str) -> None:
    """A seat came back. Anyone still holding an offer on this cohort keeps it;
    there is nothing to do but say so — the hold was never revoked, and the
    student's clock is already running."""
    logger.info("cohort %s has a seat free again", cohort_id)


async def _ensure_teaching_link(student_id: str, tutor_id: str | None) -> dict | None:
    """Joining a cohort creates the teaching relation, through the rules that
    already exist.

    `matching_service.create_link` is called UNCHANGED (O-16): it applies the
    commitment lock, the concentration cap and the home/guest constraint, and
    refuses with a reason rather than raising. Home if they have no home tutor,
    guest otherwise — a student's second cohort must not silently replace the
    tutor a term commitment protects.
    """
    if not tutor_id:
        return None
    try:
        existing = db.schema("app").table("student_tutor_link").select(
            "id, role").eq("student_id", student_id).eq(
            "state", "active").execute()
        roles = {r["role"] for r in existing.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("link read failed: %s", exc)
        roles = set()

    role = "guest" if "home" in roles else "home"
    try:
        return await matching_service.create_link(
            student_id, tutor_id, role, source="matched")
    except Exception as exc:  # noqa: BLE001
        # A refused link must not undo an accepted seat. The student is in the
        # cohort; an admin can fix the link, and the roster reports it as a
        # problem in the meantime (`validate_link_set`).
        logger.error("teaching link failed for %s/%s: %s", student_id, tutor_id, exc)
        return {"created": False, "blocking": [str(exc)]}


async def _activate_if_viable(cohort_id: str, cfg) -> bool:
    """A cohort becomes `active` at `cohort_min` confirmed members, and that is
    when its calendar is generated.

    `app.generate_cohort_sessions` is idempotent on `(cohort_id, scheduled_at)`,
    so the fourth member arriving does not duplicate the series — which is why
    it is safe to call on every acceptance rather than exactly once.
    """
    taken = _confirmed_counts([cohort_id]).get(cohort_id, 0)
    # A private pod (plan 3.2, §128) is sized to its group, which may be under
    # cohort_min: it goes live when it is full, i.e. every member accepted.
    need = cfg.cohort_min
    try:
        size = (db.schema("app").table("cohorts").select("max_students").eq("id", cohort_id)
                .limit(1).execute()).data or []
        if size and size[0].get("max_students"):
            need = min(need, int(size[0]["max_students"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohort size read failed: %s", exc)
    if taken < need:
        return False
    try:
        db.schema("app").table("cohorts").update({"status": "active"}).eq(
            "id", cohort_id).eq("status", "forming").execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("cohort activation failed: %s", exc)
        return False
    try:
        db.schema("app").rpc("generate_cohort_sessions",
                             {"p_cohort": cohort_id, "p_months": 2}).execute()
    except Exception as exc:  # noqa: BLE001
        # The cohort is real and active either way; a missing calendar is
        # recoverable by re-running the generator, an un-activated cohort is not
        # visible to the people in it.
        logger.error("session generation failed for %s: %s", cohort_id, exc)
    return True


# ── Sweeps ───────────────────────────────────────────────────────────────────

async def expire_due() -> dict:
    """Expire holds past their deadline and re-offer from what remains.

    Runs from `/tasks/daily`. The TTL is 24 h (E-13, §102) and a formation's
    matching window is days long, so a daily sweep is enough for the deadline
    to mean something without a second cron entry the hosting plan does not
    have. (The hold can outlive 24 h by up to a day; nobody loses a seat to that.)
    """
    now = datetime.now(timezone.utc).isoformat()
    cfg = load_config()
    try:
        rows = db.schema("app").table("match_offer").select(
            "id, student_id, cohort_id").eq("status", OPEN_OFFER).lt(
            "expires_at", now).execute()
        due = rows.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("expiry read failed: %s", exc)
        return {"expired": 0}

    for r in due:
        try:
            db.schema("app").table("match_offer").update({
                "status": "expired"}).eq("id", r["id"]).eq(
                "status", OPEN_OFFER).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("expiry update failed for %s: %s", r["id"], exc)
            continue
        await _promote_next(r["student_id"], cfg)

    if due:
        logger.info("Expired %s offer hold(s)", len(due))
    return {"expired": len(due)}


async def close_cycle(cycle_id: str | None = None) -> dict:
    """Dissolve cohorts that never reached three, defer everyone left over.

    Plan §3 Stage 5 and Stage 7: "`forming` cohorts that do not reach 3 by cycle
    close dissolve and their members return to the pool with
    `cycles_waited + 1`". Their members did nothing wrong, which is why they are
    deferred as COHORT_DID_NOT_FILL rather than swept in with the people who
    declined.
    """
    cfg = load_config()
    try:
        query = db.schema("app").table("intake_cycle").select(
            "id, cycle_date, status, intake_id").eq("mode", "live").eq("status", "offered")
        if cycle_id:
            query = query.eq("id", cycle_id)
        cycles = query.order("cycle_date").execute()
        rows = cycles.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("cycle read failed: %s", exc)
        return {"closed": 0}

    closed = 0
    for cycle in rows:
        # Only close a cycle whose holds have all resolved: a live hold is a
        # student still deciding, and closing under them would defer someone who
        # was about to accept.
        try:
            live = db.schema("app").table("match_offer").select("id").eq(
                "cycle_id", cycle["id"]).eq("status", OPEN_OFFER).limit(1).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("live hold check failed: %s", exc)
            continue
        if live.data:
            continue

        dissolved, deferred = await _sweep_cycle(cycle["id"], cfg)
        try:
            db.schema("app").table("intake_cycle").update({
                "status": "closed",
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", cycle["id"]).execute()
            closed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("cycle close failed: %s", exc)
        # A calendar run's formation is done once its cycle closes (§102).
        if cycle.get("intake_id"):
            try:
                db.schema("app").table("intake").update({"status": "formed"}).eq(
                    "id", cycle["intake_id"]).eq("status", "matching").execute()
            except Exception as exc:  # noqa: BLE001
                logger.error("intake status update failed: %s", exc)
        logger.info("Closed cycle %s: dissolved=%s deferred=%s",
                    cycle["id"], dissolved, deferred)
    return {"closed": closed}


async def _sweep_cycle(cycle_id: str, cfg) -> tuple[int, int]:
    # SCOPED TO THIS CYCLE'S OWN COHORTS. Sweeping every `forming` cohort on the
    # platform would dissolve a cohort another cycle is still filling — and
    # because a cycle only closes once its holds have all resolved, the two
    # cycles are not even in the same phase. The scope is the set of cohorts
    # this cycle's offers point at, which is exactly the set it created.
    try:
        offered = db.schema("app").table("match_offer").select(
            "cohort_id").eq("cycle_id", cycle_id).execute()
        cohort_ids = sorted({r["cohort_id"] for r in offered.data or []})
    except Exception as exc:  # noqa: BLE001
        logger.error("cycle cohort read failed: %s", exc)
        cohort_ids = []

    forming: list[str] = []
    if cohort_ids:
        try:
            cohorts = db.schema("app").table("cohorts").select(
                "id, status").in_("id", cohort_ids).eq("status", "forming").execute()
            forming = [c["id"] for c in cohorts.data or []]
        except Exception as exc:  # noqa: BLE001
            logger.error("forming cohort read failed: %s", exc)
            forming = []

    counts = _confirmed_counts(forming)
    dissolved = 0
    stranded: set[str] = set()

    for cohort_id in forming:
        if counts.get(cohort_id, 0) >= cfg.cohort_min:
            continue
        try:
            members = db.schema("app").table("cohort_memberships").select(
                "user_id").eq("cohort_id", cohort_id).eq("status", "active").execute()
            stranded |= {m["user_id"] for m in members.data or []}
            db.schema("app").table("cohort_memberships").update({
                "status": "dropped",
                "left_at": datetime.now(timezone.utc).isoformat(),
                "removal_reason": "cohort dissolved: fewer than "
                                  f"{cfg.cohort_min} members at cycle close",
            }).eq("cohort_id", cohort_id).eq("status", "active").execute()
            db.schema("app").table("cohorts").update({
                "status": "dissolved"}).eq("id", cohort_id).execute()
            dissolved += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("dissolve failed for %s: %s", cohort_id, exc)

    # Everyone who was offered and is not now seated.
    try:
        offers = db.schema("app").table("match_offer").select(
            "student_id, status").eq("cycle_id", cycle_id).execute()
        by_student: dict[str, set[str]] = {}
        for r in offers.data or []:
            by_student.setdefault(r["student_id"], set()).add(r["status"])
    except Exception as exc:  # noqa: BLE001
        logger.error("offer sweep read failed: %s", exc)
        by_student = {}

    from app.services import phase0_service
    deferred = 0
    for student_id, statuses in sorted(by_student.items()):
        if "accepted" in statuses and student_id not in stranded:
            continue
        # No seat at the end of the cycle: the wait pool, first in line next time
        # (§102, plan 3.7). A dissolved pod's members were already in_pod.
        phase0_service.advance(student_id, ("matched", "in_pod"), "wait_pool")
        if student_id in stranded:
            reason = "COHORT_DID_NOT_FILL"
        elif "declined" in statuses:
            # A real signal: the offers were feasible and unattractive.
            reason = "DECLINED_ALL_OFFERS"
        else:
            # Silence is not a decline, and must never be filed as one.
            reason = "OFFERS_EXPIRED"
        if await _defer(cycle_id, student_id, reason):
            deferred += 1
    return dissolved, deferred


async def _defer(cycle_id: str, student_id: str, reason: str) -> bool:
    from app.services.intake_service import _cycles_waited_map
    waited = _cycles_waited_map([student_id]).get(student_id, 0)
    try:
        db.schema("app").table("deferred_student").insert({
            "cycle_id": cycle_id,
            "student_id": student_id,
            "reason": reason,
            "detail": {},
            "cycles_waited": waited,
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        # The unique index on (cycle_id, student_id): they were already deferred
        # by the matcher itself. Not an error.
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return False
        logger.error("deferral insert failed for %s: %s", student_id, exc)
        return False
