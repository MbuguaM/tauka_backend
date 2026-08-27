"""Termly commitments — plan Phase 4, spec §1.3 / §2.3.

A termly commitment is a **contract**, not a tier. §4.4 is explicit about why:

> Adding a fifth tier value would put "what you can use" and "what you promised"
> in one column and break every gate that switches on it.

So the commitment *names* the tier it entitles and activation calls
`change_tier(..., source='commitment')`. `app.student.tier` stays the entitlement
axis; `app.enrollment_commitment` is the promise. Cancelling one is a tier
change, not a schema change.

WHAT IS ACTUALLY BEING SOLD (§2.3):

> Plan selection screen needs the termly option framed around the **guarantee**,
> not just price — "your slot with [tutor name] is protected for the term".

That is `locked_home_tutor_id` plus `priority_matching`, and it is the reason the
matcher must treat an active commitment as a hard constraint rather than a
preference. The student is not buying a discount; they are buying a promise that
nobody moves them.

SHIPPING WITH PLACEHOLDERS (D-18). No Stripe price objects exist for either
billing mode. `settings.stripe_price_term_prepay` / `_installment` are defined
and unset, `featureStatus.js` gates the UI at `'coming'`, and every checkout path
here **refuses loudly** rather than creating a session against an empty price id
— which Stripe would reject anyway, but with an error nobody could act on.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "commitment.term_months": "3",
    "commitment.self_cancel": "none",
    "commitment.priority_is_hard": "true",
}

BILLING_MODES = ("prepay", "installment")


class CommitmentUnavailable(Exception):
    """The termly product is not purchasable yet (D-18: placeholders)."""


def _config(keys: list[str] | None = None) -> dict[str, str]:
    values = dict(_DEFAULTS)
    try:
        result = db.schema("app").table("config").select("key, value").in_(
            "key", keys or list(_DEFAULTS)).execute()
        for row in result.data or []:
            if row.get("value") not in (None, ""):
                values[row["key"]] = row["value"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("commitment config read failed, using defaults: %s", exc)
    return values


def _as_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def term_window(start: date | None = None) -> tuple[date, date]:
    """The term a commitment bought today would cover.

    Length comes from `commitment.term_months` — a "term" is a product decision,
    not a constant, and the pricing page and this function must not be able to
    disagree about it.
    """
    start = start or datetime.now(timezone.utc).date()
    months = _as_int(_config(["commitment.term_months"])["commitment.term_months"], 3)
    # Month arithmetic without dateutil: add months, then clamp the day into the
    # target month so a 31st does not overflow into the next one.
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    import calendar
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start, date(year, month, day)


# ── Milestone targets ─────────────────────────────────────────────────────────

def parse_milestone(target: str | None) -> int | None:
    """`'unit_order:15'` → 15. Anything else → None.

    **A threshold on `unit.unit_order`, deliberately not a level name.**
    `app.unit` has no `level` column — the matching plan's §5.4 proposed one and
    it was never built — so a target naming a level would reference something
    that does not exist, and the hook would silently never fire.
    """
    if not target:
        return None
    try:
        kind, value = str(target).split(":", 1)
    except ValueError:
        return None
    if kind.strip() != "unit_order":
        return None
    try:
        n = int(value)
        return n if n > 0 else None
    except ValueError:
        return None


async def check_milestone(commitment: dict) -> bool:
    """Has this student completed every unit up to the target?

    §1.3: *"needs a completion-check hook against the existing FSI-phase progress
    data."* That data is `app.unit_progress`, which gained its first writer on
    2026-08-23.

    **Every unit up to the threshold, not merely the threshold unit.** Units are
    self-paced (matching plan §1b.1), so a student can reach unit 15 with 12
    behind them; "complete Phase 1" means the phase, not its last page.
    """
    target = parse_milestone(commitment.get("curriculum_milestone_target"))
    if target is None:
        return False

    try:
        units = db.schema("app").table("unit").select("id, unit_order").eq(
            "course_id", commitment["course_id"]).is_("deleted_at", "null").lte(
            "unit_order", target).execute()
        wanted = {u["id"] for u in units.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("milestone unit lookup failed: %s", exc)
        return False

    if not wanted:
        # No units at or below the target. The course has 12 units against a
        # 60-unit curriculum, so a target of 15 is genuinely unreachable today —
        # reported, never treated as satisfied.
        logger.info("milestone target %s has no units in course %s",
                    target, commitment["course_id"])
        return False

    try:
        progress = db.schema("app").table("unit_progress").select(
            "unit_id, status").eq("user_id", commitment["student_id"]).in_(
            "unit_id", list(wanted)).execute()
        done = {p["unit_id"] for p in progress.data or []
                if p.get("status") == "completed"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("milestone progress lookup failed: %s", exc)
        return False

    return wanted.issubset(done)


async def sweep_milestones() -> dict:
    """Daily. Stamp `milestone_reached_at` on commitments that have arrived.

    Only ever sets the timestamp, never clears it: a milestone is a thing that
    happened, and unsetting it because a progress row moved would take back an
    outcome-tied discount somebody already earned.
    """
    try:
        rows = db.schema("app").table("enrollment_commitment").select(
            "id, student_id, course_id, curriculum_milestone_target"
        ).eq("status", "active").is_("milestone_reached_at", "null").not_.is_(
            "curriculum_milestone_target", "null").execute()
        commitments = rows.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("milestone sweep could not read commitments: %s", exc)
        return {"checked": 0, "reached": 0}

    reached = 0
    for commitment in commitments:
        try:
            if await check_milestone(commitment):
                db.schema("app").table("enrollment_commitment").update({
                    "milestone_reached_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", commitment["id"]).execute()
                reached += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("milestone check failed for %s: %s", commitment["id"], exc)

    return {"checked": len(commitments), "reached": reached}


# ── Checkout ──────────────────────────────────────────────────────────────────

def _price_id(billing_mode: str) -> str:
    from app.config import settings
    price = {
        "prepay": settings.stripe_price_term_prepay,
        "installment": settings.stripe_price_term_installment,
    }.get(billing_mode, "")
    if not price:
        # D-18: placeholders. Refusing here, loudly and by name, beats letting
        # Stripe reject an empty price id with an error nobody can act on.
        raise CommitmentUnavailable(
            f"STRIPE_PRICE_TERM_{billing_mode.upper()} is not configured — the "
            "termly plan is not purchasable yet (plan D-18)."
        )
    return price


async def create_commitment_checkout(
    student_id: str,
    course_id: str,
    billing_mode: str,
    tier: str,
    locked_home_tutor_id: str | None,
    amount_minor: int,
    currency: str,
    customer_email: str,
    milestone_target: str | None = None,
    success_url: str | None = None,
    cancel_url: str | None = None,
) -> dict:
    """Open a term checkout and hold a `pending` commitment against it.

    The row is written **before** Stripe is called, so a completed payment always
    has somewhere to land. A checkout the student abandons leaves a `pending` row
    that never activates — visible, and cheaper than a webhook arriving with
    nothing to attach itself to.
    """
    if billing_mode not in BILLING_MODES:
        raise ValueError(f"billing_mode must be one of {BILLING_MODES}")

    price_id = _price_id(billing_mode)  # raises CommitmentUnavailable if unset

    term_start, term_end = term_window()
    installments = 1 if billing_mode == "prepay" else _as_int(
        _config(["commitment.term_months"])["commitment.term_months"], 3)

    try:
        created = db.schema("app").table("enrollment_commitment").insert({
            "student_id": student_id,
            "course_id": course_id,
            "term_start": term_start.isoformat(),
            "term_end": term_end.isoformat(),
            "locked_home_tutor_id": locked_home_tutor_id,
            "curriculum_milestone_target": milestone_target,
            "billing_mode": billing_mode,
            "amount_minor": amount_minor,
            "currency": currency.upper(),
            "installments_total": installments,
            "tier": tier,
            "status": "pending",
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "idx_enrollment_commitment_one_active" in str(exc):
            raise ValueError("This student already has a live commitment for this course")
        raise

    commitment_id = (created.data or [{}])[0].get("id")

    from app.config import settings
    from app.services.stripe_service import create_term_checkout

    session = await create_term_checkout(
        commitment_id=commitment_id,
        student_id=student_id,
        price_id=price_id,
        billing_mode=billing_mode,
        installments=installments,
        customer_email=customer_email,
        success_url=success_url or f"{settings.web_base_url}/account/subscription",
        cancel_url=cancel_url or f"{settings.web_base_url}/pricing",
    )

    try:
        db.schema("app").table("enrollment_commitment").update({
            "stripe_reference": session["session_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", commitment_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("could not attach the Stripe reference to %s: %s", commitment_id, exc)

    return {**session, "commitment_id": commitment_id,
            "term_start": term_start.isoformat(), "term_end": term_end.isoformat()}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def activate(commitment_id: str, stripe_reference: str | None = None) -> dict:
    """Called from the Stripe webhook when the term checkout completes.

    Grants the tier the commitment names — `change_tier(..., 'commitment')` —
    which is how "what you can use" stays in `student.tier` while "what you
    promised" stays here.

    Idempotent: a webhook Stripe replays must not double-grant, and re-running
    `change_tier` on an already-committed student would overwrite `original_tier`
    with the tier they are already on.
    """
    from app.services.tier_service import change_tier

    try:
        result = db.schema("app").table("enrollment_commitment").select("*").eq(
            "id", commitment_id).limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.error("activate could not read commitment %s: %s", commitment_id, exc)
        return {"activated": False, "reason": "read_failed"}

    if not row:
        return {"activated": False, "reason": "no_such_commitment"}
    if row["status"] == "active":
        return {"activated": False, "reason": "already_active", "commitment_id": commitment_id}
    if row["status"] not in ("pending",):
        return {"activated": False, "reason": f"commitment is {row['status']}"}

    now = datetime.now(timezone.utc).isoformat()
    patch = {"status": "active", "activated_at": now, "updated_at": now}
    if stripe_reference:
        patch["stripe_reference"] = stripe_reference
    if row["billing_mode"] == "prepay":
        patch["installments_paid"] = row["installments_total"]
    else:
        # The first instalment is the checkout itself; the rest arrive as
        # `invoice.paid`.
        patch["installments_paid"] = max(1, int(row.get("installments_paid") or 0))

    db.schema("app").table("enrollment_commitment").update(patch).eq(
        "id", commitment_id).execute()

    await change_tier(row["student_id"], row["tier"], "commitment")

    logger.info("Commitment %s activated for %s (%s, %s)",
                commitment_id, row["student_id"], row["tier"], row["billing_mode"])
    return {"activated": True, "commitment_id": commitment_id,
            "tier": row["tier"], "student_id": row["student_id"]}


async def record_installment(stripe_reference: str) -> dict:
    """An instalment landed (`invoice.paid` on the term subscription).

    Counts up to `installments_total` and no further. The CHECK would refuse an
    overshoot anyway; clamping here means a duplicated webhook is a no-op rather
    than a 500 in the Stripe delivery log, which is what makes Stripe retry it.
    """
    try:
        result = db.schema("app").table("enrollment_commitment").select(
            "id, installments_paid, installments_total, status"
        ).eq("stripe_reference", stripe_reference).limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("installment lookup failed for %s: %s", stripe_reference, exc)
        return {"recorded": False, "reason": "read_failed"}

    if not row:
        return {"recorded": False, "reason": "no_such_commitment"}

    paid = int(row.get("installments_paid") or 0)
    total = int(row["installments_total"])
    if paid >= total:
        return {"recorded": False, "reason": "already_complete",
                "installments_paid": paid, "installments_total": total}

    now = datetime.now(timezone.utc).isoformat()
    patch = {"installments_paid": paid + 1, "updated_at": now}
    # The last instalment does NOT complete the commitment — the term does. A
    # student who has finished paying in month 3 of a 3-month term still has the
    # rest of that term, and the guarantee runs with it.
    db.schema("app").table("enrollment_commitment").update(patch).eq(
        "id", row["id"]).execute()

    return {"recorded": True, "commitment_id": row["id"],
            "installments_paid": paid + 1, "installments_total": total}


async def expire_finished_terms(today: date | None = None) -> dict:
    """Daily. Move commitments whose term has ended to `completed`.

    **Does not touch the tier.** Whether a student drops back after a term is a
    billing question — their monthly subscription may well continue — and this
    job silently downgrading paying customers would be the worst possible way to
    find that out. The tier change belongs to the Stripe lifecycle, not here.
    """
    today = today or datetime.now(timezone.utc).date()
    try:
        result = db.schema("app").table("enrollment_commitment").select("id").eq(
            "status", "active").lt("term_end", today.isoformat()).execute()
        ids = [r["id"] for r in result.data or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("term expiry sweep failed: %s", exc)
        return {"completed": 0}

    if not ids:
        return {"completed": 0}

    now = datetime.now(timezone.utc).isoformat()
    db.schema("app").table("enrollment_commitment").update({
        "status": "completed", "updated_at": now,
    }).in_("id", ids).execute()
    return {"completed": len(ids)}


async def cancel(commitment_id: str, actor_id: str, reason: str | None = None) -> dict:
    """Cancel a commitment. **Admin-invoked only.**

    `commitment.self_cancel` is `'none'` and the route is admin-authenticated:
    a termly plan is sold on a guarantee that runs both ways, and a self-serve
    cancel button would need a refund policy that does not exist yet.

    The tier is deliberately **not** reverted here for the same reason
    `expire_finished_terms` does not: the refund and the entitlement are separate
    decisions, and a cancel that silently downgrades someone mid-conversation is
    a support incident.
    """
    try:
        result = db.schema("app").table("enrollment_commitment").select(
            "id, status, student_id").eq("id", commitment_id).limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not read commitment: {exc}") from exc

    if not row:
        return {"cancelled": False, "reason": "no_such_commitment"}
    if row["status"] not in ("pending", "active"):
        return {"cancelled": False, "reason": f"commitment is {row['status']}"}

    now = datetime.now(timezone.utc).isoformat()
    db.schema("app").table("enrollment_commitment").update({
        "status": "cancelled", "cancelled_at": now, "updated_at": now,
    }).eq("id", commitment_id).execute()

    logger.info("Commitment %s cancelled by %s (%s)", commitment_id, actor_id, reason)
    return {"cancelled": True, "commitment_id": commitment_id,
            "tier_unchanged": True,
            "note": "The tier was not reverted — that is a separate billing decision."}


# ── Reads ─────────────────────────────────────────────────────────────────────

async def get_active(student_id: str) -> dict | None:
    """The student's live commitment, if any — what §2.3's screen renders."""
    try:
        result = db.schema("app").table("enrollment_commitment").select("*").eq(
            "student_id", student_id).in_("status", ["pending", "active"]).order(
            "created_at", desc=True).limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("active commitment read failed for %s: %s", student_id, exc)
        return None
    if not row:
        return None

    target = parse_milestone(row.get("curriculum_milestone_target"))
    return {
        **row,
        "milestone_target_unit_order": target,
        "milestone_reached": row.get("milestone_reached_at") is not None,
        "days_remaining": _days_remaining(row.get("term_end")),
    }


def _days_remaining(term_end: str | None) -> int | None:
    if not term_end:
        return None
    try:
        end = date.fromisoformat(str(term_end)[:10])
    except ValueError:
        return None
    return max(0, (end - datetime.now(timezone.utc).date()).days)


def locked_tutor_for(student_id: str) -> str | None:
    """The tutor a live commitment protects, for the matcher to honour.

    §1.3: *"the matching engine must treat this as a non-negotiable constraint
    (student is never reassigned off their home tutor mid-term except for
    tutor-side unavailability — see 1.7 warm handoff)."*

    Returns None when there is no commitment, which is the ordinary case and must
    read as "no constraint", never as "no tutor".
    """
    try:
        result = db.schema("app").table("enrollment_commitment").select(
            "locked_home_tutor_id, term_end"
        ).eq("student_id", student_id).eq("status", "active").limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("locked tutor lookup failed for %s: %s", student_id, exc)
        # None, not a raise. A failed read must not silently drop the guarantee,
        # so the caller logs it — but it also must not block an assignment
        # outright, or one bad query freezes all matching.
        return None
    return (row or {}).get("locked_home_tutor_id")
