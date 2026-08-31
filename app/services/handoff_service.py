"""Warm handoff — plan Phase 6, spec §1.7 / §2.5.

When a home tutor becomes unavailable, the student does not discover a different
name on their next booking. §1.7: *"a distinct state machine
(`active → handoff_pending → reassigned`) rather than an instant swap, so the
app layer can surface the transition to the student appropriately"*.

NO MIGRATION BACKS THIS MODULE, AND THAT IS THE POINT.

`app.student_tutor_link` (§73.1) already carries every column §1.7 asks for:

  state            CHECK (state IN ('active','handoff_pending','reassigned','ended'))
  replaced_link_id uuid REFERENCES app.student_tutor_link(id)
  ended_at         + student_tutor_link_ended_state

Phase 2 wrote those deliberately — "`handoff_pending` exists so the app can
surface the transition to the student rather than swapping a tutor out
silently". Phase 6 is the code that drives them.

THE THREE STATES, AND WHAT IS TRUE IN EACH.

  active           the ordinary relationship
  handoff_pending  the tutor is leaving; **they are still the student's tutor**
                   and still teach until the intro happens. `ended_at` is NULL,
                   which the CHECK constraint enforces.
  reassigned       closed, `ended_at` set, and `replaced_link_id` on the NEW row
                   points back here so the chain survives

The middle state is the whole feature. A handoff that went straight from active
to reassigned would be the silent swap, with a state machine drawn around it.

WHY THE UNIQUE INDEX LETS THIS WORK.

`idx_student_tutor_link_one_home` is UNIQUE on `(student_id)` WHERE
`role = 'home' AND state = 'active'`. `handoff_pending` is not `active`, so a
pending outgoing home link does not occupy the index and the incoming one can be
created before the outgoing one closes. The overlap is intentional: for the
length of the warm intro the student has an outgoing tutor and an incoming one,
and both should be visible.

WHAT THIS MODULE DOES NOT DO.

It does not choose the replacement automatically. `propose_replacement` returns
a *ranked suggestion* and `complete` takes an explicit tutor id, because
§1.7 says the replacement should come "from existing guest pool where possible,
to preserve some familiarity" — a preference, not an algorithm — and because the
cohort matcher that would own an automatic choice does not exist (O-16).
"""

import logging
from datetime import datetime, timezone

from app.services import matching_service, progress_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

HANDOFF_REASONS = ("tutor_inactive", "tutor_on_leave", "cohort_change",
                   "student_request", "admin")


