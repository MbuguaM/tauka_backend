-- ============================================================================
-- §111 (2026-10-18)  Pausing (enrolment plan 4.4, §2c)
-- ============================================================================
--
-- enrolment domain (tauka-python).
--
-- §2c: "A cancellation or pause takes effect at the end of the learner's paid
-- month. The pod seat is refilled at the next checkpoint." Decided 2026-10-18
-- (developer):
--   * a paused month costs nothing and gives Free access (lessons already
--     unlocked stay unlocked; no live sessions, no new unlocks);
--   * a pause lasts at most 3 months, then the learner is treated as cancelled;
--   * coming back (paying again) puts the learner in the wait pool, first
--     priority at the next formation at their level, in whatever season.
--
-- One row per pause. scheduled (asked; billing stops at the period end) ->
-- active (the period ended: out of the pod, enrolment `paused`) -> resumed
-- (paid again) | lapsed (3 months passed: enrolment `cancelled`). A scheduled
-- pause can be withdrawn before it starts.

CREATE TABLE IF NOT EXISTS app.enrolment_pause (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id       uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  status           text NOT NULL DEFAULT 'scheduled'
                     CHECK (status IN ('scheduled','active','withdrawn','resumed','lapsed')),
  requested_at     timestamptz NOT NULL DEFAULT now(),
  -- The end of the paid month: when billing stops and the pause begins.
  effective_at     timestamptz NOT NULL,
  -- effective_at + 3 months: after this an active pause lapses into a cancellation.
  ends_by          timestamptz NOT NULL,
  started_at       timestamptz,
  ended_at         timestamptz,
  -- The pod the learner left when the pause began (for the record and for ops).
  left_cohort_id   uuid REFERENCES app.cohorts(id) ON DELETE SET NULL,
  reminder_sent_at timestamptz,
  CONSTRAINT enrolment_pause_window CHECK (
    ends_by > effective_at AND ends_by <= effective_at + interval '3 months 1 day'),
  CONSTRAINT enrolment_pause_started CHECK ((status IN ('scheduled','withdrawn')) = (started_at IS NULL)),
  CONSTRAINT enrolment_pause_ended CHECK ((status IN ('scheduled','active')) = (ended_at IS NULL))
);
-- At most one open pause per learner.
CREATE UNIQUE INDEX IF NOT EXISTS idx_enrolment_pause_one_open
  ON app.enrolment_pause (student_id) WHERE status IN ('scheduled','active');
CREATE INDEX IF NOT EXISTS idx_enrolment_pause_due ON app.enrolment_pause (status, effective_at, ends_by);

-- The lifecycle, and nothing but the review columns may change.
CREATE OR REPLACE FUNCTION app.enrolment_pause_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status, NEW.status) IN (
       ('scheduled','active'), ('scheduled','withdrawn'), ('active','resumed'), ('active','lapsed')) THEN
    RAISE EXCEPTION 'enrolment_pause: % -> % is not allowed', OLD.status, NEW.status
      USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.student_id, NEW.requested_at, NEW.effective_at, NEW.ends_by)
     IS DISTINCT FROM (OLD.student_id, OLD.requested_at, OLD.effective_at, OLD.ends_by) THEN
    RAISE EXCEPTION 'enrolment_pause: the dates are fixed'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_enrolment_pause_guard ON app.enrolment_pause;
CREATE TRIGGER trg_enrolment_pause_guard BEFORE UPDATE ON app.enrolment_pause
  FOR EACH ROW EXECUTE FUNCTION app.enrolment_pause_guard();

ALTER TABLE app.enrolment_pause ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS enrolment_pause_read ON app.enrolment_pause;
CREATE POLICY enrolment_pause_read ON app.enrolment_pause FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.has_admin_capability('ops'));
REVOKE ALL ON app.enrolment_pause FROM anon, authenticated;
GRANT SELECT ON app.enrolment_pause TO authenticated;
GRANT ALL ON app.enrolment_pause TO service_role;

COMMENT ON TABLE app.enrolment_pause IS
  'A learner''s pause (plan 4.4, §2c): starts at the end of the paid month, Free access, at '
  'most 3 months, then cancelled; paying again resumes into the wait pool. Written by '
  'tauka-python; the learner and ops read.';
-- Applied to production 2026-10-18 in three parts (the MCP tool refused the whole
-- payload): s111_enrolment_pause, s111_enrolment_pause_guard, s111_enrolment_pause_rls.
-- The comment text there reads "A learner pause" (no apostrophe).
