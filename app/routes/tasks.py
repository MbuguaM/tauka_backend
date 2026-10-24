"""
Scheduled jobs, triggered externally instead of by an in-process scheduler.

WHY THERE IS NO SCHEDULER ANY MORE
----------------------------------
This service used APScheduler started from the FastAPI lifespan. That cannot
work on a serverless host: the function is frozen the moment a response is
returned, so the event loop APScheduler needs never ticks and **not one job ever
fired**. Silently — the app boots, logs "Scheduler started with 3 jobs", and
then does nothing at 06:00. Gift expiry warnings would simply never send.

It was also wrong on a persistent host the moment there were two instances:
in-process APScheduler has no jobstore and no lock, so every instance runs every
job. Two instances means every supporter gets two milestone emails, and a
rolling deploy briefly gives you both.

An external trigger fixes both. It is externally observable (you can see the
invocation and its status), it cannot double-fire from scaling, and it is
testable with curl.

VERCEL CRON NOTES
-----------------
  * Vercel Cron issues **GET** only — hence GET, not POST, for what is really
    a mutating action.
  * It authenticates by sending `Authorization: Bearer $CRON_SECRET` when a
    `CRON_SECRET` env var exists on the project. Set it, or this refuses
    everything.
  * The Hobby plan has historically capped cron jobs per project and their
    frequency at roughly once daily. That is why all three jobs live behind ONE
    endpoint rather than three — it fits the tightest plan, and the jobs are
    daily anyway.
"""
import logging
import secrets

from fastapi import APIRouter, Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _authorise(authorization: str | None) -> None:
    """
    Fail CLOSED. An unset secret refuses every request rather than allowing
    them: this endpoint can mail every supporter in the database, so the
    consequence of a missing env var must be "nothing runs", never "anyone
    can run it".
    """
    if not settings.cron_secret:
        logger.error(
            "CRON_SECRET is not set — refusing to run scheduled jobs. Set it on "
            "the host; Vercel Cron sends it automatically once it exists."
        )
        raise HTTPException(status_code=503, detail="Scheduled jobs are not configured")

    expected = f"Bearer {settings.cron_secret}"
    # Constant-time: a plain == leaks the secret one byte at a time to anyone
    # who can measure the response, and this endpoint is public by necessity.
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/hourly")
async def run_hourly_tasks(authorization: str | None = Header(default=None)):
    """Hourly jobs: pod-session rooms (features-addendum plan §3 item 3),
    exchange requests and rooms (addendum A), and session reminders 24 h and
    1 h before (plan 6.1/6.2).
    Needs an external hourly cron with the same CRON_SECRET (Vercel's own cron
    here runs daily). Safe to schedule now: nothing is sent until push or
    WhatsApp is switched on."""
    _authorise(authorization)
    from app.tasks.communication_cycles import run_cohort_room_cycle, run_exchange_hourly, run_reminder_cycle
    # Rooms first, so a reminder an hour before a pod session can't beat its room.
    results: dict[str, str] = {}
    for name, job in (("cohort_room_cycle", run_cohort_room_cycle), ("exchange_hourly", run_exchange_hourly),
                      ("reminder_cycle", run_reminder_cycle)):
        try:
            await job()
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            logger.exception("hourly job %s failed", name)
            results[name] = f"failed: {exc}"
    return {"ran": results}


