-- ============================================================================
-- §127 (2026-10-19)  Phase 0 reminders by email (enrolment plan 1.3)
-- ============================================================================
--
-- enrolment domain; tauka-python writes.
--
-- A learner in Phase 0 (state phase0 or paid, not finished) gets an email
-- nudge every 7 days from their start, and reminders 14 and 3 days before
-- their target intake's lesson deadline (cutoff_closes). WhatsApp waits on
-- L-011. Each send is CLAIMED first (a row here), removed if the send fails, so
-- no reminder goes twice. A one-click link (an HMAC of the learner id, nothing
-- stored) turns them off: `enrolment_state.reminders_off_at`.

ALTER TABLE app.enrolment_state ADD COLUMN IF NOT EXISTS reminders_off_at timestamptz;
COMMENT ON COLUMN app.enrolment_state.reminders_off_at IS 'Phase 0 reminder emails turned off by the learner (plan 1.3, §127). NULL = on.';

CREATE TABLE IF NOT EXISTS app.phase0_reminder (
  student_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  intake_id  uuid NOT NULL REFERENCES app.intake(id) ON DELETE CASCADE,
  kind       text NOT NULL CHECK (kind ~ '^(weekly_[0-9]{1,2}|deadline_14|deadline_3)$'),
  sent_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, intake_id, kind)
);

ALTER TABLE app.phase0_reminder ENABLE ROW LEVEL SECURITY;
CREATE POLICY phase0_reminder_read ON app.phase0_reminder FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.phase0_reminder FROM anon, authenticated;
GRANT SELECT ON app.phase0_reminder TO authenticated;
GRANT ALL ON app.phase0_reminder TO service_role;
COMMENT ON TABLE app.phase0_reminder IS 'One row per Phase 0 reminder email sent (plan 1.3, §127): weekly_<n> or deadline_14 / deadline_3, per target intake. Claimed before sending. tauka-python writes.';
