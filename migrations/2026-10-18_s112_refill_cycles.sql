-- ============================================================================
-- §112 (2026-10-18)  Seat refills (enrolment plan 3.6)
-- ============================================================================
--
-- cohort_matching domain (tauka-python).
--
-- A live pod with an empty seat (formed with three, or a member moved at a
-- checkpoint, paused or left) is offered to a waiting learner who fits it:
-- same level, a catalogue slot at the pod's time, lessons within the pod's
-- band. Offers are ordinary `match_offer` rows, so the learner accepts exactly
-- as at a formation — and an offer needs a cycle. A REFILL cycle is that
-- parent: at most one a day, never forms a cohort (the existing
-- `intake_cycle_dry_run_check` already pins cohorts_formed = 0 for any mode
-- other than live), and is closed by refill_service, not by the formation sweep
-- (close_cycle reads mode = 'live' only, so it can't dissolve a running pod).
--
-- When: during the first 14 days after a formation day (Ethiopic day 1-14).
-- For a new pod that is plan 3.6's "fourth_seat_closes_at = formation + 14
-- days"; every later month it is §2c's "the seat is refilled at the next
-- checkpoint". Computed, so no column is added to tauka-flutter's cohorts.

ALTER TABLE app.intake_cycle DROP CONSTRAINT IF EXISTS intake_cycle_mode_check;
ALTER TABLE app.intake_cycle ADD CONSTRAINT intake_cycle_mode_check
  CHECK (mode IN ('dry_run', 'live', 'refill'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_cycle_refill_date
  ON app.intake_cycle (cycle_date) WHERE mode = 'refill';
COMMENT ON CONSTRAINT intake_cycle_mode_check ON app.intake_cycle IS
  'refill (§112, plan 3.6): the parent of the day''s seat-refill offers into live pods; forms nothing.';
-- Applied to production 2026-10-18 as migration s112_refill_cycles.
