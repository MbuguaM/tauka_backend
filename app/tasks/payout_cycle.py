import logging

from app.services import payout_service

logger = logging.getLogger(__name__)


async def run_payout_cycle() -> None:
    """Daily cron: the monthly payout work, behind a cadence guard.

    `/tasks/daily` fires every day and these jobs must fire once a month, so the
    guard is the first thing that runs. It reads `payout.cadence_day` from
    `app.config` rather than hardcoding the 1st, and clamps into the month so a
    value of 31 does not skip February entirely.

    Order matters and is not arbitrary:

    1. **Rates** — the run multiplies sessions by a rate version, so the version
       for the new month has to exist before anything is priced.
    2. **Vesting** — the referral share on a line is a percentage, and the
       percentage has to be current before it is applied.
    3. **The run** — computes what each tutor is owed and stops. It is left
       unapproved: approval names a person, and a machine writing `approved_by`
       on a payment would be fabricating an approver.

    **The float check runs every day, not monthly.** A reserve that has gone
    short is not something to find out about on the 1st of next month, and a
    reconciliation mismatch is worse the longer it sits.
    """
    # Always, regardless of cadence. This is the alarm, not the payroll.
    try:
        state = await payout_service.check_float_balance()
        if state.get("low") or state.get("mismatches"):
            logger.warning("Float alert: low=%s mismatches=%s",
                           state.get("low"), state.get("mismatches"))
    except Exception as exc:  # noqa: BLE001
        logger.error("float check failed: %s", exc)

    if not payout_service.is_cadence_day():
        return

    try:
        rates = await payout_service.recompute_rates()
        logger.info("Payout cycle — rates: %s", rates)
    except Exception as exc:  # noqa: BLE001
        # Each step is isolated. A rate recompute that fails must not stop
        # vesting, and neither must stop the float alarm above — which is why
        # that one already ran.
        logger.error("recompute_rates failed: %s", exc)

    try:
        vesting = await payout_service.recompute_vesting()
        logger.info("Payout cycle — vesting: %s", vesting)
    except Exception as exc:  # noqa: BLE001
        logger.error("recompute_vesting failed: %s", exc)

    try:
        run = await payout_service.compute_payout_run()
        logger.info("Payout cycle — run: %s", run)
    except Exception as exc:  # noqa: BLE001
        logger.error("compute_payout_run failed: %s", exc)
