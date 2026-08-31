"""The 1-on-1 lane — plan Phase 2b, §3.1b; matching plan §1b.7 / §5.3.

**This is the only real booking in the system.** A cohort session is a fixed
weekly slot materialised by `app.generate_cohort_sessions()` and never
rescheduled; membership is the entitlement and nothing is booked. A 1-on-1 is
bought — by the Intensive tier's 2/month, or outright — and booked against a
tutor's availability.

Two invariants, both enforced in SQL rather than here:

1. **The balance is a fold over an append-only ledger, never a counter.** The
   matching plan gives the reason in one line: *"a lost decrement here is a
   refund"*. `app.session_entitlement` has an append-only trigger that fires for
   the service role too, so this module cannot violate it even by accident.
2. **A booking and its consumption cannot come apart.** `app.book_one_to_one`
   does both in one transaction with the student's ledger rows locked. Doing it
   as two statements from here would leave a window in which a crash yields
   either a free session or a stolen one.

This module's job is therefore the parts SQL is bad at: expanding availability
into slots, mapping the function's stable refusal words onto something a person
can read, and deciding what a student is granted at the start of a billing cycle.
"""

import logging
from datetime import datetime, date, time, timedelta, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# {25, 50} from the pricing page (matching plan §1b.3). Never a hardcoded 50.
DURATIONS = (25, 50)

_DEFAULTS = {
    "booking.cancellation_window_hours": "24",
    "booking.horizon_days": "28",
    "booking.max_reschedules": "2",
    "entitlement.intensive_one_to_one_per_cycle": "2",
}

# `app.book_one_to_one` and `app.cancel_one_to_one` raise single-word messages so
# that this mapping is a lookup rather than prose-parsing. Anything not in here
# is a genuine fault and is allowed to surface as a 500.
REFUSALS = {
    "invalid_duration": "Sessions are 25 or 50 minutes.",
    "invalid_outcome": "Mark a session as completed or as a no-show.",
    "not_your_session": "Only the tutor who taught it can mark this session.",
    "not_markable": "That session has already been marked or cancelled.",
    "not_yet": "That session has not happened yet.",
    "past_slot": "That time has already passed.",
    "not_your_tutor": "You can only book your own tutors.",
    "no_entitlement": "You have no 1-on-1 sessions left this cycle.",
    "slot_taken": "That slot was just taken. Pick another.",
    "no_such_session": "That booking no longer exists.",
    "not_cancellable": "That booking has already been cancelled or completed.",
    "not_yours": "That is not your booking.",
    # §80.2. Deliberately separate from `not_cancellable` even though both mean
    # "status is not booked": a student who tapped Reschedule should be told
    # about rescheduling.
    "not_reschedulable": "That booking can no longer be moved.",
    "reschedule_limit": "This session has been moved as many times as it can be. "
                        "Keep this time, or cancel it.",
}


class BookingRefused(Exception):
    """A refusal the student can act on, carrying the reason word."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(REFUSALS.get(reason, "That booking could not be completed."))

    @property
    def message(self) -> str:
        return REFUSALS.get(self.reason, "That booking could not be completed.")


def _config(keys: list[str]) -> dict[str, str]:
    values = dict(_DEFAULTS)
    try:
        result = db.schema("app").table("config").select("key, value").in_("key", keys).execute()
        for row in result.data or []:
            if row.get("value") not in (None, ""):
                values[row["key"]] = row["value"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("booking config read failed, using defaults: %s", exc)
    return values


def _as_int(value: str | None, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _refusal_from(exc: Exception) -> str | None:
    """Pull the function's stable word out of a PostgREST error envelope."""
    text = str(exc)
    for word in REFUSALS:
        if word in text:
            return word
    return None


# ── Entitlement balance ───────────────────────────────────────────────────────

async def get_balance(student_id: str, kind: str = "one_to_one") -> dict:
    """Fold the ledger. Returns the balance and the rows it was folded from.

    The rows come back because a student who is told "0 remaining" is entitled to
    see why — two granted, two consumed, one refunded — and because a support
    conversation about a missing session is unanswerable from a single number.
    """
    now = datetime.now(timezone.utc)
    try:
        result = db.schema("app").table("session_entitlement").select(
            "id, direction, quantity, reason, reference_type, reference_id, "
            "period_start, period_end, expires_at, occurred_at"
        ).eq("student_id", student_id).eq("kind", kind).order(
            "occurred_at", desc=True
        ).limit(200).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("entitlement read failed for %s: %s", student_id, exc)
        # A balance that failed to load is not zero. Saying `None` forces the
        # caller to render "we could not check" rather than "you have none",
        # which is the difference between a retry and a support ticket.
        return {"balance": None, "kind": kind, "entries": [], "degraded": True}

    balance = 0
    for row in rows:
        expires = row.get("expires_at")
        if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= now:
            continue
        q = int(row.get("quantity") or 0)
        balance += q if row["direction"] in ("grant", "refund") else -q

    return {
        "balance": balance,
        "kind": kind,
        "entries": rows,
        "degraded": False,
    }


