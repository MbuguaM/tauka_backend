"""The intake run — plan §3 Stage 7, Phase 4; driven by the calendar since §102.

`/tasks/daily` fires every day. Matching runs when an `app.intake` is due:
`cutoff_closes + match.run_days_after_cutoff` has come and formation hasn't
(plan 3.4, E-02). `match.intake_days` ("1st and 15th") was retired in §102. A
**rehearsal** intake (Hidar 2019, a simulated pod) is run DRY: it writes one
`intake_cycle` row of proposals and places nobody.

THREE JOBS, ONE ENTRY POINT, AND THE ORDER MATTERS.

  1. **Expire** holds past their 24 h deadline (E-13), and promote each affected
     student's next offer. Runs daily and first, because an expired hold is a
     seat somebody else could be sitting in.
  2. **Close** any live cycle whose holds have all resolved: dissolve cohorts
     that never reached three, defer whoever is left with a reason. Runs before
     the new cycle so the closing cycle's members are back in the pool for it.
  3. **Run** a new cycle for each due intake.

Steps 1 and 2 run every day. Only step 3 waits for the calendar, because a
24-hour deadline that is only swept on matching day is not a deadline.
"""

import logging
from datetime import datetime, timezone

from app.services import intake_service, offer_service

logger = logging.getLogger(__name__)


async def run_intake_cycle() -> None:
    """Sweep, close, and match each intake that is due."""
    try:
        expired = await offer_service.expire_due()
    except Exception as exc:  # noqa: BLE001
        logger.error("offer expiry sweep failed: %s", exc)
        expired = {"expired": 0}

    try:
        closed = await offer_service.close_cycle()
    except Exception as exc:  # noqa: BLE001
        logger.error("cycle close failed: %s", exc)
        closed = {"closed": 0}

    today = datetime.now(timezone.utc).date()
    due = intake_service.due_intakes(today)
    if not due:
        logger.info("Intake: nothing due (expired=%s, closed=%s)",
                    expired.get("expired"), closed.get("closed"))
        return
    for intake in due:
        await _run_one(intake, today)


async def _run_one(intake: dict, today) -> None:
    name = f"{intake['ethiopic_month'].title()} {intake['ec_year']}"
    rehearsal = intake.get("mode") == "rehearsal"
    try:
        result = await intake_service.run_cycle(
            mode="dry_run" if rehearsal else "live", cycle_date=today,
            notes=f"{name} ({'rehearsal' if rehearsal else 'formation'})", intake=intake)
    except Exception as exc:  # noqa: BLE001
        logger.error("intake cycle failed for %s: %s", name, exc)
        return
    if rehearsal:
        logger.info("Rehearsal %s: dry run %s, pool=%s", name, result.get("cycle_id"),
                    (result.get("stats") or {}).get("pool_size"))
        return

    if not result.get("ok"):
        # The commonest cause is the partial unique index refusing a second live
        # cycle for today — which is the freeze working, not a fault. Logged at
        # warning so a genuinely failed run is still visible.
        logger.warning("Intake cycle did not run: %s", result.get("error"))
        return

    stats = result.get("stats", {})
    logger.info(
        "Intake cycle %s: pool=%s eligible=%s slots=%s cohorts=%s placed=%s deferred=%s",
        result.get("cycle_id"), stats.get("pool_size"), stats.get("eligible"),
        stats.get("tutor_slots"), result.get("cohorts_formed"),
        stats.get("students_placed"), result.get("deferred"),
    )

    # Worth a line of its own. On a platform with one tutor and two students a
    # cycle that places nobody is the EXPECTED outcome (plan §4: "with today's
    # numbers it is zero"), and a cycle that places nobody because it read an
    # empty pool is a different thing entirely. Without this they look the same
    # in the log.
    if stats.get("eligible") == 0:
        logger.warning("Intake cycle had an empty eligible pool — has anyone "
                       "finished Phase 0, paid, and set a timezone and three preferences?")
    elif stats.get("tutor_slots") == 0:
        logger.warning("Intake cycle found no tutor slots — no tutor has a "
                       "teaching profile, availability and free capacity.")
