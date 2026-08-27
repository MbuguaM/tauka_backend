"""Home/guest roster and the 1-on-1 booking lane — plan Phase 2.

Mounted at `/tutoring`. Two lanes live here and they are deliberately not mixed:

- `/tutoring/roster*` is the **relation** — who a student's tutors are (§2.1).
- `/tutoring/booking*` is the **1-on-1 lane** — the only real booking in the
  system (§3.1b). Cohort sessions are not bookable and have no endpoint here.

Every route reads its subject from the verified JWT. There is no `{student_id}`
in any path: these return a person's tutors and a person's paid session balance,
and a client-supplied id on either would be an IDOR.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import get_current_user, require_admin, require_tutor
from app.models.schemas import (
    BookSessionRequest,
    CancelSessionRequest,
    CreateLinkRequest,
    MarkSessionRequest,
)
from app.services import booking_service, matching_service
from app.services.booking_service import BookingRefused

logger = logging.getLogger(__name__)
router = APIRouter()


# ── The relation (§2.1) ───────────────────────────────────────────────────────

@router.get("/roster")
async def get_roster(user_id: str = Depends(get_current_user)):
    """STUDENT — home tutor + guest roster.

    Home and guests come back as separate fields rather than one list with a
    role, because §2.1 asks for visually distinct treatment and a client that
    partitions the list itself will eventually forget to.

    Served here rather than read from Supabase directly because the screen needs
    the tutors' **names**, and `app.user_profiles` has no policy letting a
    student read their tutor's row. Adding one would hand over the whole row —
    RLS is row-level — including `phone_number` and `location`. A student is
    entitled to their tutor's name and face, not their phone number.
    """
    return await matching_service.get_roster(user_id)


@router.get("/roster/validate")
async def validate_roster(user_id: str = Depends(get_current_user)):
    """STUDENT — is my roster complete (one home, ≥N guests)?

    Separate from `/roster` because the answer is about the platform's supply,
    not about the student, and the screen says something different for
    "your guest tutors are being arranged" than for "here they are".
    """
    return matching_service.validate_link_set(user_id)


@router.post("/links")
async def create_link(req: CreateLinkRequest, admin_id: str = Depends(require_admin)):
    """ADMIN — assign a tutor to a student.

    The matcher will call `matching_service.create_link` directly when it exists
    (`tutor-cohort-matching-plan.md` Phases 1–4 are unbuilt, and none of its
    tables are in the database). Until then this is how a link is made, and it
    runs the identical concentration-cap and role checks — the point being that
    the rules do not live in the matcher, they live under it.
    """
    result = await matching_service.create_link(
        student_id=req.student_id,
        tutor_id=req.tutor_id,
        role=req.role,
        source=req.source,
    )
    if not result.get("created"):
        # 409, not 400: the request is well-formed and the refusal is about the
        # state of the world (a cap, an existing home tutor), which the caller
        # may well resolve and retry.
        raise HTTPException(status_code=409, detail={
            "blocking": result.get("blocking", []),
            "advisories": result.get("advisories", []),
            "concentration": result.get("concentration"),
        })
    return result


@router.get("/links/evaluate")
async def evaluate_link(
    student_id: str = Query(...),
    tutor_id: str = Query(...),
    role: str = Query("guest"),
    admin_id: str = Depends(require_admin),
):
    """ADMIN — would this assignment be allowed, and why?

    §1.4 wants the concentration cap checked "on every new match/assignment, not
    just periodic auditing". This is the same check `create_link` runs, exposed
    so an admin screen can grey out an option instead of offering it and then
    refusing it.
    """
    return matching_service.evaluate_assignment(student_id, tutor_id, role)


@router.get("/links/student/{student_id}")
async def admin_student_links(student_id: str, admin_id: str = Depends(require_admin)):
    """ADMIN — the full home/guest picture for one student.

    D-05's second clause: *"the admin should be able to see whose is the home and
    guest"*. Without this the structure is visible to the matcher, to the student
    and to nobody who can fix it.
    """
    roster = await matching_service.get_roster(student_id)
    return {**roster, "validation": matching_service.validate_link_set(student_id)}


@router.get("/concentration")
async def my_concentration(user_id: str = Depends(require_tutor)):
    """TUTOR — what share of linked students this tutor holds.

    A tutor is entitled to know they are near a cap that will stop them being
    assigned more students. It counts assignments, not activity, which is why
    §3.5 permits this to gate behaviour at all.
    """
    cap = matching_service.evaluate_assignment(
        # A self-evaluation: "if one more student were linked to me, where would
        # I be?" The student id is a placeholder that cannot match a real row.
        student_id="00000000-0000-0000-0000-000000000000",
        tutor_id=user_id,
        role="guest",
    )
    return {"concentration": cap["concentration"], "supply": cap["supply"]}


# ── The 1-on-1 lane (§3.1b) ───────────────────────────────────────────────────

@router.get("/booking/options")
async def booking_options(
    duration: int = Query(50, description="25 or 50"),
    user_id: str = Depends(get_current_user),
):
    """STUDENT — which tutors can I book, and when?

    Ordered by **soonest open slot** (D-03), not home-first. This overrides spec
    §2.1's "default-suggesting home tutor slots first": in the lane the student
    paid for, the ordering key is availability, not hierarchy.
    """
    try:
        return await booking_service.get_bookable(user_id, duration)
    except BookingRefused as exc:
        raise HTTPException(status_code=400, detail=exc.message)


@router.get("/booking/entitlements")
async def entitlements(user_id: str = Depends(get_current_user)):
    """STUDENT — the balance and the ledger it was folded from.

    The entries are returned, not just the number: a student told "0 remaining"
    is entitled to see two granted, two consumed, one refunded, and a support
    conversation about a missing session is unanswerable from a single integer.
    """
    return await booking_service.get_balance(user_id)


@router.post("/booking/book")
async def book(req: BookSessionRequest, user_id: str = Depends(get_current_user)):
    """STUDENT — book a 1-on-1.

    Everything that decides whether this is allowed lives in
    `app.book_one_to_one`, which consumes the entitlement in the same
    transaction. Two statements from here would leave a window in which a crash
    yields either a free session or a stolen one.
    """
    try:
        return await booking_service.book(
            student_id=user_id,
            tutor_id=req.tutor_id,
            starts_at=req.starts_at,
            duration=req.duration_minutes,
            availability_id=req.availability_id,
        )
    except BookingRefused as exc:
        # 409 for a slot someone else just took, 400 for everything the student
        # can fix by choosing differently.
        status = 409 if exc.reason == "slot_taken" else 400
        raise HTTPException(status_code=status,
                            detail={"reason": exc.reason, "message": exc.message})


@router.post("/booking/cancel")
async def cancel(req: CancelSessionRequest, user_id: str = Depends(get_current_user)):
    """STUDENT or TUTOR — cancel a booking.

    Whether the entitlement comes back is decided by `app.cancel_one_to_one`
    against `booking.cancellation_window_hours`, and the response says which
    happened rather than leaving the student to infer it from a number that did
    not move. A tutor cancelling always refunds the student.
    """
    try:
        return await booking_service.cancel(req.session_id, user_id, req.reason)
    except BookingRefused as exc:
        status = 404 if exc.reason == "no_such_session" else 400
        raise HTTPException(status_code=status,
                            detail={"reason": exc.reason, "message": exc.message})


@router.get("/booking/sessions")
async def my_sessions(user_id: str = Depends(get_current_user)):
    """STUDENT — my 1-on-1 bookings, past and future."""
    return await booking_service.list_sessions(user_id, as_tutor=False)


@router.get("/booking/tutor-sessions")
async def tutor_sessions(user_id: str = Depends(require_tutor)):
    """TUTOR — 1-on-1s booked with me.

    No student names are attached. The tutor already reads
    `app.one_to_one_session` under RLS and can resolve their own students through
    the course join; this endpoint exists for the calendar, not for a directory.
    """
    return await booking_service.list_sessions(user_id, as_tutor=True)


@router.post("/booking/mark")
async def mark_session(req: MarkSessionRequest, user_id: str = Depends(require_tutor)):
    """TUTOR — record whether a booked 1-on-1 actually happened.

    **The only way a session becomes `completed`**, and therefore the only way it
    becomes payable. There is no sweep: `payout_service` pays for `completed`
    rows, so inferring delivery from a past timestamp would pay for lessons
    nobody attended.

    A no-show does **not** return the entitlement. The slot was held and the
    tutor turned up; the cancellation window is what a student uses when they
    cannot make it.
    """
    try:
        return await booking_service.mark_outcome(req.session_id, req.outcome, user_id)
    except BookingRefused as exc:
        status = 404 if exc.reason == "no_such_session" else 400
        raise HTTPException(status_code=status,
                            detail={"reason": exc.reason, "message": exc.message})


@router.get("/booking/unmarked")
async def unmarked(user_id: str = Depends(require_admin)):
    """ADMIN — past 1-on-1s nobody has marked.

    The same list the daily job logs. Unmarked sessions are unpaid sessions, so
    this is the queue that stands between a tutor and their money.
    """
    return {"sessions": await booking_service.unmarked_sessions()}
