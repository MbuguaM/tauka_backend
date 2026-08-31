"""Tutor standing — plan Phase 6, spec §1.6.

A versioned verdict about a tutor, recomputed monthly, that gates real backend
behaviour: first-pick eligibility on new assignments, and mentorship
eligibility. §1.6 is explicit that it must not be cosmetic.

THE FOUR INPUTS, AND THE ONE THAT DOES NOT EXIST.

§3.5 permits exactly four: *"rating history (`video_session_rating`), retention
duration (`student_tutor_link` lifetimes), referral counts (`referral_signup`),
sessions delivered (`tutor_sessions`)"*.

**`app.tutor_sessions` does not exist.** It was never applied to production — it
lives only in `missing_definitions.sql` — and `payout_service` already refuses
to read it for the same reason (CONFLICT-040 was the same dead object graph).
Sessions delivered is `app.cohort_session.lead_tutor_id` +
`app.one_to_one_session.tutor_id`, which is what "delivering" means since
Phase 2 and what payout pays against.

That matters beyond bookkeeping: **standing and pay now count the same
deliveries**, because this module calls `payout_service.delivered_sessions` and
`payout_service.quality_inputs` rather than re-deriving either. Two modules with
their own idea of what a delivered session is would eventually promote a tutor
the payout run did not pay.

FORBIDDEN INPUTS, RESTATED BECAUSE THIS IS WHERE IT WOULD BE TEMPTING.

Nothing from `app.activity_summary` or `app.ai_usage`. `CLAUDE.md`: *"NEVER gate
a feature on an activity or telemetry number, in any project — the measurements
stop measuring the moment they buy something."* Standing gates assignment, which
is upstream of pay, so this is the strongest form of that gate. The `inputs`
jsonb column is snapshotted on every computation precisely so a later reader can
check the rule was kept.

THE PLATFORM HAS ONE TUTOR AND NONE OF THE INPUTS HAS A ROW.

Measured 2026-08-28: `video_session_rating` 0, `student_tutor_link` 0,
`referral_signup` 0, `one_to_one_session` 0, `cohort_session_attendance` 0. Every
tutor computes `provisional`, and will for months. Two consequences, both
deliberate:

- **That is the honest answer, not a placeholder.** A rubric that awarded
  `established` to a tutor with no delivered sessions would be a fabricated
  credential, which is the FC-030 failure mode wearing different clothes.
- **The first-pick gate is supply-guarded.** Below `match.min_tutor_supply`
  active tutors it is advisory and reports why, exactly as the concentration cap
  and guest minimum are in `matching_service`. A hard gate on a one-tutor
  platform blocks every assignment that will ever be attempted.
"""

import calendar
import json
import logging
from datetime import date, datetime, timedelta, timezone

from app.services import matching_service, payout_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# The ladder, low to high. Order is load-bearing: `_verdict` walks it downward
# and takes the first rung whose every criterion is met.
STANDINGS = ("provisional", "established", "senior", "lead")

_DEFAULTS = {
    "standing.cadence_day": "1",
    "standing.first_pick_min": "senior",
    "standing.rubric": json.dumps({
        "established": {"min_sessions": 20, "min_months": 3},
        "senior": {"min_sessions": 100, "min_months": 12,
                   "min_avg_rating": 4.3, "min_ratings": 5,
                   "min_retention_months": 4},
        "lead": {"min_sessions": 250, "min_months": 24,
                 "min_avg_rating": 4.5, "min_ratings": 15,
                 "min_retention_months": 6, "min_retained_referrals": 5},
    }),
}

# Nothing was delivered before the platform existed. Used as the lower bound of
# the "lifetime" window rather than `date.min`, which PostgREST will not accept
# in an ISO timestamp filter.
_EPOCH = date(2024, 1, 1)


# ── Config ────────────────────────────────────────────────────────────────────