@router.get("/daily")
async def run_daily_tasks(authorization: str | None = Header(default=None)):
    """
    Run every daily job, in order. Triggered by Vercel Cron.

    Each job is isolated: one raising must not stop the rest, because a failure
    in testimonial outreach is no reason for gift expiries to go unprocessed.
    Returns per-job status so a failure is visible in the cron log rather than
    buried, and reports 200 even with failures inside — the trigger succeeded,
    and a non-200 would make Vercel retry the jobs that already worked.
    """
    _authorise(authorization)

    from app.tasks.activity_retention import run_activity_retention_task
    from app.tasks.gift_lifecycle import run_gift_lifecycle
    from app.tasks.intake_cycle import run_intake_cycle
    from app.tasks.makeup_cycle import run_makeup_cycle
    from app.tasks.clinic_cycle import run_clinic_cycle
    from app.tasks.checkpoint_cycle import run_checkpoint_cycle
    from app.tasks.placement_cycle import run_placement_cycle
    from app.tasks.pause_cycle import run_pause_cycle
    from app.tasks.refill_cycle import run_refill_cycle
    from app.tasks.schedule_cycle import run_schedule_cycle
    from app.tasks.catchup_cycle import run_catchup_cycle
    from app.tasks.concern_cycle import run_concern_cycle
    from app.tasks.cover_cycle import run_cover_cycle
    from app.tasks.grace_cycle import run_grace_cycle
    from app.tasks.communication_cycles import (run_benchmark_snapshot, run_dst_cycle, run_gift_card_cycle,
                                                run_exchange_daily, run_mentoring_cycle, run_peer_cycle,
                                                run_qa_sample_cycle, run_season_cycle, run_sponsorship_cycle, run_summary_refresh,
                                                run_unit_assessment_cycle)
    from app.tasks.phase0_reminder_cycle import run_phase0_reminder_cycle
    from app.tasks.milestone_check import run_milestone_check
    from app.tasks.commitment_lifecycle import (
        report_unmarked_sessions,
        run_commitment_lifecycle,
    )
    from app.tasks.payout_cycle import run_payout_cycle
    from app.tasks.standing_cycle import run_standing_cycle
    from app.tasks.testimonial_outreach import run_testimonial_outreach
    from app.tasks.waitlist_retention import run_waitlist_retention
    from app.tasks.tutor_application_retention import run_tutor_application_retention

    jobs = (
        ("gift_lifecycle", run_gift_lifecycle),
        ("milestone_check", run_milestone_check),
        ("testimonial_outreach", run_testimonial_outreach),
        # Runs daily, but only the float alarm fires daily; the rate recompute,
        # the vesting recalculation and the payout run sit behind a cadence
        # guard reading `payout.cadence_day`. Placed before activity_retention
        # for the same reason everything is: it does not delete anything.
        ("payout_cycle", run_payout_cycle),
        # Before the payout cycle would also be fine; after it is chosen so a
        # term expiring on the 1st is not raced by the run that pays for it.
        ("commitment_lifecycle", run_commitment_lifecycle),
        # Reports only. A 1-on-1 becomes payable when its tutor marks it,
        # never when a job notices it is in the past.
        ("unmarked_sessions", report_unmarked_sessions),
        # Monthly behind its own `standing.cadence_day`. AFTER payout_cycle
        # deliberately: both count the same delivered sessions, and running
        # standing second means a month's deliveries are already settled by the
        # time promotion reads them. It writes only on a change of verdict.
        ("standing_cycle", run_standing_cycle),
        # Cohort matching. Runs the offer-expiry sweep and the cycle close
        # DAILY, and the matcher itself only when an `app.intake` is due (§102:
        # the day after its lesson deadline) — a 24-hour hold that is only
        # swept on matching day is not a deadline. Placed after standing_cycle because it can create cohorts,
        # and a cohort formed today has no delivered sessions for either of the
        # jobs above to count.
        # Pauses (plan 4.4, §111): start the ones whose paid month ended — out of
        # the pod, offers declined — BEFORE intake_cycle and makeup_cycle, so a
        # learner paused today is neither matched nor grouped today.
        ("pause_cycle", run_pause_cycle),
        # Missed payments (plan 2.3a, §126): day 3/6 reminders, lapse on day 7,
        # BEFORE makeup_cycle so a learner lapsed today isn't grouped today.
        ("grace_cycle", run_grace_cycle),
        # Phase 0 reminder emails (plan 1.3, §127): weekly, and 14 / 3 days
        # before the target intake's lesson deadline.
        ("phase0_reminder_cycle", run_phase0_reminder_cycle),
        ("intake_cycle", run_intake_cycle),
        # Seat refills (plan 3.6, §112): AFTER intake_cycle, so today's offer
        # expiries have run and a formation's own offers come first; BEFORE
        # makeup_cycle, so a learner offered a seat today isn't also grouped.
        ("refill_cycle", run_refill_cycle),
        # Pod time changes (plan 4.5, §113): apply agreed ones on their start
        # day, before makeup_cycle reads the pods' session times.
        ("schedule_cycle", run_schedule_cycle),
        # Make-ups for learners with no pod (enrolment plan §2e). AFTER
        # intake_cycle: a pod formed today takes its members out of the
        # waiting pool before anyone is grouped for a make-up.
        # Cover requests (plan §2h, §123): BEFORE makeup_cycle, so a pod session
        # cancelled for no cover has granted its credits by the time make-ups
        # are grouped.
        ("cover_cycle", run_cover_cycle),
        ("makeup_cycle", run_makeup_cycle),
        # Phase 0 clinics (plan 1.5, §105): missing rooms and day-before reminders.
        ("clinic_cycle", run_clinic_cycle),
        # Catch-up for absent learners (plan 4.8, §114): notes + the lesson's practice.
        ("catchup_cycle", run_catchup_cycle),
        # Concern reports (plan 4.6, §117): failed alerts resent; 24 h overdue alert.
        ("concern_cycle", run_concern_cycle),
        # Monthly pod checkpoints (plan 4.2, §109): opens this Ethiopic month's
        # review for each live pod. AFTER intake_cycle, so a pod formed today
        # (start_date = day 1) is not reviewed before its first session.
        ("checkpoint_cycle", run_checkpoint_cycle),
        # Placement emails (plan 3.5, §110) the accept request missed or abandoned.
        ("placement_cycle", run_placement_cycle),
        # Pod welcomes and season posts (plan 6.3, §133): after placement.
        ("season_cycle", run_season_cycle),
        # Clock-change warnings a week ahead (plan 6.5).
        ("dst_cycle", run_dst_cycle),
        # QA observations, one per tutor per month (plan 8.7).
        ("qa_sample_cycle", run_qa_sample_cycle),
        # Peer pairing on Mondays (Track 5), after placement so new pods count.
        ("peer_cycle", run_peer_cycle),
        ("mentoring_cycle", run_mentoring_cycle),
        # Exchange partners (features addendum A): inactive partners to the
        # waitlist, the waitlist into free places, held rewards released.
        ("exchange_daily", run_exchange_daily),
        # Lesson summaries (features addendum B4): flagged or out-of-date ones.
        ("summary_refresh", run_summary_refresh),
        # Unit assessments (§141): blocks reached, so tutors see the speaking check.
        ("unit_assessment_cycle", run_unit_assessment_cycle),
        # Benchmarks after each formation (Track 9), for the two-consecutive rule.
        ("benchmark_snapshot", run_benchmark_snapshot),
        # Gift cards (Track 10, §139): end access after the months; lapse unredeemed after a year.
        ("gift_card_cycle", run_gift_card_cycle),
        # Sponsorship (§144): holds released, seats ended after their months, unclaimed lapsed.
        ("sponsorship_cycle", run_sponsorship_cycle),
        # Last on purpose: it is the only job here that deletes anything, and
        # nothing else depends on it having run. A day late costs storage; a
        # mail job skipped because deletion threw costs a person their email.
        ("activity_retention", run_activity_retention_task),
        # Also a deletion, so also at the end: unconverted waitlist entries
        # past their 12 months (enrolment flow, Retention).
        ("waitlist_retention", run_waitlist_retention),
        # Also a deletion: rejected / withdrawn tutor applications after 12 months (§120, L-017).
        ("tutor_application_retention", run_tutor_application_retention),
    )

    results: dict[str, str] = {}
    for name, job in jobs:
        try:
            await job()
            results[name] = "ok"
            logger.info("Scheduled job %s completed", name)
        except Exception as exc:
            results[name] = f"failed: {exc}"
            logger.exception("Scheduled job %s failed", name)

    return {"ran": results}
