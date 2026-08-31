"""The 1st-and-15th intake run — plan §3 Stage 7, Phase 4.

`/tasks/daily` fires every day; intake runs twice a month. The guard reads
`match.intake_days` from `app.config` rather than hardcoding the dates — the
same shape as `payout.cadence_day` and `standing.cadence_day`, with its own key
so moving payday does not silently move matching day.

THREE JOBS, ONE ENTRY POINT, AND THE ORDER MATTERS.

  1. **Expire** holds past their 48h deadline, and promote each affected
     student's next offer. Runs daily and first, because an expired hold is a
     seat somebody else could be sitting in.
  2. **Close** any live cycle whose holds have all resolved: dissolve cohorts
     that never reached three, defer whoever is left with a reason. Runs before
     the new cycle so the closing cycle's members are back in the pool for it.
  3. **Run** a new cycle, on an intake day only.

Steps 1 and 2 run every day. Only step 3 is behind the cadence guard, because a
48-hour deadline that is only swept on the 1st and the 15th is not a deadline.
"""

import logging
from datetime import datetime, timezone

from app.services import intake_service, offer_service

logger = logging.getLogger(__name__)


async def run_intake_cycle() -> None:
    """Sweep, close, and — on an intake day — match."""
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

    if not intake_service.is_intake_day():
        logger.info("Intake: not an intake day (expired=%s, closed=%s)",
                    expired.get("expired"), closed.get("closed"))
        return

    today = datetime.now(timezone.utc).date()
    try:
        result = await intake_service.run_cycle(mode="live", cycle_date=today)
    except Exception as exc:  # noqa: BLE001
        logger.error("intake cycle failed: %s", exc)
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
        logger.warning("Intake cycle had an empty eligible pool — is anyone "
                       "on a paid tier with a timezone and three preferences?")
    elif stats.get("tutor_slots") == 0:
        logger.warning("Intake cycle found no tutor slots — no tutor has a "
                       "teaching profile, availability and free capacity.")