def _config(keys: list[str]) -> dict[str, str]:
    values = dict(_DEFAULTS)
    try:
        result = db.schema("app").table("config").select("key, value").in_(
            "key", keys).execute()
        for row in result.data or []:
            if row.get("value") not in (None, ""):
                values[row["key"]] = row["value"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("standing config read failed, using defaults: %s", exc)
    return values


def _as_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def rubric() -> dict:
    """The ladder from `app.config`, falling back to the built-in defaults.

    A malformed rubric must not stop the monthly run — it degrades to the
    defaults and says so, because the alternative is a month with no standings
    computed and nothing in the log naming the reason.
    """
    raw = _config(["standing.rubric"])["standing.rubric"]
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("rubric is not an object")
    except (ValueError, TypeError) as exc:
        logger.error("standing.rubric is unparseable (%s) — using defaults", exc)
        parsed = json.loads(_DEFAULTS["standing.rubric"])
    return parsed


def is_cadence_day(today: date | None = None) -> bool:
    """The monthly guard, mirroring `payout_service.is_cadence_day`.

    Its own key rather than `payout.cadence_day`: standing is not payroll, and
    coupling them would mean moving the payout date silently moved the
    promotion date. Clamped into the month so 31 does not skip February.
    """
    today = today or datetime.now(timezone.utc).date()
    wanted = _as_int(_config(["standing.cadence_day"])["standing.cadence_day"], 1)
    last = calendar.monthrange(today.year, today.month)[1]
    return today.day == max(1, min(wanted, last))


# ── Inputs ────────────────────────────────────────────────────────────────────

def _months_between(earlier: datetime, later: datetime) -> int:
    """Whole months, counted on the calendar rather than by dividing days.

    `days / 30` drifts: twelve of those is 360 days, so a tutor would reach
    "12 months" five days early, every year, and the error compounds at the top
    of the ladder where the thresholds are largest.
    """
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(0, months)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def retention_inputs(tutor_id: str, now: datetime | None = None) -> dict:
    """§3.5's "retention duration (`student_tutor_link` lifetimes)".

    Two different numbers come out of the same rows and they are not
    interchangeable:

    - **`tenure_months`** — how long this tutor has been teaching anyone. The
      ladder's `min_months` rung.
    - **`mean_retention_months`** — how long their students *stay*, which is
      what §1.6 actually names. An open link counts up to now; a closed one
      counts to `ended_at`.

    A tutor with one student of three months has a tenure of 3 and a retention
    of 3. A tutor with forty students who each leave after a fortnight has a
    long tenure and a retention near zero, and only the second number notices.

    **Every state counts toward retention, including `reassigned`.** A link that
    ended in a handoff is a relationship the student kept until the platform
    moved them, and scoring it as a loss would penalise a tutor for going on
    leave.
    """
    now = now or datetime.now(timezone.utc)
    try:
        result = db.schema("app").table("student_tutor_link").select(
            "state, started_at, ended_at"
        ).eq("tutor_id", tutor_id).execute()
        rows = result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("retention read failed for %s: %s", tutor_id, exc)
        # Zero, not a guess. A failed read must not promote anybody.
        return {"tenure_months": 0, "mean_retention_months": 0.0,
                "link_count": 0, "active_links": 0}

    lifetimes: list[int] = []
    earliest: datetime | None = None
    active = 0
    for row in rows:
        started = _parse_ts(row.get("started_at"))
        if not started:
            continue
        if earliest is None or started < earliest:
            earliest = started
        ended = _parse_ts(row.get("ended_at")) or now
        lifetimes.append(_months_between(started, ended))
        if row.get("state") in ("active", "handoff_pending"):
            active += 1

    return {
        "tenure_months": _months_between(earliest, now) if earliest else 0,
        "mean_retention_months": round(sum(lifetimes) / len(lifetimes), 2)
        if lifetimes else 0.0,
        "link_count": len(lifetimes),
        "active_links": active,
    }


def referral_inputs(tutor_id: str) -> dict:
    """Signups attributed to this tutor, and how many are still on the platform.

    **Retained, not signed up, is the number the ladder uses.** A referral count
    alone rewards recruitment; §8's referral-fraud risk is precisely a tutor
    referring students they control. A student who signed up and left counts
    once in `signups` and never in `retained`.

    "Retained" is an active `student_tutor_link` — the same signal
    `payout_service` vests against, so a referral cannot be retained for
    standing and unretained for pay.
    """
    try:
        result = db.schema("app").table("referral_signup").select(
            "student_id").eq("tutor_id", tutor_id).execute()
        student_ids = [r["student_id"] for r in (result.data or []) if r.get("student_id")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral read failed for %s: %s", tutor_id, exc)
        return {"signups": 0, "retained": 0}

    if not student_ids:
        return {"signups": 0, "retained": 0}

    try:
        linked = db.schema("app").table("student_tutor_link").select(
            "student_id").in_("student_id", student_ids).eq("state", "active").execute()
        retained = len({r["student_id"] for r in (linked.data or [])})
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral retention read failed for %s: %s", tutor_id, exc)
        retained = 0

    return {"signups": len(student_ids), "retained": retained}


def gather_inputs(tutor_id: str, delivered: dict | None = None,
                  now: datetime | None = None) -> dict:
    """Every permitted input for one tutor, as one snapshot.

    `delivered` is passed in by the monthly run, which computes the whole
    platform's deliveries once rather than once per tutor. Passing None makes
    this usable on its own for a single tutor — the `/tutor/standing` read path.
    """
    now = now or datetime.now(timezone.utc)
    if delivered is None:
        delivered = lifetime_deliveries().get(tutor_id, {})

    quality = payout_service.quality_inputs(tutor_id)
    retention = retention_inputs(tutor_id, now=now)
    referral = referral_inputs(tutor_id)

    return {
        "sessions": int(delivered.get("cohort", 0)) + int(delivered.get("one_to_one", 0)),
        "cohort_sessions": int(delivered.get("cohort", 0)),
        "one_to_one_sessions": int(delivered.get("one_to_one", 0)),
        "minutes": int(delivered.get("minutes", 0)),
        "avg_rating": quality.get("mean_stars"),
        "rating_count": int(quality.get("rating_count", 0)),
        "tenure_months": retention["tenure_months"],
        "mean_retention_months": retention["mean_retention_months"],
        "link_count": retention["link_count"],
        "active_links": retention["active_links"],
        "referral_signups": referral["signups"],
        "retained_referrals": referral["retained"],
        # Recorded so a later reader can see which rubric produced the verdict.
        # A promotion is not explicable without the ladder it was measured on.
        "computed_at": now.isoformat(),
    }


def lifetime_deliveries() -> dict[str, dict]:
    """Every delivered session, ever, per tutor.

    Deliberately `payout_service.delivered_sessions` over a wide window rather
    than a second query: the definition of "delivered" — `status = 'completed'`,
    `lead_tutor_id` for the cohort lane, `tutor_id` for the 1-on-1 lane — must
    have exactly one home, and that is the module that pays against it.
    """
    today = datetime.now(timezone.utc).date()
    return payout_service.delivered_sessions(_EPOCH, today + timedelta(days=1))


# ── The verdict ───────────────────────────────────────────────────────────────

_CRITERIA = {
    "min_sessions": ("sessions", False),
    "min_months": ("tenure_months", False),
    "min_avg_rating": ("avg_rating", True),
    "min_ratings": ("rating_count", False),
    "min_retention_months": ("mean_retention_months", False),
    "min_retained_referrals": ("retained_referrals", False),
}


def _meets(rung: dict, inputs: dict) -> tuple[bool, list[str]]:
    """Does this input snapshot clear every criterion on this rung?

    Returns the misses as well as the verdict, so a tutor can be told *"two more
    months and eleven more sessions"* rather than *"no"*. §1.6 makes standing
    gate real behaviour; a gate whose distance you cannot see is
    indistinguishable from the platform disliking you.
    """
    misses: list[str] = []
    for key, threshold in rung.items():
        mapped = _CRITERIA.get(key)
        if not mapped:
            # An unknown criterion is a rubric someone edited by hand into a
            # shape this code does not implement. Refuse the rung rather than
            # silently ignoring the constraint — ignoring it promotes people.
            misses.append(f"unknown criterion {key}")
            continue
        field, nullable = mapped
        value = inputs.get(field)
        if value is None:
            # `avg_rating` is None when nobody has rated this tutor. That is not
            # a zero — a tutor with no ratings has not been rated badly — but it
            # cannot clear a rating threshold either.
            misses.append(f"{field}: none yet, needs {threshold}")
            continue
        try:
            if float(value) < float(threshold):
                misses.append(f"{field}: {value} < {threshold}")
        except (TypeError, ValueError):
            misses.append(f"{field}: {value!r} is not comparable to {threshold!r}")
    return (not misses), misses


def verdict(inputs: dict, ladder: dict | None = None) -> dict:
    """The highest rung every criterion of which is met.

    Walks downward from `lead`, so a tutor who clears `lead` is not stopped at
    `senior` by the loop order. `provisional` is the floor and has no criteria —
    it is what every tutor is until the platform has anything to measure.
    """
    ladder = ladder if ladder is not None else rubric()
    for name in reversed(STANDINGS[1:]):
        rung = ladder.get(name)
        if not isinstance(rung, dict):
            continue
        ok, misses = _meets(rung, inputs)
        if ok:
            return {"standing": name, "next": None, "misses": []}

    # Not promoted. Report the distance to the *next* rung up only — the whole
    # ladder as a wall of misses tells a new tutor nothing actionable.
    next_rung = ladder.get("established")
    misses = _meets(next_rung, inputs)[1] if isinstance(next_rung, dict) else []
    return {"standing": "provisional", "next": "established", "misses": misses}


# ── Persistence ───────────────────────────────────────────────────────────────

def current_standing(tutor_id: str) -> dict | None:
    """The open row, or None if this tutor has never been computed."""
    try:
        result = db.schema("app").table("tutor_standing").select("*").eq(
            "tutor_id", tutor_id).is_("effective_to", "null").limit(1).execute()
        return (result.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("current standing read failed for %s: %s", tutor_id, exc)
        return None


def _record(tutor_id: str, standing: str, inputs: dict, now: datetime) -> dict:
    """Close the open row and open a new one — but only if the verdict changed.

    **An unchanged standing writes nothing.** The table is a history of
    *transitions*; a row per tutor per month would bury the two rows that matter
    under a hundred that say the same thing, and `effective_from` would stop
    meaning "since when".

    The close-then-open order is not interchangeable. The partial unique index
    `idx_tutor_standing_current` allows exactly one row with a NULL
    `effective_to` per tutor, so opening first would be refused — which is the
    index doing its job, and is why it exists.
    """
    open_row = current_standing(tutor_id)
    if open_row and open_row.get("standing") == standing:
        try:
            db.schema("app").table("tutor_standing").update({
                "computed_at": now.isoformat(), "inputs": inputs,
            }).eq("id", open_row["id"]).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("standing refresh failed for %s: %s", tutor_id, exc)
        return {"tutor_id": tutor_id, "standing": standing, "changed": False}

    if open_row:
        db.schema("app").table("tutor_standing").update({
            "effective_to": now.isoformat(),
        }).eq("id", open_row["id"]).execute()

    db.schema("app").table("tutor_standing").insert({
        "tutor_id": tutor_id,
        "standing": standing,
        "computed_at": now.isoformat(),
        "effective_from": now.isoformat(),
        "inputs": inputs,
    }).execute()

    return {"tutor_id": tutor_id, "standing": standing, "changed": True,
            "from": (open_row or {}).get("standing")}


async def recompute_standings() -> dict:
    """The monthly job. Every active tutor, one input gather, one verdict each.

    Isolated per tutor: one tutor whose links fail to read must not stop the
    rest, the same way `payout_cycle` isolates its three steps.
    """
    now = datetime.now(timezone.utc)
    ladder = rubric()
    delivered = lifetime_deliveries()

    try:
        result = db.schema("app").table("tutor").select("id").eq(
            "status", "active").is_("deleted_at", "null").execute()
        tutor_ids = [r["id"] for r in (result.data or [])]
    except Exception as exc:  # noqa: BLE001
        logger.error("standing run could not list tutors: %s", exc)
        return {"computed": 0, "changed": 0, "failed": 0, "error": str(exc)}

    computed = changed = failed = 0
    for tutor_id in tutor_ids:
        try:
            inputs = gather_inputs(tutor_id, delivered.get(tutor_id, {}), now=now)
            outcome = _record(tutor_id, verdict(inputs, ladder)["standing"], inputs, now)
            computed += 1
            changed += 1 if outcome.get("changed") else 0
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.error("standing computation failed for %s: %s", tutor_id, exc)

    logger.info("Standing run: %d computed, %d changed, %d failed",
                computed, changed, failed)
    return {"computed": computed, "changed": changed, "failed": failed}


# ── The gate (§1.6: "must gate real backend behavior") ────────────────────────

def first_pick_eligible(tutor_id: str) -> dict:
    """May this tutor take first pick on a new assignment?

    **Advisory below `match.min_tutor_supply` active tutors, binding above it.**
    That is the same threshold `matching_service` applies to the concentration
    cap and the guest minimum, read from the same key on purpose: a platform
    where first-pick binds but the concentration cap does not is a platform that
    hands every student to the one senior tutor and then refuses to notice.

    `eligible` is the answer to act on; `binding` says whether the answer was
    enforced or reported. A caller that reads only `eligible` gets the
    permissive behaviour, which is the safe direction on a platform of one.
    """
    cfg = _config(["standing.first_pick_min", "match.min_tutor_supply"])
    minimum = cfg["standing.first_pick_min"]
    if minimum not in STANDINGS:
        logger.warning("standing.first_pick_min=%r is not a standing — using 'senior'",
                       minimum)
        minimum = "senior"

    supply = matching_service.active_tutor_count()
    binding = supply >= _as_int(cfg["match.min_tutor_supply"], 4)

    row = current_standing(tutor_id)
    standing = (row or {}).get("standing", "provisional")
    meets = STANDINGS.index(standing) >= STANDINGS.index(minimum)

    return {
        "eligible": meets or not binding,
        "meets_threshold": meets,
        "standing": standing,
        "required": minimum,
        "binding": binding,
        "supply": supply,
        "reason": None if meets else (
            f"standing is {standing}, first pick needs {minimum}"
            + ("" if binding else " — advisory only, tutor supply is below "
                                 f"match.min_tutor_supply ({supply})")
        ),
    }


def mentorship_eligible(tutor_id: str) -> dict:
    """§1.6's second named gate. `senior` or above, and never supply-waived.

    Unlike first pick, this one has no advisory mode: mentoring is a thing a
    person does for another person, and waiving the bar because the platform is
    small would put a tutor with no delivered sessions in charge of one who has
    them. There is nothing to unblock — nobody is waiting to be mentored on a
    one-tutor platform.
    """
    row = current_standing(tutor_id)
    standing = (row or {}).get("standing", "provisional")
    meets = STANDINGS.index(standing) >= STANDINGS.index("senior")
    return {"eligible": meets, "standing": standing, "required": "senior"}


def standing_report(tutor_id: str) -> dict:
    """What the tutor's own standing panel renders.

    Recomputes the verdict live rather than trusting the stored row, and returns
    both. They should agree; when they do not, the tutor has crossed a threshold
    since the last run and `pending` says so — which is a far better answer than
    showing a stale standing for up to a month with no explanation.
    """
    row = current_standing(tutor_id)
    inputs = gather_inputs(tutor_id)
    live = verdict(inputs)
    stored = (row or {}).get("standing", "provisional")

    return {
        "standing": stored,
        "effective_from": (row or {}).get("effective_from"),
        "pending": live["standing"] if live["standing"] != stored else None,
        "next": live["next"],
        "misses": live["misses"],
        "inputs": inputs,
        "first_pick": first_pick_eligible(tutor_id),
        "mentorship": mentorship_eligible(tutor_id),
    }


def history(tutor_id: str, limit: int = 24) -> list[dict]:
    """Standing over time, newest first. The audit trail §4.6 exists for."""
    try:
        result = db.schema("app").table("tutor_standing").select(
            "standing, computed_at, effective_from, effective_to, inputs"
        ).eq("tutor_id", tutor_id).order(
            "effective_from", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("standing history read failed for %s: %s", tutor_id, exc)
        return []
