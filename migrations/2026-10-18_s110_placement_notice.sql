-- ============================================================================
-- §110 (2026-10-18)  Placement notices (enrolment plan 3.5)
-- ============================================================================
--
-- cohort_matching domain (tauka-python).
--
-- When a pod goes live (or a learner accepts a seat in one that already is),
-- each member gets one email: pod, home tutor, the slot in their own time, the
-- season, the first sessions as an .ics attachment, and the pre-session
-- checklist. Email is the channel: push is suspended in the app (FC-008) and
-- WhatsApp (6.1) isn't built.
--
-- This row is the "sent once" record. cohort_memberships belongs to
-- tauka-flutter and CONFLICT-048 limits tauka-python to its status columns, so
-- the stamp lives here. tauka-python inserts the row BEFORE sending (the claim,
-- so a retried job or a parallel accept never mails twice) and deletes it if
-- the send fails, so the daily job tries again.

CREATE TABLE IF NOT EXISTS app.placement_notice (
  cohort_id  uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  student_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  sent_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (cohort_id, student_id)
);

ALTER TABLE app.placement_notice ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS placement_notice_read ON app.placement_notice;
CREATE POLICY placement_notice_read ON app.placement_notice FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.has_admin_capability('ops'));
REVOKE ALL ON app.placement_notice FROM anon, authenticated;
GRANT SELECT ON app.placement_notice TO authenticated;
GRANT ALL ON app.placement_notice TO service_role;

COMMENT ON TABLE app.placement_notice IS
  'One placement email per learner per pod (plan 3.5): claimed before sending, removed on a '
  'failed send. Written by tauka-python; the learner and ops read.';
-- Applied to production 2026-10-18 as migration s110_placement_notice.
