"""The payout rail — plan Phase 3a, spec §1.4.

**NOTHING HERE MOVES MONEY.** A run is computed and left for a human to approve;
`payout_line.external_ref` is filled in with the reference an admin pastes after
paying. §11.4: *"a correct ledger with a human pressing send is strictly better
than an automated rail built for one tutor."* Automation is 3b and is blocked on
O-06 (which rail first).

Four jobs live here, and the interesting decisions are in three of them:

- **`recompute_rates`** — the rate stops being a static field and becomes
  `base × tenure × quality`, versioned so a past payout stays auditable against
  the rate that applied at the time (§1.4).
- **`recompute_vesting`** — the referring tutor's share of a student they brought,
  rising with how long that student stays.
- **`compute_payout_run`** — what each tutor is owed for a period, from sessions
  they actually **delivered**.
- **`reconcile`** — the float fold against `balance_after_minor`, plus the
  low-balance alert.

MONEY IS `*_minor` PLUS AN EXPLICIT CURRENCY. D-17 settled a rate card per
country, so "cents" is simply wrong for a Ugandan shilling. Integer minor units,
never a float — the original rule's intent, generalised.

WHAT COUNTS AS A DELIVERED SESSION. Not `app.tutor_sessions`, which has 0 rows
and no writer. Phase 2 created the two real sources:
`app.cohort_session.lead_tutor_id` (the rota — who *led* the occurrence, which
may be a guest) and `app.one_to_one_session.tutor_id`. §1.4 requires the
**delivering** tutor to be paid, and those two columns are what "delivering"
means now.
"""

import calendar
import json
import logging
from datetime import date, datetime, timedelta, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# Make-ups and clinics are paid rate x minutes / this (a pod session's length).
# Developer decision 2026-10-18 (enrolment plan §2f item 6).
PRO_RATA_BASE_MINUTES = 50

_DEFAULTS = {
    "payout.cadence_day": "1",
    "payout.float_low_balance_minor": '{"USD":50000}',
    "payout.vesting_schedule_bp": '{"0":0,"3":250,"6":500,"12":1000}',
    "payout.vesting_min_tenure_days": "90",
    "payout.default_currency": "USD",
    # Plan 8.4 (§134): what each kind of work is worth against the rate card's
    # per-session rate, in basis points. Make-ups and clinics stay pro-rata.
    "payout.type_rates_bp": '{"one_to_one_25":5000,"review_item":1000,"gathering":15000,'
                            '"qa_observation":5000,"other":10000}',
    # Plan 8.4: a monthly connectivity stipend per currency, paid to anyone who
    # delivered work in the period. Empty = none.
    "payout.connectivity_stipend_minor": "{}",
    # Plan 8.5 / E-11: 'commission' = a flat share of what a referred learner
    # paid, for 12 months from signup; 'vesting' = the older tenure share.
    "payout.referral_rule": "commission",
    "payout.referral_commission_bp": "1000",
    "payout.referral_commission_months": "12",
}

# Tenure is measured in whole months of continuous platform membership. The
# ladder is deliberately coarse and flat-topped: a rate that keeps climbing
# forever is a promise nobody costed, and the loyalty effect §1.4 is after comes
# almost entirely from the first year.
TENURE_LADDER_BP = ((0, 10000), (6, 10500), (12, 11000), (24, 11500))

# Quality multiplies on the tutor's mean star rating. **A tutor with too few
# ratings gets 1.0, not a penalty** — a single bad night from a student having a
# bad day must not cut someone's pay, and §3.5 permits ratings as an input
# precisely because they are not telemetry.
MIN_RATINGS_FOR_QUALITY = 5
QUALITY_LADDER_BP = ((0.0, 9500), (3.5, 10000), (4.3, 10500), (4.7, 11000))


# ── Config ────────────────────────────────────────────────────────────────────

