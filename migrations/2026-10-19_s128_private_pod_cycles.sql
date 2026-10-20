-- ============================================================================
-- §128 (2026-10-19)  Forming a private pod in software (enrolment plan 3.2)
-- ============================================================================
--
-- cohort_matching domain (intake_cycle); tauka-python writes.
--
-- A private-pod group (enrolment_group kind private_pod, 2-4 learners) is
-- formed by ops: a tutor and a catalogue slot. tauka-python creates ONE
-- `forming` pod sized to the group and an ordinary 24-hour offer to each
-- member, under a cycle of mode 'private'. Accepting is the usual flow
-- (membership, teaching link, in_pod, placement email). The pod goes live once
-- every member has accepted; if anyone declines or lets it expire, the pod is
-- dissolved and the members go back to the wait pool (refill_service closes
-- these cycles, like refills).
--
-- A private cycle forms a pod, so the dry-run check now allows it to count one.

ALTER TABLE app.intake_cycle DROP CONSTRAINT IF EXISTS intake_cycle_mode_check;
ALTER TABLE app.intake_cycle ADD CONSTRAINT intake_cycle_mode_check
  CHECK (mode IN ('dry_run', 'live', 'refill', 'private'));
ALTER TABLE app.intake_cycle DROP CONSTRAINT IF EXISTS intake_cycle_dry_run_check;
ALTER TABLE app.intake_cycle ADD CONSTRAINT intake_cycle_dry_run_check
  CHECK (mode IN ('live', 'private') OR cohorts_formed = 0);
COMMENT ON CONSTRAINT intake_cycle_mode_check ON app.intake_cycle IS
  'refill (§112, plan 3.6): seat refills into live pods, forms nothing. private (§128, plan 3.2): one ops-formed private pod and its members'' offers.';