async def grant_cycle_entitlement(student_id: str, tier: str, period_start: datetime,
                                  period_end: datetime) -> dict:
    """The monthly grant. Idempotent by construction, not by checking first.

    `idx_session_entitlement_one_cycle_grant` is UNIQUE on
    `(student_id, kind, period_start) WHERE reason='tier_cycle' AND
    direction='grant'`, so a retry, a double-trigger or a clock skew cannot hand
    out a second month's sessions. Losing that race is a success.

    Only `intensive_tier` grants 1-on-1s (matching plan §1b.7: "2 per month").
    Every other tier gets zero, and a top-up is an `addon_purchase` row written
    by the Stripe path, not by this function.
    """
    per_cycle = _as_int(
        _config(["entitlement.intensive_one_to_one_per_cycle"])[
            "entitlement.intensive_one_to_one_per_cycle"], 2)
    quantity = per_cycle if tier == "intensive_tier" else 0

    if quantity <= 0:
        return {"granted": 0, "reason": "tier_grants_none", "tier": tier}

    try:
        db.schema("app").table("session_entitlement").insert({
            "student_id": student_id,
            "kind": "one_to_one",
            "direction": "grant",
            "quantity": quantity,
            "reason": "tier_cycle",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            # Unconsumed tier sessions die with the cycle. They are an allowance,
            # not a purchase; a bought top-up has expires_at NULL and does roll.
            "expires_at": period_end.isoformat(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return {"granted": 0, "reason": "already_granted_this_cycle", "tier": tier}
        raise

    return {"granted": quantity, "reason": "tier_cycle", "tier": tier}


# ── Availability → bookable slots ─────────────────────────────────────────────

def _weekly_windows(tutor_ids: list[str]) -> list[dict]:
    if not tutor_ids:
        return []
    try:
        result = db.schema("app").table("tutor_availability").select(
            "id, tutor_id, day_of_week, start_time, end_time, timezone"
        ).in_("tutor_id", tutor_ids).eq("active", True).execute()
        return result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("availability read failed: %s", exc)
        return []


def _taken(tutor_ids: list[str], frm: datetime, to: datetime) -> set[tuple[str, str]]:
    """(tutor_id, iso instant) pairs already spoken for.

    Both lanes count. A tutor leading a cohort session at 18:00 is not available
    for a 1-on-1 at 18:00, and a booking flow that only consulted its own table
    would cheerfully double-book them.
    """
    busy: set[tuple[str, str]] = set()
    if not tutor_ids:
        return busy
    try:
        rows = db.schema("app").table("one_to_one_session").select(
            "tutor_id, scheduled_at"
        ).in_("tutor_id", tutor_ids).in_("status", ["booked", "completed"]).gte(
            "scheduled_at", frm.isoformat()).lte("scheduled_at", to.isoformat()).execute()
        for r in rows.data or []:
            busy.add((r["tutor_id"], r["scheduled_at"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("booked-slot read failed: %s", exc)
    try:
        rows = db.schema("app").table("cohort_session").select(
            "lead_tutor_id, scheduled_at"
        ).in_("lead_tutor_id", tutor_ids).eq("status", "scheduled").gte(
            "scheduled_at", frm.isoformat()).lte("scheduled_at", to.isoformat()).execute()
        for r in rows.data or []:
            if r.get("lead_tutor_id"):
                busy.add((r["lead_tutor_id"], r["scheduled_at"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohort-slot read failed: %s", exc)
    return busy


def _parse_time(raw: str) -> time:
    return time.fromisoformat(str(raw)[:8] if len(str(raw)) >= 8 else str(raw))


def expand_slots(tutor_ids: list[str], duration: int, horizon_days: int) -> list[dict]:
    """Turn weekly availability windows into concrete future instants.

    The window is stored as (day_of_week, start_time, end_time, timezone) — a
    repeating rule, not a list of times — so somebody has to expand it, and doing
    it here rather than in the client means one implementation of the timezone
    arithmetic instead of three.

    `day_of_week` is 0=Sun..6=Sat, the convention the client already uses and the
    one `app.generate_cohort_sessions` converts from. Python's `weekday()` is
    0=Mon, so the conversion is explicit below rather than assumed.
    """
    from zoneinfo import ZoneInfo

    windows = _weekly_windows(tutor_ids)
    if not windows:
        return []

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)
    busy = _taken(tutor_ids, now, horizon)

    slots: list[dict] = []
    for w in windows:
        try:
            tz = ZoneInfo(w.get("timezone") or "UTC")
        except Exception:  # noqa: BLE001
            # An unknown zone is a data problem, not a reason to drop the tutor
            # out of the picker silently. UTC keeps them visible and the time
            # wrong-but-explicable; a dropped tutor is invisible and unexplicable.
            logger.warning("unknown timezone %r on availability %s", w.get("timezone"), w.get("id"))
            tz = ZoneInfo("UTC")

        try:
            start = _parse_time(w["start_time"])
            end = _parse_time(w["end_time"])
        except (ValueError, KeyError, TypeError):
            logger.warning("unparseable availability window %s", w.get("id"))
            continue

        want_dow = int(w.get("day_of_week", 0))
        cursor: date = now.astimezone(tz).date()
        for _ in range(horizon_days + 1):
            # Python weekday(): 0=Mon..6=Sun → convert to 0=Sun..6=Sat.
            if (cursor.weekday() + 1) % 7 == want_dow:
                slot_start = datetime.combine(cursor, start, tzinfo=tz)
                window_end = datetime.combine(cursor, end, tzinfo=tz)
                while slot_start + timedelta(minutes=duration) <= window_end:
                    instant = slot_start.astimezone(timezone.utc)
                    if now < instant <= horizon:
                        iso = instant.isoformat().replace("+00:00", "Z")
                        # Compare on the instant, not the string: the database
                        # returns its own ISO formatting and the two need not
                        # match textually.
                        if not any(t == w["tutor_id"] and _same_instant(s, instant)
                                   for (t, s) in busy):
                            slots.append({
                                "tutor_id": w["tutor_id"],
                                "availability_id": w["id"],
                                "starts_at": iso,
                                "duration_minutes": duration,
                            })
                    slot_start += timedelta(minutes=duration)
            cursor += timedelta(days=1)

    slots.sort(key=lambda s: (s["starts_at"], s["tutor_id"]))
    return slots


def _same_instant(raw: str, instant: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) == instant
    except ValueError:
        return False


async def get_bookable(student_id: str, duration: int = 50) -> dict:
    """What this student may book, and when — spec §2.1's booking screen.

    **Ordered by soonest open slot, not by home-first (D-03).** This deliberately
    overrides spec §2.1's "default-suggesting home tutor slots first, guest tutor
    slots as secondary": in the lane the student paid for, the ordering key is
    availability, not hierarchy. The home/guest ratio governs the cohort rota and
    nothing else.

    Eligibility is the student's own `student_tutor_link` set (D-02) — home tutor
    plus their guest pool, and nobody else. A purchased session is not an open
    marketplace: letting a student book any tutor on the platform reintroduces
    the concentration risk §1.4 exists to cap and defeats the guest-pool dilution
    §1.2 exists to create.
    """
    if duration not in DURATIONS:
        raise BookingRefused("invalid_duration")

    from app.services import matching_service

    roster = await matching_service.get_roster(student_id)
    tutors = ([roster["home"]] if roster.get("home") else []) + roster.get("guests", [])
    by_id = {t["tutor_id"]: t for t in tutors}

    cfg = _config(list(_DEFAULTS))
    horizon = _as_int(cfg["booking.horizon_days"], 28)
    slots = expand_slots(list(by_id), duration, horizon)

    # Group by tutor, then order the tutors by their soonest slot. A tutor with
    # no availability at all is still listed, with an empty slot list and a
    # reason — dropping them would read as "this tutor is not yours any more".
    grouped: dict[str, list[dict]] = {tid: [] for tid in by_id}
    for s in slots:
        grouped.setdefault(s["tutor_id"], []).append(s)

    out = []
    for tid, tutor in by_id.items():
        tslots = grouped.get(tid, [])
        out.append({
            **tutor,
            "next_slot": tslots[0]["starts_at"] if tslots else None,
            "slots": tslots[:60],
        })
    out.sort(key=lambda t: (t["next_slot"] is None, t["next_slot"] or ""))

    balance = await get_balance(student_id)
    return {
        "duration_minutes": duration,
        "horizon_days": horizon,
        "balance": balance["balance"],
        "balance_degraded": balance["degraded"],
        "cancellation_window_hours": _as_int(cfg["booking.cancellation_window_hours"], 24),
        "tutors": out,
        # True when nobody has published any availability. The screen must say
        # "no times published yet", never "no tutors" — 0 rows in
        # tutor_availability is the live state and it is not the student's fault.
        "no_availability_published": not slots,
    }


# ── Booking and cancellation ──────────────────────────────────────────────────

async def book(student_id: str, tutor_id: str, starts_at: str, duration: int,
               availability_id: str | None = None) -> dict:
    """Book a slot. Every rule is inside `app.book_one_to_one`, on purpose."""
    if duration not in DURATIONS:
        raise BookingRefused("invalid_duration")
    try:
        result = db.schema("app").rpc("book_one_to_one", {
            "p_student": student_id,
            "p_tutor": tutor_id,
            "p_at": starts_at,
            "p_duration": duration,
            "p_availability": availability_id,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        reason = _refusal_from(exc)
        if reason:
            raise BookingRefused(reason) from exc
        logger.error("book_one_to_one failed for %s/%s: %s", student_id, tutor_id, exc)
        raise

    session_id = result.data if isinstance(result.data, str) else None
    balance = await get_balance(student_id)
    return {
        "session_id": session_id,
        "tutor_id": tutor_id,
        "starts_at": starts_at,
        "duration_minutes": duration,
        "balance_remaining": balance["balance"],
    }


async def cancel(session_id: str, by_user: str, reason: str | None = None) -> dict:
    """Cancel a booking. Whether the session comes back is the function's call.

    The window is `booking.cancellation_window_hours`. Outside it the session is
    cancelled and **the entitlement is not returned** — that is what a window is
    for — and the response says so rather than leaving the student to discover it
    from a number that did not move.
    """
    try:
        result = db.schema("app").rpc("cancel_one_to_one", {
            "p_session": session_id,
            "p_by": by_user,
            "p_reason": reason,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        word = _refusal_from(exc)
        if word:
            raise BookingRefused(word) from exc
        logger.error("cancel_one_to_one failed for %s: %s", session_id, exc)
        raise

    refunded = bool(result.data)
    return {
        "cancelled": True,
        "refunded": refunded,
        "message": (
            "Cancelled — the session has been returned to your balance."
            if refunded else
            "Cancelled. This was inside the cancellation window, so the session "
            "was not returned to your balance."
        ),
    }


async def reschedule(session_id: str, starts_at: str, by_user: str,
                     tutor_id: str | None = None,
                     availability_id: str | None = None) -> dict:
    """Move a booking. Spec §2.6: *"few taps, no punitive copy/messaging."*

    **This is not cancel-then-book, and the difference is the whole feature.**
    Composed from those two, a move made outside the cancellation window costs
    the student a session — cancel does not refund there, and book consumes
    again. `app.reschedule_one_to_one` does it in one transaction and **never
    touches `app.session_entitlement`**: a reschedule is neither a grant nor a
    spend, and a refund/consume pair would net to zero while making the ledger
    unreadable for the one question it exists to answer.

    WHAT IT DOES COST, AND WHY.

    Moving a session from *inside* the window stamps `refund_forfeited` on it, so
    cancelling it later never refunds. Without that, the window would stop
    existing: book for tomorrow, move it to next month two hours before, then
    cancel with thirty days' notice and be refunded in full — a late drop
    laundered through a reschedule.

    So the rule is: **you may always move a session; you may not convert a late
    drop into an early one.** A tutor moving it costs the student nothing, on the
    same reasoning `cancel` already uses — a student whose tutor rearranged has
    done nothing wrong.

    The response says whether the forfeit was taken. A student finds out at the
    moment it happens, not later from a balance that did not move.
    """
    payload: dict = {
        "p_session": session_id,
        "p_at": starts_at,
        "p_by": by_user,
    }
    # Sent only when moving tutors. Passing NULL explicitly is the same thing to
    # the function, but an absent key keeps the RPC log readable about what was
    # actually asked for.
    if tutor_id:
        payload["p_tutor"] = tutor_id
    if availability_id:
        payload["p_availability"] = availability_id

    try:
        result = db.schema("app").rpc("reschedule_one_to_one", payload).execute()
    except Exception as exc:  # noqa: BLE001
        word = _refusal_from(exc)
        if word:
            raise BookingRefused(word) from exc
        logger.error("reschedule_one_to_one failed for %s: %s", session_id, exc)
        raise

    row = result.data if isinstance(result.data, dict) else {}
    forfeited = bool(row.get("refund_forfeited"))
    left = row.get("reschedules_left")

    # Assembled here rather than in the client so the two facts a student needs
    # — can I move it again, and did that cost me the refund — are stated
    # together and cannot drift apart across two codebases.
    parts = ["Moved."]
    if forfeited:
        parts.append(
            "Because this was inside the cancellation window, cancelling it "
            "later will not return it to your balance."
        )
    if isinstance(left, int):
        parts.append(
            "You can move it once more." if left == 1
            else "This session cannot be moved again." if left == 0
            else f"You can move it {left} more times."
        )

    return {
        "rescheduled": True,
        "session_id": session_id,
        "scheduled_at": row.get("scheduled_at"),
        "tutor_id": row.get("tutor_id"),
        "rescheduled_count": row.get("rescheduled_count"),
        "reschedules_left": left,
        "refund_forfeited": forfeited,
        "message": " ".join(parts),
    }


async def list_sessions(user_id: str, as_tutor: bool = False) -> list[dict]:
    """Bookings for one person. RLS would allow the client to read these
    directly; they are served here so the tutor's name can be attached without
    granting the student a read on `app.user_profiles` (see
    `matching_service._tutor_display`)."""
    from app.services import matching_service

    column = "tutor_id" if as_tutor else "student_id"
    try:
        result = db.schema("app").table("one_to_one_session").select(
            "id, student_id, tutor_id, scheduled_at, duration_minutes, status, "
            "cancelled_at, video_session_id"
        ).eq(column, user_id).order("scheduled_at", desc=True).limit(100).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("session list failed for %s: %s", user_id, exc)
        return []

    if as_tutor:
        return rows

    display = matching_service._tutor_display([r["tutor_id"] for r in rows])
    for r in rows:
        d = display.get(r["tutor_id"], {})
        r["tutor_name"] = " ".join(
            p for p in (d.get("first_name"), d.get("last_name")) if p) or None
        r["tutor_avatar_url"] = d.get("avatar_url")
    return rows


# ── Marking a session delivered ───────────────────────────────────────────────

async def mark_outcome(session_id: str, outcome: str, actor_id: str) -> dict:
    """The tutor records whether a booked 1-on-1 happened.

    **Delivery is never inferred from a timestamp.** `payout_service` pays for
    `completed` rows, so an automatic sweep would pay a tutor for a session
    nobody attended. The cohort domain already holds the weaker version of this
    rule — *"a past session the tutor has not written up shows as 'not marked
    yet', not as missed"* — and inferring delivery is worse than inferring
    absence, because money moves.

    Authorisation lives in `app.mark_one_to_one_outcome`, not here: it is
    SECURITY DEFINER and checks `tutor_id = auth.uid()` or admin, exactly as
    `app.mark_attendance` does for cohorts.
    """
    if outcome not in ("completed", "no_show"):
        raise BookingRefused("invalid_outcome")
    try:
        db.schema("app").rpc("mark_one_to_one_outcome", {
            "p_session": session_id,
            "p_outcome": outcome,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        reason = _refusal_from(exc)
        if reason:
            raise BookingRefused(reason) from exc
        logger.error("mark_one_to_one_outcome failed for %s: %s", session_id, exc)
        raise

    return {
        "session_id": session_id,
        "outcome": outcome,
        # Said explicitly because it is the surprising half. The slot was held
        # and the tutor turned up; the cancellation window exists precisely so a
        # student who cannot make it says so in time.
        "entitlement_returned": False,
        "message": ("Marked as delivered." if outcome == "completed"
                    else "Marked as a no-show. The session was not returned to "
                         "the student's balance."),
    }


async def unmarked_sessions(hours: int = 24) -> list[dict]:
    """Past 1-on-1s nobody has marked yet. **A report, not an action.**

    The daily job surfaces these so a tutor can be nudged. It deliberately does
    not decide: a session left unmarked for a week is a question for a person,
    and answering it automatically is how a payout run starts paying for lessons
    that never happened.
    """
    try:
        result = db.schema("app").rpc("unmarked_one_to_one_sessions",
                                      {"p_hours": hours}).execute()
        return result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("unmarked session lookup failed: %s", exc)
        return []