class HandoffRefused(Exception):
    """Raised for a refusal a caller should show, not log.

    Mirrors `booking_service.BookingRefused`: the reasons here are things an
    admin screen wants to render next to the student ("this student has no
    active home tutor"), not 500s.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Reading the current state ─────────────────────────────────────────────────

def _links(student_id: str, states: tuple[str, ...]) -> list[dict]:
    try:
        result = db.schema("app").table("student_tutor_link").select(
            "id, tutor_id, role, state, source, started_at, ended_at, replaced_link_id"
        ).eq("student_id", student_id).in_("state", list(states)).order(
            "started_at").execute()
        return result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("link read failed for %s: %s", student_id, exc)
        # Empty, not a raise. Every caller below treats "no links" as a refusal
        # with a reason, which is a better failure than a 500 on a read.
        return []


def status(student_id: str) -> dict:
    """What §2.5's "meet your new tutor" screen reads.

    Returns the outgoing tutor, the incoming one if it exists, and whether the
    warm intro is still outstanding. **`in_progress` is false when nothing is
    pending** — the screen must be able to tell "no handoff" from "a handoff
    with no replacement chosen yet", because those look identical if all you
    return is a null tutor.
    """
    rows = _links(student_id, ("active", "handoff_pending"))
    outgoing = next((r for r in rows
                     if r["role"] == "home" and r["state"] == "handoff_pending"), None)
    incoming = next((r for r in rows
                     if r["role"] == "home" and r["state"] == "active"
                     and r.get("source") == "handoff"), None)

    tutor_ids = [r["tutor_id"] for r in (outgoing, incoming) if r]
    display = matching_service._tutor_display(tutor_ids) if tutor_ids else {}

    def shape(row: dict | None) -> dict | None:
        if not row:
            return None
        d = display.get(row["tutor_id"], {})
        return {
            "link_id": row["id"],
            "tutor_id": row["tutor_id"],
            "since": row["started_at"],
            "name": " ".join(p for p in (d.get("first_name"), d.get("last_name")) if p) or None,
            "avatar_url": d.get("avatar_url"),
            "bio": d.get("bio"),
            "language": d.get("language"),
        }

    return {
        "in_progress": outgoing is not None,
        "outgoing": shape(outgoing),
        "incoming": shape(incoming),
        # §1.7: "flag the enrollment for a 'warm intro' session rather than
        # silent reassignment". The intro is outstanding from the moment a
        # replacement exists until the outgoing link closes.
        "warm_intro_due": outgoing is not None and incoming is not None,
    }


# ── Choosing a replacement ────────────────────────────────────────────────────

def propose_replacement(student_id: str) -> dict:
    """Ranked suggestions, guest pool first. §1.7's "preserve some familiarity".

    Order:

    1. **The student's own guest tutors.** Someone they have already been
       taught by. This is the whole reason the guest pool exists as a pool
       rather than a rota, and it is the difference between a handoff and a
       reassignment.
    2. **Any other active tutor**, ranked by how few students they hold — the
       concentration cap's logic applied as a preference before it has to be
       applied as a refusal.

    Every candidate carries its own `evaluate_assignment` verdict, so an admin
    sees *"would breach the concentration cap"* against a name rather than
    picking it and being refused.
    """
    guests = [r for r in _links(student_id, ("active",)) if r["role"] == "guest"]
    guest_ids = [r["tutor_id"] for r in guests]

    try:
        result = db.schema("app").table("tutor").select("id").eq(
            "status", "active").is_("deleted_at", "null").execute()
        all_ids = [r["id"] for r in (result.data or [])]
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor list failed during handoff proposal: %s", exc)
        all_ids = list(guest_ids)

    # The outgoing tutor must never be proposed as their own replacement.
    outgoing = next((r["tutor_id"] for r in _links(student_id, ("handoff_pending",))
                     if r["role"] == "home"), None)

    ordered = [t for t in guest_ids if t != outgoing]
    ordered += sorted(
        (t for t in all_ids if t not in guest_ids and t != outgoing),
        key=lambda t: matching_service.concentration(t)["tutor_students"],
    )

    display = matching_service._tutor_display(ordered) if ordered else {}
    candidates = []
    for tutor_id in ordered:
        verdict = matching_service.evaluate_assignment(student_id, tutor_id, "home")
        d = display.get(tutor_id, {})
        candidates.append({
            "tutor_id": tutor_id,
            "name": " ".join(p for p in (d.get("first_name"), d.get("last_name")) if p) or None,
            "avatar_url": d.get("avatar_url"),
            "familiar": tutor_id in guest_ids,
            "allowed": verdict["allowed"],
            "blocking": verdict["blocking"],
            "advisories": verdict["advisories"],
        })

    return {
        "candidates": candidates,
        "from_guest_pool": sum(1 for c in candidates if c["familiar"]),
        # Said plainly rather than returning an empty list with no explanation.
        "supply_limited": len(candidates) == 0,
    }


# ── The state machine ─────────────────────────────────────────────────────────

async def begin(student_id: str, reason: str = "tutor_inactive") -> dict:
    """`active → handoff_pending` on the student's home link.

    Idempotent: beginning a handoff that is already pending returns the pending
    state rather than raising. An admin clicking twice, or a tutor-level
    departure sweeping a student already swept, must not be an error.
    """
    if reason not in HANDOFF_REASONS:
        raise HandoffRefused(f"reason must be one of {HANDOFF_REASONS}")

    rows = _links(student_id, ("active", "handoff_pending"))
    pending = next((r for r in rows
                    if r["role"] == "home" and r["state"] == "handoff_pending"), None)
    if pending:
        return {"began": False, "already_pending": True, **status(student_id)}

    home = next((r for r in rows if r["role"] == "home" and r["state"] == "active"), None)
    if not home:
        raise HandoffRefused("this student has no active home tutor to hand off")

    db.schema("app").table("student_tutor_link").update({
        # `ended_at` is deliberately NOT set. The outgoing tutor still teaches
        # until the intro happens, and `student_tutor_link_ended_state` would
        # reject the row anyway — the constraint and the product agree here.
        "state": "handoff_pending",
        "updated_at": _now(),
    }).eq("id", home["id"]).execute()

    logger.info("Handoff begun for student %s (reason=%s)", student_id, reason)
    return {"began": True, "reason": reason,
            **status(student_id), **propose_replacement(student_id)}


async def complete(student_id: str, new_tutor_id: str) -> dict:
    """`handoff_pending → reassigned`, and a new home link for the replacement.

    Four writes, in an order chosen so a failure part-way leaves a state a human
    can read rather than one that lies:

    1. **End any GUEST link** the incoming tutor already holds with this
       student. Without this the promoted tutor is both home and guest, and
       every roster query returns them twice.
    2. **Create the new home link** — `source='handoff'`, `replaced_link_id`
       pointing at the outgoing one, so §1.8's "a new tutor inheriting a student
       must get full historical access" has a chain to walk.
    3. **Close the outgoing link** as `reassigned` with `ended_at`.
    4. **Repoint an active termly commitment**, if there is one.

    Step 2 before step 3 is not arbitrary. The partial unique index permits it —
    `handoff_pending` is not `active` — and doing it this way means an
    interruption leaves the student *with* a tutor. The reverse order would
    leave them with none.

    **THE COMMITMENT BLOCK IS DELIBERATELY BYPASSED, AND ONLY IT.**
    `evaluate_assignment` refuses a home assignment that moves a
    commitment-locked student, with the message *"reassignment needs a §1.7
    handoff, not a rematch"*. This is that handoff. §1.3's guarantee is that the
    platform will not move the student for its own convenience; it cannot mean
    the student keeps a tutor who has left. Every other blocking reason —
    concentration cap, bad role — still refuses, and the bypass is recorded on
    the return value so it is visible in whatever logged it.
    """
    rows = _links(student_id, ("active", "handoff_pending"))
    outgoing = next((r for r in rows
                     if r["role"] == "home" and r["state"] == "handoff_pending"), None)
    if not outgoing:
        raise HandoffRefused("no handoff is pending for this student")
    if outgoing["tutor_id"] == new_tutor_id:
        raise HandoffRefused("the replacement is the outgoing tutor")

    verdict = matching_service.evaluate_assignment(student_id, new_tutor_id, "home")
    commitment_bypassed = False
    blocking = []
    for reason in verdict["blocking"]:
        if "termly commitment" in reason:
            commitment_bypassed = True
            continue
        blocking.append(reason)
    if blocking:
        raise HandoffRefused("; ".join(blocking))

    now = _now()

    # 1. The incoming tutor cannot be home and guest at once.
    existing_guest = next((r for r in rows
                           if r["tutor_id"] == new_tutor_id and r["role"] == "guest"
                           and r["state"] == "active"), None)
    if existing_guest:
        db.schema("app").table("student_tutor_link").update({
            "state": "ended", "ended_at": now, "updated_at": now,
        }).eq("id", existing_guest["id"]).execute()

    # 2. The new relationship, chained to the one it replaces.
    created = db.schema("app").table("student_tutor_link").insert({
        "student_id": student_id,
        "tutor_id": new_tutor_id,
        "role": "home",
        "source": "handoff",
        "replaced_link_id": outgoing["id"],
    }).execute()

    # 3. Close the outgoing one.
    db.schema("app").table("student_tutor_link").update({
        "state": "reassigned", "ended_at": now, "updated_at": now,
    }).eq("id", outgoing["id"]).execute()

    # 4. A term the student paid for names a tutor. Leaving it pointing at
    #    someone who has left would make `locked_by_commitment` block the very
    #    tutor the platform just assigned — a student would be unable to be
    #    reassigned again for the rest of the term.
    repointed = False
    try:
        commitments = db.schema("app").table("enrollment_commitment").select(
            "id, locked_home_tutor_id, status"
        ).eq("student_id", student_id).eq("status", "active").execute()
        for row in commitments.data or []:
            if row.get("locked_home_tutor_id") == outgoing["tutor_id"]:
                db.schema("app").table("enrollment_commitment").update({
                    "locked_home_tutor_id": new_tutor_id, "updated_at": now,
                }).eq("id", row["id"]).execute()
                repointed = True
    except Exception as exc:  # noqa: BLE001
        # Logged loudly and not raised: the handoff itself succeeded, and
        # failing here would leave the student reassigned but the caller told it
        # went wrong. A stale lock is recoverable; a confused caller retrying a
        # completed handoff is not.
        logger.error("commitment repoint failed for %s: %s", student_id, exc)

    # §1.8: "a new tutor inheriting a student must get full historical access,
    # not a fresh start."
    #
    # **GRANTED as of 2026-08-28 (§83, plan O-27), and still reported.**
    #
    # This used to be reported and not granted: history was gated on the course
    # join and a handoff writes `student_tutor_link`, a different table, so an
    # incoming tutor not already assigned to the student's course inherited the
    # relationship and none of the history.
    #
    # It was NOT closed by auto-creating the `tutor_assignment` row — that row is
    # course-level and would have handed the incoming tutor every student on the
    # course, which is an admin's staffing decision and not a side effect of one
    # student's handoff. It was closed by widening the PREDICATE instead of the
    # grant: `app.teaches_student()` now also matches an ACTIVE
    # `student_tutor_link`, home or guest. The link *is* the "my student"
    # relationship; the course join was only ever a proxy for it, and the proxy
    # is what broke at a handoff.
    #
    # The reporting below is kept deliberately. `granted` should now be true for
    # every completed handoff, so a false is a real anomaly worth surfacing
    # rather than the ordinary case — and `missing_course_assignments` still
    # tells an admin which course assignments a tutor lacks, which matters for
    # everything course-scoped that is not history.
    access = progress_service.teaches_student(new_tutor_id, student_id)
    missing = sorted(set(progress_service._student_courses(student_id))
                     - set(access["shared_courses"]))
    if not access["allowed"]:
        logger.warning(
            "Handoff for student %s gave tutor %s no history access — "
            "no shared course assignment (courses: %s)",
            student_id, new_tutor_id, missing)

    logger.info("Handoff completed for student %s: %s -> %s",
                student_id, outgoing["tutor_id"], new_tutor_id)
    return {
        "completed": True,
        "progress_access": {
            "granted": access["allowed"],
            "shared_courses": access["shared_courses"],
            "missing_course_assignments": missing,
        },
        "link": (created.data or [None])[0],
        "replaced_link_id": outgoing["id"],
        "previous_tutor_id": outgoing["tutor_id"],
        "commitment_repointed": repointed,
        "commitment_lock_bypassed": commitment_bypassed,
        "advisories": verdict["advisories"],
        # The screen §2.5 renders next. Returned here so the caller does not
        # have to make a second round trip to find out what to show.
        **{"handoff": status(student_id)},
    }


async def cancel(student_id: str) -> dict:
    """`handoff_pending → active`. The tutor came back.

    Not a state in §1.7's machine, and it exists anyway: a tutor marked on leave
    who returns before a replacement is chosen would otherwise leave the student
    stranded in a pending state with no exit. It only ever reverses a handoff
    that has not completed.
    """
    pending = next((r for r in _links(student_id, ("handoff_pending",))
                    if r["role"] == "home"), None)
    if not pending:
        raise HandoffRefused("no handoff is pending for this student")

    db.schema("app").table("student_tutor_link").update({
        "state": "active", "updated_at": _now(),
    }).eq("id", pending["id"]).execute()

    logger.info("Handoff cancelled for student %s", student_id)
    return {"cancelled": True, **status(student_id)}


async def begin_for_tutor(tutor_id: str, reason: str = "tutor_inactive") -> dict:
    """Every student this tutor is home to, moved to `handoff_pending`.

    The realistic entry point: a tutor goes on leave, not a student requests a
    change. Per-student failures are isolated and counted rather than aborting
    the sweep — a tutor with fifteen students must not leave twelve of them
    stranded because the thirteenth had no active link.

    **Guest links are untouched.** §1.7 is about the home relationship; a tutor
    on leave stops being someone's primary, but their guest links end naturally
    when the rota stops picking them.
    """
    try:
        result = db.schema("app").table("student_tutor_link").select(
            "student_id").eq("tutor_id", tutor_id).eq("role", "home").eq(
            "state", "active").execute()
        student_ids = [r["student_id"] for r in (result.data or [])]
    except Exception as exc:  # noqa: BLE001
        logger.error("could not list students for tutor handoff %s: %s", tutor_id, exc)
        raise HandoffRefused(f"could not read this tutor's students: {exc}") from exc

    begun, failed = [], []
    for student_id in student_ids:
        try:
            await begin(student_id, reason)
            begun.append(student_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"student_id": student_id, "error": str(exc)})
            logger.error("handoff begin failed for %s: %s", student_id, exc)

    return {"tutor_id": tutor_id, "reason": reason,
            "students_pending": len(begun), "failed": failed}