def _config(keys: list[str] | None = None) -> dict[str, str]:
    wanted = keys or list(_DEFAULTS)
    values = dict(_DEFAULTS)
    try:
        result = db.schema("app").table("config").select("key, value").in_("key", wanted).execute()
        for row in result.data or []:
            if row.get("value") not in (None, ""):
                values[row["key"]] = row["value"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("payout config read failed, using defaults: %s", exc)
    return values


def _as_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _as_json(value, fallback: dict) -> dict:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else fallback
    except (TypeError, ValueError):
        return fallback


def is_cadence_day(today: date | None = None) -> bool:
    """The guard every monthly job sits behind.

    `/tasks/daily` fires every day; these jobs must fire once a month. The day
    comes from `payout.cadence_day` so a run can be moved without a deploy, and
    is clamped into the month so a value of 31 does not skip February entirely.
    """
    today = today or datetime.now(timezone.utc).date()
    wanted = _as_int(_config(["payout.cadence_day"])["payout.cadence_day"], 1)
    last = calendar.monthrange(today.year, today.month)[1]
    return today.day == max(1, min(wanted, last))


def previous_period(today: date | None = None) -> tuple[date, date]:
    """The month that just ended — what a run on the 1st is paying for.

    Computed from the *first* of this month backwards rather than by subtracting
    30 days, which drifts and would eventually pay one month twice.
    """
    today = today or datetime.now(timezone.utc).date()
    first_this = today.replace(day=1)
    end = first_this - timedelta(days=1)
    return end.replace(day=1), end


# ── Rate computation ──────────────────────────────────────────────────────────

def _ladder_bp(value: float, ladder: tuple) -> int:
    """The highest band whose threshold `value` reaches."""
    result = ladder[0][1]
    for threshold, bp in ladder:
        if value >= threshold:
            result = bp
    return result


def tenure_months(created_at: str | None, asof: date) -> int:
    started = _parse_date(created_at)
    if started is None:
        return 0
    return max(0, (asof.year - started.year) * 12 + (asof.month - started.month))


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def quality_inputs(tutor_id: str) -> dict:
    """Mean star rating across this tutor's sessions, and how many there were.

    Reads `app.video_session_rating`, which §3.5 explicitly permits as a payout
    input. **It must never read `app.activity_summary` or `app.ai_usage`** —
    `CLAUDE.md` forbids gating anything on a telemetry number, and pay is the
    strongest gate there is.
    """
    try:
        sessions = db.schema("app").table("video_session").select("id").eq(
            "tutor_id", tutor_id).execute()
        ids = [s["id"] for s in sessions.data or []]
        if not ids:
            return {"mean_stars": None, "rating_count": 0}
        ratings = db.schema("app").table("video_session_rating").select(
            "stars").in_("session_id", ids).execute()
        stars = [int(r["stars"]) for r in ratings.data or [] if r.get("stars") is not None]
    except Exception as exc:  # noqa: BLE001
        logger.warning("quality inputs failed for %s: %s", tutor_id, exc)
        return {"mean_stars": None, "rating_count": 0}

    if not stars:
        return {"mean_stars": None, "rating_count": 0}
    return {"mean_stars": round(sum(stars) / len(stars), 2), "rating_count": len(stars)}


def _current_rate_card(country_code: str, asof: date) -> dict | None:
    try:
        result = db.schema("app").table("rate_card").select("*").eq(
            "country_code", country_code).lte(
            "effective_from", asof.isoformat()).order(
            "effective_from", desc=True).limit(1).execute()
        row = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("rate card lookup failed for %s: %s", country_code, exc)
        return None
    if not row:
        return None
    ends = _parse_date(row.get("effective_to"))
    return None if (ends and ends <= asof) else row


def compute_rate(tutor: dict, asof: date) -> dict | None:
    """`base × tenure × quality`, with the inputs kept.

    Returns None when the tutor cannot be rated — no country, or no card for it.
    **That is reported, not defaulted.** Guessing a country means paying someone
    a rate from a place they do not live in, and a silent fallback would make
    that invisible.
    """
    country = (tutor.get("country_code") or "").strip().upper()
    if not country:
        return None
    card = _current_rate_card(country, asof)
    if not card:
        return None

    months = tenure_months(tutor.get("created_at"), asof)
    tenure_bp = _ladder_bp(months, TENURE_LADDER_BP)

    q = quality_inputs(tutor["id"])
    if q["rating_count"] >= MIN_RATINGS_FOR_QUALITY and q["mean_stars"] is not None:
        quality_bp = _ladder_bp(q["mean_stars"], QUALITY_LADDER_BP)
    else:
        # Not enough evidence is not evidence of poor quality.
        quality_bp = 10000

    base = int(card["base_rate_minor"])
    # Integer arithmetic throughout: two bp multipliers applied to minor units,
    # divided once at the end. Multiplying floats here is how a rate ends up a
    # fraction of a shilling different every month.
    computed = (base * tenure_bp * quality_bp) // (10000 * 10000)

    return {
        "tutor_id": tutor["id"],
        "rate_card_id": card["id"],
        "country_code": country,
        "currency": card["currency"],
        "base_rate_minor": base,
        "tenure_multiplier_bp": tenure_bp,
        "quality_multiplier_bp": quality_bp,
        "computed_rate_minor": computed,
        "effective_from": asof.replace(day=1).isoformat(),
        # The snapshot that makes a past rate explicable rather than merely
        # reproducible — the numbers it was derived from, at the time.
        "inputs": {
            "tenure_months": months,
            "mean_stars": q["mean_stars"],
            "rating_count": q["rating_count"],
            "half_session_bp": card.get("half_session_bp", 5000),
            "computed_for": asof.isoformat(),
        },
    }


async def recompute_rates(asof: date | None = None) -> dict:
    """Monthly. One `tutor_rate_version` per active tutor, previous one closed."""
    asof = asof or datetime.now(timezone.utc).date()
    effective_from = asof.replace(day=1)

    try:
        tutors = db.schema("app").table("tutor").select(
            "id, created_at, country_code, status, deleted_at"
        ).eq("status", "active").is_("deleted_at", "null").execute()
        rows = tutors.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("recompute_rates could not read tutors: %s", exc)
        return {"computed": 0, "skipped": 0, "error": str(exc)}

    computed = skipped = 0
    unrateable: list[str] = []

    for tutor in rows:
        rate = compute_rate(tutor, asof)
        if rate is None:
            skipped += 1
            unrateable.append(tutor["id"])
            continue
        try:
            # Close the open version first. If the insert then loses the
            # idempotence race the tutor is briefly left with no open version,
            # which is visible and recoverable; the reverse order would leave two
            # open versions, and "what is their rate" would have two answers.
            db.schema("app").table("tutor_rate_version").update({
                "effective_to": effective_from.isoformat(),
            }).eq("tutor_id", tutor["id"]).is_("effective_to", "null").lt(
                "effective_from", effective_from.isoformat()).execute()

            db.schema("app").table("tutor_rate_version").insert(rate).execute()
            computed += 1
        except Exception as exc:  # noqa: BLE001
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                # Already computed for this month. A retried cron run is a
                # no-op, which is the point of the unique index.
                skipped += 1
                continue
            logger.error("rate recompute failed for %s: %s", tutor["id"], exc)
            skipped += 1

    if unrateable:
        # Loud, because an unrateable tutor is an unpayable tutor and the cause
        # is always a missing country or a missing rate card — both fixable in a
        # minute by someone who knows.
        logger.warning(
            "%d tutor(s) have no applicable rate card (missing country_code or "
            "no card for it): %s", len(unrateable), ", ".join(unrateable[:10]))

    return {"computed": computed, "skipped": skipped,
            "unrateable": len(unrateable), "effective_from": effective_from.isoformat()}


# ── Referral vesting ──────────────────────────────────────────────────────────

async def recompute_vesting(asof: date | None = None) -> dict:
    """Monthly. `referral_signup.vested_share_bp` from how long the student stayed.

    §1.4: *"new field(s) tracking how long a given student has remained assigned
    to a tutor via referral, with the tutor's revenue share on that student
    increasing over time — needs a scheduled recalculation job, not just a static
    split."*

    Two rules make this defensible rather than exploitable:

    - **A minimum tenure before anything vests at all.** Plan §8 names referral
      fraud — *"a tutor referring students they control, to collect a vesting
      share"* — and says a minimum-tenure gate covers more of it than the
      concentration cap does.
    - **Vesting only ratchets up.** A student who lapses and returns does not
      restart the tutor's share at zero, and a bad month for one student cannot
      claw back a share already earned.
    """
    asof = asof or datetime.now(timezone.utc).date()
    cfg = _config()
    schedule = {int(k): int(v) for k, v in
                _as_json(cfg["payout.vesting_schedule_bp"], {"0": 0}).items()}
    min_days = _as_int(cfg["payout.vesting_min_tenure_days"], 90)

    try:
        signups = db.schema("app").table("referral_signup").select(
            "id, student_id, tutor_id, signed_up_at, vested_share_bp"
        ).execute()
        rows = signups.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("recompute_vesting could not read signups: %s", exc)
        return {"updated": 0, "error": str(exc)}

    active_students = _active_student_ids([r["student_id"] for r in rows])

    updated = 0
    for row in rows:
        signed = _parse_date(row.get("signed_up_at"))
        if signed is None:
            continue

        # A student who has left stops accruing. The share already vested stays —
        # the tutor did bring them — but it does not keep climbing on someone who
        # is gone.
        if row["student_id"] not in active_students:
            continue

        days = (asof - signed).days
        if days < min_days:
            continue

        months = max(0, (asof.year - signed.year) * 12 + (asof.month - signed.month))
        bp = 0
        for threshold in sorted(schedule):
            if months >= threshold:
                bp = schedule[threshold]

        current = int(row.get("vested_share_bp") or 0)
        if bp <= current:
            continue

        try:
            db.schema("app").table("referral_signup").update({
                "vested_share_bp": bp,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["id"]).execute()
            updated += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("vesting update failed for %s: %s", row["id"], exc)

    return {"updated": updated, "considered": len(rows),
            "min_tenure_days": min_days, "asof": asof.isoformat()}


def _active_student_ids(ids: list[str]) -> set[str]:
    if not ids:
        return set()
    try:
        result = db.schema("app").table("student").select(
            "id, account_status, deleted_at").in_("id", list(set(ids))).execute()
        return {r["id"] for r in result.data or []
                if r.get("deleted_at") is None and r.get("account_status") == "active"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("active student lookup failed: %s", exc)
        # Empty, not "all". Treating a failed read as "everyone is active" would
        # keep vesting shares climbing on students who may have left.
        return set()


# ── Delivered sessions ────────────────────────────────────────────────────────

def delivered_sessions(period_start: date, period_end: date) -> dict[str, dict]:
    """`{tutor_id: {"cohort", "one_to_one", "minutes", "students": {id: n}}}`.

    Both lanes, keyed on who **delivered**:
    `cohort_session.lead_tutor_id` is the rota's answer to "who led this
    occurrence" and may be a guest rather than the cohort's own tutor;
    `one_to_one_session.tutor_id` is the tutor the student booked.

    `app.tutor_sessions` is deliberately not read: it has no writer and 0 rows,
    and counting from it would produce a run of zeroes that looks like a working
    job.

    **`students` exists so the referral share is computable.** The share is a
    percentage of what a *referred student's* sessions cost, so a per-tutor total
    is not enough — the run has to know which students were taught, by whom.
    """
    frm = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    to = datetime.combine(period_end + timedelta(days=1), datetime.min.time(),
                          tzinfo=timezone.utc)
    out: dict[str, dict] = {}

    def entry(tutor_id: str) -> dict:
        return out.setdefault(
            tutor_id, {"cohort": 0, "one_to_one": 0, "makeup": 0, "clinic": 0,
                       "minutes": 0, "pro_rata_minutes": 0, "students": {}})

    # ── the cohort lane ──────────────────────────────────────────────────────
    cohort_rows: list[dict] = []
    try:
        result = db.schema("app").table("cohort_session").select(
            "cohort_id, lead_tutor_id, duration_minutes, status"
        ).gte("scheduled_at", frm.isoformat()).lt(
            "scheduled_at", to.isoformat()).execute()
        # `completed` only. A `scheduled` session in the past is a session nobody
        # marked, and paying for it would pay for attendance that was never
        # confirmed — the cohort domain's "absence is never inferred" rule,
        # applied to money.
        cohort_rows = [r for r in (result.data or [])
                       if r.get("lead_tutor_id") and r.get("status") == "completed"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohort session read failed: %s", exc)

    members = _cohort_members({r["cohort_id"] for r in cohort_rows if r.get("cohort_id")})
    for r in cohort_rows:
        e = entry(r["lead_tutor_id"])
        e["cohort"] += 1
        e["minutes"] += int(r.get("duration_minutes") or 50)
        for student_id in members.get(r.get("cohort_id"), ()):
            e["students"][student_id] = e["students"].get(student_id, 0) + 1

    # ── the 1-on-1 lane ──────────────────────────────────────────────────────
    try:
        result = db.schema("app").table("one_to_one_session").select(
            "tutor_id, student_id, duration_minutes, status"
        ).gte("scheduled_at", frm.isoformat()).lt(
            "scheduled_at", to.isoformat()).execute()
        for r in result.data or []:
            if r.get("status") != "completed":
                continue
            e = entry(r["tutor_id"])
            e["one_to_one"] += 1
            # Plan 8.4: a 25-minute 1:1 is priced as its own type.
            if int(r.get("duration_minutes") or 50) == 25:
                e["one_to_one_25"] = e.get("one_to_one_25", 0) + 1
            e["minutes"] += int(r.get("duration_minutes") or 50)
            if r.get("student_id"):
                e["students"][r["student_id"]] = e["students"].get(r["student_id"], 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("one-to-one session read failed: %s", exc)

    # ── the make-up and clinic lanes (§2f item 6, plan 1.5) ──────────────────
    # Paid PRO-RATA by minutes (developer 2026-10-18): rate x minutes / 50. Only
    # `completed` — a make-up completes when every seat is marked (§103); a
    # clinic when every booking is, with delivered_minutes 15 for the 1:1
    # check-in fallback (§105). Neither feeds `students`: the referral share is
    # a per-session share and stays on the pod and 1-on-1 lanes.
    try:
        result = db.schema("app").table("makeup_session").select(
            "tutor_id, duration_minutes, status"
        ).gte("scheduled_at", frm.isoformat()).lt("scheduled_at", to.isoformat()).execute()
        for r in result.data or []:
            if r.get("status") != "completed" or not r.get("tutor_id"):
                continue
            e = entry(r["tutor_id"])
            e["makeup"] += 1
            e["minutes"] += int(r["duration_minutes"])
            e["pro_rata_minutes"] += int(r["duration_minutes"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("make-up session read failed: %s", exc)

    try:
        result = db.schema("app").table("clinic").select(
            "tutor_id, co_tutor_id, delivered_minutes, status"
        ).gte("starts_at", frm.isoformat()).lt("starts_at", to.isoformat()).execute()
        for r in result.data or []:
            if r.get("status") != "completed" or not r.get("delivered_minutes"):
                continue
            # Track 10 (§140): a two-tutor clinic pays both tutors.
            for t in {r["tutor_id"], r.get("co_tutor_id")} - {None}:
                e = entry(t)
                e["clinic"] += 1
                e["minutes"] += int(r["delivered_minutes"])
                e["pro_rata_minutes"] += int(r["delivered_minutes"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("clinic read failed: %s", exc)

    # ── per-item work (plan 8.4, §134) ───────────────────────────────────────
    # Fidel readings reviewed (1.4) and work ops recorded (gatherings, QA).
    try:
        result = db.schema("app").table("fidel_approval").select(
            "reviewed_by, reviewed_at, status"
        ).gte("reviewed_at", frm.isoformat()).lt("reviewed_at", to.isoformat()).execute()
        for r in result.data or []:
            if r.get("reviewed_by") and r.get("status") in ("approved", "redo"):
                e = entry(r["reviewed_by"])
                e["review_item"] = e.get("review_item", 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("review read failed: %s", exc)
    try:
        result = db.schema("app").table("tutor_work_item").select(
            "tutor_id, kind, quantity, occurred_on"
        ).gte("occurred_on", period_start.isoformat()).lte("occurred_on", period_end.isoformat()).execute()
        for r in result.data or []:
            e = entry(r["tutor_id"])
            e[r["kind"]] = e.get(r["kind"], 0) + int(r.get("quantity") or 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("work item read failed: %s", exc)

    return out


def _cohort_members(cohort_ids: set[str]) -> dict[str, list[str]]:
    """Active members per cohort, batched.

    A cohort session is delivered to everyone in the cohort, so a referred
    student in a cohort of four earns their referrer a share of one quarter of
    the sessions' worth — see `_referral_share_minor`, which divides by the
    cohort size for exactly this reason.
    """
    if not cohort_ids:
        return {}
    try:
        result = db.schema("app").table("cohort_memberships").select(
            "cohort_id, user_id").in_("cohort_id", list(cohort_ids)).eq(
            "status", "active").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohort membership read failed: %s", exc)
        return {}
    out: dict[str, list[str]] = {}
    for row in result.data or []:
        out.setdefault(row["cohort_id"], []).append(row["user_id"])
    return out


# ── The payout run ────────────────────────────────────────────────────────────

def _current_rate_versions(tutor_ids: list[str]) -> dict[str, dict]:
    if not tutor_ids:
        return {}
    try:
        result = db.schema("app").table("tutor_rate_version").select("*").in_(
            "tutor_id", tutor_ids).is_("effective_to", "null").execute()
        return {r["tutor_id"]: r for r in result.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("rate version lookup failed: %s", exc)
        return {}


def _referral_share_minor(tutor_id: str, sessions_by_tutor: dict[str, dict],
                          rates: dict[str, dict], currency: str) -> int:
    """What this tutor earns from students they referred but did not teach.

    §3.1b: *"two different tutors can be owed money from one purchased session"* —
    the delivering tutor for teaching it, the referring tutor for the share. This
    computes the second.

    The share is `vested_share_bp` of what that student's sessions cost at the
    **delivering** tutor's rate. A cohort session is delivered to the whole
    cohort, so its cost is divided by the number of students in it — otherwise
    one session with four members would pay four full shares.

    **Only within one currency.** A share against a line denominated in KES
    cannot be added to a EUR run, and converting here would apply an FX rate at
    computation time rather than at settlement, which §11.3 forbids.
    Cross-currency pairs are skipped, and counted so the skip is visible.
    """
    try:
        signups = db.schema("app").table("referral_signup").select(
            "student_id, vested_share_bp"
        ).eq("tutor_id", tutor_id).gt("vested_share_bp", 0).execute()
        rows = signups.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral share read failed for %s: %s", tutor_id, exc)
        return 0
    if not rows:
        return 0

    by_student = {r["student_id"]: int(r["vested_share_bp"]) for r in rows}
    total = 0
    skipped_currency = 0

    for delivering, counts in sessions_by_tutor.items():
        # Their own teaching is paid on their own line. Taking a referral share
        # of it as well would pay the same person twice for one session.
        if delivering == tutor_id:
            continue
        rate = rates.get(delivering)
        if not rate:
            continue
        if rate["currency"] != currency:
            skipped_currency += 1
            continue

        per_session = int(rate["computed_rate_minor"])
        cohort_size = max(1, len(counts.get("students", {})) or 1)

        for student_id, n in counts.get("students", {}).items():
            bp = by_student.get(student_id)
            if not bp:
                continue
            # Integer arithmetic, divided once. `cohort_size` is 1 for a 1-on-1.
            total += (per_session * n * bp) // (10000 * cohort_size)

    if skipped_currency:
        logger.info("referral share for %s skipped %d cross-currency tutor(s)",
                    tutor_id, skipped_currency)
    return total


async def compute_payout_run(period_start: date | None = None,
                             period_end: date | None = None) -> dict:
    """Compute one run per currency for the period. Leaves them **unapproved**.

    **Deviation from the plan's wording, stated plainly.** §3a says runs are
    "computed and left in `approved`". A run reaches `approved` only with an
    `approved_by` — the schema requires it — and a machine writing that column
    would be fabricating an approver for a payment. The job therefore stops at
    `planned`, and `approve_run()` is a separate, admin-authenticated step. The
    plan's intent (the pipeline halts before money moves) is unchanged; only who
    signs is.
    """
    if period_start is None or period_end is None:
        period_start, period_end = previous_period()

    sessions = delivered_sessions(period_start, period_end)
    if not sessions:
        return {"runs": [], "reason": "no_delivered_sessions",
                "period": [period_start.isoformat(), period_end.isoformat()]}

    rates = _current_rate_versions(list(sessions))
    unrateable = [t for t in sessions if t not in rates]

    # One run per currency: mixing currencies would make `total_minor`
    # meaningless, the same rule as the ledger fold (§11.3).
    by_currency: dict[str, list[str]] = {}
    for tutor_id in sessions:
        rate = rates.get(tutor_id)
        if rate:
            by_currency.setdefault(rate["currency"], []).append(tutor_id)

    cfg = _config()
    type_bp = {k: int(v) for k, v in _as_json(cfg["payout.type_rates_bp"], {}).items()}
    stipends = {k: int(v) for k, v in _as_json(cfg["payout.connectivity_stipend_minor"], {}).items()}
    rule = cfg.get("payout.referral_rule") or "commission"
    c_bp = _as_int(cfg.get("payout.referral_commission_bp"), 1000)
    c_months = _as_int(cfg.get("payout.referral_commission_months"), 12)

    # E-11: a referring tutor is owed commission even in a month they taught
    # nothing, so they get a line too (if they have a rate, for its currency).
    if rule == "commission":
        try:
            referrers = {r["tutor_id"] for r in (db.schema("app").table("referral_signup").select("tutor_id")
                         .execute()).data or []}
        except Exception as exc:  # noqa: BLE001
            logger.warning("referrer read failed: %s", exc)
            referrers = set()
        extra = [t for t in referrers if t not in sessions]
        if extra:
            for t, r in _current_rate_versions(extra).items():
                rates[t] = r
                sessions.setdefault(t, {"cohort": 0, "one_to_one": 0, "makeup": 0, "clinic": 0,
                                        "minutes": 0, "pro_rata_minutes": 0, "students": {}})
                by_currency.setdefault(r["currency"], []).append(t)

    created = []
    for currency, tutor_ids in by_currency.items():
        run_id = _ensure_run(period_start, period_end, currency)
        if run_id is None:
            continue

        total = 0
        for tutor_id in tutor_ids:
            rate = rates[tutor_id]
            counts = sessions[tutor_id]
            n = (counts["cohort"] + counts["one_to_one"]
                 + counts.get("makeup", 0) + counts.get("clinic", 0))
            per = int(rate["computed_rate_minor"])
            amount = line_amount(counts, per, type_bp)
            worked = n + sum(counts.get(k, 0) for k in ("review_item", "gathering", "qa_observation", "other"))
            stipend = stipends.get(currency, 0) if worked else 0
            if rule == "commission":
                share = _referral_commission_minor(tutor_id, period_start, period_end, currency, c_bp, c_months)
            else:
                share = _referral_share_minor(tutor_id, sessions, rates, currency)
            line_total = amount + stipend + share
            if line_total <= 0 and not worked:
                continue
            total += line_total
            breakdown = {"sessions_minor": amount, "stipend_minor": stipend, "referral_minor": share,
                         "referral_rule": rule, "rate_minor": per,
                         "counts": {k: v for k, v in counts.items() if k != "students" and v}}
            _upsert_line(run_id, tutor_id, currency, line_total, rate["id"], n, share, breakdown)

        try:
            db.schema("app").table("payout_run").update({
                "total_minor": total,
                "tutor_count": len(tutor_ids),
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "status": "planned",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", run_id).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("could not finalise run %s: %s", run_id, exc)

        created.append({"run_id": run_id, "currency": currency,
                        "total_minor": total, "tutor_count": len(tutor_ids)})

    if unrateable:
        logger.warning("%d tutor(s) delivered sessions but have no rate version "
                       "and were left out of the run: %s",
                       len(unrateable), ", ".join(unrateable[:10]))

    return {"runs": created, "unrateable": unrateable,
            "period": [period_start.isoformat(), period_end.isoformat()]}


def line_amount(counts: dict, per_session: int, type_bp: dict | None = None) -> int:
    """What a tutor's delivered sessions are worth, in minor units.

    Pod and 1-on-1 sessions at the flat per-session rate; make-ups and clinics
    pro-rata by minutes against a 50-minute session (developer 2026-10-18),
    rounded down once over the total — integers throughout, never float.

    Plan 8.4: with `type_bp`, a 25-minute 1:1, a reviewed reading, a gathering
    and other work items are priced in basis points of the per-session rate.
    """
    flat = per_session * (counts.get("cohort", 0) + counts.get("one_to_one", 0))
    amount = flat + (per_session * counts.get("pro_rata_minutes", 0)) // PRO_RATA_BASE_MINUTES
    if type_bp:
        # The 25-minute ones were counted at the full rate above; take the rest off.
        amount -= (per_session * counts.get("one_to_one_25", 0) * (10000 - type_bp.get("one_to_one_25", 10000))) // 10000
        for kind in ("review_item", "gathering", "qa_observation", "other"):
            amount += (per_session * counts.get(kind, 0) * type_bp.get(kind, 0)) // 10000
    return max(0, amount)


def _referral_commission_minor(tutor_id: str, period_start: date, period_end: date, currency: str,
                               bp: int, months: int) -> int:
    """E-11 (plan 8.5): a flat share of what each learner this tutor referred
    PAID in the period, for `months` from their signup. Same currency only."""
    try:
        signups = (db.schema("app").table("referral_signup").select("student_id, signed_up_at")
                   .eq("tutor_id", tutor_id).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral read failed for %s: %s", tutor_id, exc)
        return 0
    if not signups:
        return 0
    until = {s["student_id"]: _parse_date(s.get("signed_up_at")) for s in signups}
    frm = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    to = datetime.combine(period_end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).isoformat()
    try:
        pays = (db.schema("app").table("student_payment").select("student_id, amount_minor, currency, paid_at")
                .in_("student_id", list(until)).gte("paid_at", frm).lt("paid_at", to).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment read failed for %s: %s", tutor_id, exc)
        return 0
    total = 0
    for p in pays:
        signed = until.get(p["student_id"])
        paid = _parse_date(p.get("paid_at"))
        if not signed or not paid or p.get("currency") != currency:
            continue
        months_in = (paid.year - signed.year) * 12 + (paid.month - signed.month) - (1 if paid.day < signed.day else 0)
        if 0 <= months_in < months:
            total += int(p["amount_minor"])
    return (total * bp) // 10000


def _ensure_run(period_start: date, period_end: date, currency: str) -> str | None:
    """Get or create the run. The unique index is what makes a retry safe."""
    try:
        existing = db.schema("app").table("payout_run").select("id, status").eq(
            "period_start", period_start.isoformat()).eq(
            "period_end", period_end.isoformat()).eq("currency", currency).execute()
        row = (existing.data or [None])[0]
        if row:
            # Never recompute a run somebody has already approved or paid. The
            # numbers behind it are what they signed off on.
            if row["status"] not in ("planned", "computing", "failed"):
                logger.info("run %s is %s — not recomputing", row["id"], row["status"])
                return None
            return row["id"]

        created = db.schema("app").table("payout_run").insert({
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "currency": currency,
            "status": "computing",
        }).execute()
        return (created.data or [{}])[0].get("id")
    except Exception as exc:  # noqa: BLE001
        logger.error("could not open a run for %s %s: %s", period_start, currency, exc)
        return None


def _upsert_line(run_id: str, tutor_id: str, currency: str, amount: int,
                 rate_version_id: str, session_count: int, share: int, breakdown: dict | None = None) -> None:
    payload = {
        "run_id": run_id, "tutor_id": tutor_id, "currency": currency,
        "amount_minor": amount, "rate_version_id": rate_version_id,
        "session_count": session_count, "referral_share_minor": share,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if breakdown is not None:
        payload["breakdown"] = breakdown   # plan 8.4 (§134)
    try:
        db.schema("app").table("payout_line").insert(payload).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            # Recomputing an unapproved run: the numbers may legitimately have
            # changed (a session was marked completed late), so update rather
            # than skip.
            try:
                db.schema("app").table("payout_line").update(payload).eq(
                    "run_id", run_id).eq("tutor_id", tutor_id).execute()
            except Exception as inner:  # noqa: BLE001
                logger.error("line update failed for %s: %s", tutor_id, inner)
        else:
            logger.error("line insert failed for %s: %s", tutor_id, exc)


async def approve_run(run_id: str, admin_id: str) -> dict:
    """A human signs off. The last step Phase 3a takes; 3b starts after it."""
    try:
        run = db.schema("app").table("payout_run").select("*").eq(
            "id", run_id).limit(1).execute()
        row = (run.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not read run: {exc}") from exc

    if not row:
        return {"approved": False, "reason": "no_such_run"}
    if row["status"] != "planned":
        return {"approved": False, "reason": f"run is {row['status']}"}

    now = datetime.now(timezone.utc).isoformat()
    db.schema("app").table("payout_run").update({
        "status": "approved", "approved_by": admin_id,
        "approved_at": now, "updated_at": now,
    }).eq("id", run_id).execute()
    db.schema("app").table("payout_line").update({
        "status": "approved", "updated_at": now,
    }).eq("run_id", run_id).eq("status", "pending").execute()

    return {"approved": True, "run_id": run_id,
            "total_minor": row["total_minor"], "currency": row["currency"]}


async def record_disbursement(line_id: str, external_ref: str,
                              settled_amount_minor: int | None = None,
                              settled_currency: str | None = None,
                              fx_rate: float | None = None,
                              fx_source: str | None = None) -> dict:
    """Record that a human paid a line. **The manual `PayoutDriver` from §11.4.**

    `external_ref` holds an M-Pesa receipt or a bank reference identically, so a
    future automated driver writes the same column and nothing downstream
    changes.

    The FX columns are snapshotted **here, at settlement** — not at computation.
    The rate that matters is the one the money actually moved at (§11.3), and the
    table's CHECK refuses a cross-currency settlement without one.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "status": "settled", "external_ref": external_ref,
        "settled_at": now, "updated_at": now,
    }
    if settled_amount_minor is not None:
        payload["settled_amount_minor"] = settled_amount_minor
    if settled_currency:
        payload["settled_currency"] = settled_currency
    if fx_rate is not None:
        payload["fx_rate"] = fx_rate
        payload["fx_source"] = fx_source or "manual"
        payload["fx_captured_at"] = now

    try:
        result = db.schema("app").table("payout_line").update(payload).eq(
            "id", line_id).execute()
    except Exception as exc:  # noqa: BLE001
        if "payout_line_fx_complete" in str(exc):
            return {"settled": False,
                    "reason": "a cross-currency settlement needs fx_rate and fx_source"}
        raise

    row = (result.data or [None])[0]
    if not row:
        return {"settled": False, "reason": "no_such_line"}

    # Debit the float. The ledger's unique index on
    # (reason, reference_type, reference_id) makes a double-tap a no-op, so this
    # cannot debit twice for one line.
    try:
        db.schema("app").rpc("append_float_ledger", {
            "p_currency": row["currency"],
            "p_direction": "debit",
            "p_amount": int(row["amount_minor"]),
            "p_reason": "payout",
            "p_ref_type": "payout_line",
            "p_ref_id": line_id,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
            # The payment happened; the ledger did not record it. That is a
            # reconciliation problem and must be loud, but it must not look like
            # the payment failed.
            logger.error("float debit failed for settled line %s: %s", line_id, exc)

    return {"settled": True, "line_id": line_id}


# ── Reconciliation and the low-balance alert ──────────────────────────────────

async def reconcile() -> dict:
    """Fold the ledger and check it against the stamped running balance.

    `balance_after_minor` is redundant with the fold **on purpose**: a
    reconciliation that recomputes the fold and disagrees has found a real
    problem, and without the column there is nothing to disagree with.
    """
    try:
        rows = db.schema("app").table("float_ledger").select(
            "currency, direction, amount_minor, balance_after_minor, occurred_at, created_at"
        ).order("occurred_at").order("created_at").execute()
        ledger = rows.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("reconcile could not read the ledger: %s", exc)
        return {"currencies": {}, "error": str(exc)}

    folds: dict[str, int] = {}
    stamped: dict[str, int] = {}
    for row in ledger:
        cur = row["currency"]
        amount = int(row["amount_minor"])
        folds[cur] = folds.get(cur, 0) + (amount if row["direction"] == "credit" else -amount)
        stamped[cur] = int(row["balance_after_minor"])

    out = {}
    for cur, fold in folds.items():
        out[cur] = {
            "fold_minor": fold,
            "stamped_minor": stamped.get(cur),
            "agrees": fold == stamped.get(cur),
        }
        if not out[cur]["agrees"]:
            logger.error("FLOAT RECONCILIATION MISMATCH %s: fold=%s stamped=%s",
                         cur, fold, stamped.get(cur))
    return {"currencies": out}


async def check_float_balance() -> dict:
    """Low-balance alert. §4.3: a threshold in `app.config`, mailed to ADMIN_EMAIL.

    Thresholds are **per currency** — a single number cannot mean anything across
    KES and EUR at once — and a currency with no threshold set is reported but not
    alerted on, rather than compared against a default that would be wrong by
    three orders of magnitude.
    """
    from app.config import settings

    state = await reconcile()
    thresholds = _as_json(
        _config(["payout.float_low_balance_minor"])["payout.float_low_balance_minor"], {})

    low = []
    for cur, info in state.get("currencies", {}).items():
        limit = thresholds.get(cur)
        if limit is None:
            continue
        if info["fold_minor"] < int(limit):
            low.append({"currency": cur, "balance_minor": info["fold_minor"],
                        "threshold_minor": int(limit)})

    mismatches = [c for c, i in state.get("currencies", {}).items() if not i["agrees"]]

    if (low or mismatches) and settings.admin_email:
        from app.services.email_service import send_template_email
        lines = [f"{x['currency']}: {x['balance_minor']} below {x['threshold_minor']}"
                 for x in low]
        if mismatches:
            lines.append("RECONCILIATION MISMATCH in: " + ", ".join(mismatches))
        try:
            await send_template_email(
                to=settings.admin_email,
                template_name="float_alert",
                template_data={
                    "subject": "Tauka float alert",
                    "lines": lines,
                    "low": low,
                    "mismatches": mismatches,
                },
                tags=["float_alert"],
            )
        except Exception as exc:  # noqa: BLE001
            # An alert that cannot be mailed must still be in the log. This is
            # the one place where losing the message means nobody finds out the
            # reserve is short.
            logger.error("FLOAT ALERT could not be mailed (%s): %s", exc, "; ".join(lines))

    return {"low": low, "mismatches": mismatches, "currencies": state.get("currencies", {})}


# ── Tutor-facing read ─────────────────────────────────────────────────────────

async def tutor_payout_history(tutor_id: str, limit: int = 24) -> dict:
    """A tutor's own lines, and their current rate with the inputs behind it.

    **The run is never returned** — `payout_run.total_minor` is every tutor's pay
    added together. RLS says the same thing; this is the second lock on the same
    door.

    The rate comes back with `inputs` because pay that cannot be explained to the
    person receiving it is precisely what §1.4's loyalty thesis is trying to
    avoid.
    """
    lines: list[dict] = []
    try:
        result = db.schema("app").table("payout_line").select(
            "id, currency, amount_minor, session_count, referral_share_minor, "
            "status, external_ref, settled_amount_minor, settled_currency, "
            "settled_at, created_at"
        ).eq("tutor_id", tutor_id).order("created_at", desc=True).limit(limit).execute()
        lines = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("payout history failed for %s: %s", tutor_id, exc)

    rate = None
    try:
        result = db.schema("app").table("tutor_rate_version").select(
            "currency, base_rate_minor, tenure_multiplier_bp, quality_multiplier_bp, "
            "computed_rate_minor, effective_from, inputs"
        ).eq("tutor_id", tutor_id).is_("effective_to", "null").limit(1).execute()
        rate = (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("current rate lookup failed for %s: %s", tutor_id, exc)

    return {
        "current_rate": rate,
        # True when nothing has computed a rate for this tutor — almost always a
        # missing `tutor.country_code` or no rate card for their country. The
        # client says "your rate is being set up", not "0".
        "rate_pending": rate is None,
        "lines": lines,
    }
