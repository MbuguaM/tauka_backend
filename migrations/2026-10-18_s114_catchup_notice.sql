-- ============================================================================
-- §114 (2026-10-18)  Catch-up for an absent learner (enrolment plan 4.8)
-- ============================================================================
--
-- cohort_matching domain (tauka-python).
--
-- Plan 4.8: "absent learner gets notes and an async task". Decided 2026-10-18:
-- the task is the practice of the lesson the session covered
-- (cohort_session.lesson_ref) — existing exercises, no extra tutor work. Sent
-- by email once the tutor has written notes, or 48 hours after the session
-- either way, to every member marked absent or excused.
--
-- The once-only record lives here. `cohort_session_attendance.catchup_sent`
-- (tauka-flutter's table, undocumented, never written) looks made for this,
-- but tauka-python has no shared-write approval on that table, and this
-- needed none: a table of its own in its own domain.
--
-- Also decided: a pod session RUNS with any learner present (one is enough;
-- "runs with >= 2" means never cancelled for low turnout) — no schema. Tutor
-- no-shows wait for the standby roster (plan 8.6).

CREATE TABLE IF NOT EXISTS app.catchup_notice (
  cohort_session_id uuid NOT NULL REFERENCES app.cohort_session(id) ON DELETE CASCADE,
  student_id        uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  sent_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (cohort_session_id, student_id)
);
ALTER TABLE app.catchup_notice ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS catchup_notice_read ON app.catchup_notice;
CREATE POLICY catchup_notice_read ON app.catchup_notice FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.has_admin_capability('ops'));
REVOKE ALL ON app.catchup_notice FROM anon, authenticated;
GRANT SELECT ON app.catchup_notice TO authenticated;
GRANT ALL ON app.catchup_notice TO service_role;
COMMENT ON TABLE app.catchup_notice IS
  'One catch-up email per absent or excused learner per pod session (plan 4.8): the tutor notes and the lesson practice. Written by tauka-python.';
-- Applied to production 2026-10-18 as migration s114_catchup_notice.
