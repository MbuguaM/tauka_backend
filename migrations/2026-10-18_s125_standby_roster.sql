-- ============================================================================
-- §125 (2026-10-18)  Standby roster and tutor no-shows (enrolment plan 8.6, 4.8)
-- ============================================================================
--
-- tutor_management domain (tutor_standby_window) and cohort_matching
-- (session_cover_request, §123); tauka-python writes both.
--
-- Decided 2026-10-18 (developer): build the full roster now (the plan's §5 said
-- run it by hand twice first; the developer chose to automate).
--   * A tutor commits weekly STANDBY windows (their own zone). A tutor on
--     standby at a session's start is offered a no-show first.
--   * A learner in the session, or ops, reports "my tutor isn't here" from the
--     start until 15 minutes after. That opens a cover request with
--     reason_code 'no_show' (reported_by = the reporter; requested_by stays the
--     absent lead). It is offered at once: standby tutors first, then the
--     pod's guests, then any free tutor in every learner's pool. One may take
--     it until 20 minutes after the start.
--   * The absent tutor gets a `no_show` reliability mark only when the session
--     is covered or fails, never when they turn up (they or ops withdraw it).
--   * Reliability score (display only, ops): 100 - 10 per late_drop - 25 per
--     no_show + 5 per cover_taken over 90 days, clamped 0..100. Nothing reads it
--     to decide pay or matching.

ALTER TABLE app.session_cover_request DROP CONSTRAINT IF EXISTS session_cover_request_reason_code_check;
ALTER TABLE app.session_cover_request ADD CONSTRAINT session_cover_request_reason_code_check
  CHECK (reason_code IN ('ill','emergency','clash','other','no_show'));
ALTER TABLE app.session_cover_request ADD COLUMN IF NOT EXISTS reported_by uuid;
ALTER TABLE app.session_cover_request ADD CONSTRAINT session_cover_request_no_show
  CHECK ((reason_code = 'no_show') = (reported_by IS NOT NULL));

CREATE TABLE IF NOT EXISTS app.tutor_standby_window (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id    uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  day_of_week smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),   -- 0 = Monday, as tutor_availability
  start_time  time NOT NULL,
  end_time    time NOT NULL,
  timezone    text NOT NULL DEFAULT 'UTC',
  active      boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tutor_standby_window_span CHECK (end_time > start_time OR end_time = '00:00')
);
CREATE INDEX IF NOT EXISTS idx_tutor_standby_window_tutor ON app.tutor_standby_window (tutor_id) WHERE active;

ALTER TABLE app.tutor_standby_window ENABLE ROW LEVEL SECURITY;
CREATE POLICY tutor_standby_window_read ON app.tutor_standby_window FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.tutor_standby_window FROM anon, authenticated;
GRANT SELECT ON app.tutor_standby_window TO authenticated;
GRANT ALL ON app.tutor_standby_window TO service_role;
COMMENT ON TABLE app.tutor_standby_window IS 'Weekly standby windows a tutor commits to (plan 8.6). A tutor on standby is offered a no-show first. tauka-python writes; the tutor and admins read.';
COMMENT ON COLUMN app.session_cover_request.reported_by IS 'For a no_show (plan 8.6): the learner or ops admin who reported the absent tutor. NULL for an ordinary cover request.';
