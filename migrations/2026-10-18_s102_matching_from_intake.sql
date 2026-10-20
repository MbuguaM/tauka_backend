-- ============================================================================
-- §102 (2026-10-18)  Matching runs from the intake calendar (plan 3.4, E-02, E-13)
-- ============================================================================
--
-- E-02 (adopted §2i): formation follows the explicit `app.intake` calendar, not
-- a Gregorian day-of-month rule, so `match.intake_days` ("1,15") is RETIRED.
-- A live intake is matched on `cutoff_closes + match.run_days_after_cutoff`
-- (default 1: Tir closes 5 Jan, matches 6 Jan, forms 9 Jan). A rehearsal intake
-- gets a dry run only. E-13: offers are held for 24 hours, not 48, so a first
-- round, its expiry and a second round all fit before formation day.
--
-- cohort_matching (intake_cycle) and platform_config (config) are both
-- tauka-python's.

ALTER TABLE app.intake_cycle
  ADD COLUMN IF NOT EXISTS intake_id uuid REFERENCES app.intake(id) ON DELETE SET NULL;
COMMENT ON COLUMN app.intake_cycle.intake_id IS
  'The formation this run matched for (§102). NULL for ad-hoc admin dry runs and for '
  'runs before the calendar existed.';
-- One live run per intake: a second would re-freeze a pool the first already
-- placed people out of (the same reason as uq_intake_cycle_live_date).
CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_cycle_live_intake
  ON app.intake_cycle (intake_id) WHERE mode = 'live' AND intake_id IS NOT NULL;

UPDATE app.config SET value = '24' WHERE key = 'match.offer_ttl_hours';
INSERT INTO app.config (key, value)
SELECT 'match.run_days_after_cutoff', '1'
 WHERE NOT EXISTS (SELECT 1 FROM app.config WHERE key = 'match.run_days_after_cutoff');
DELETE FROM app.config WHERE key = 'match.intake_days';
-- Applied to production 2026-10-18 as migration s102_matching_from_intake.
