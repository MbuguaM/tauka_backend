-- ============================================================================
-- §133 (2026-10-19)  Communications (enrolment plan Track 6: 6.1, 6.3, 6.5; 3.5)
-- ============================================================================
--
-- platform_config (notice_log, whatsapp_contact): tauka-python writes.
-- broadcast (tauka-flutter's messaging domain): CONFLICT-061, see below.
--
-- notice_log: one row per (user, kind, ref) for every reminder or warning sent
-- by push / WhatsApp / email outside a feature's own table — session reminders
-- 24 h and 1 h before (hourly job), DST warnings, seat alerts. The unique key is
-- the once-only guard (claimed before sending).
--
-- whatsapp_contact (6.1, behind the whatsapp_reminders flag, L-011): a phone
-- number given FOR WhatsApp with an explicit opt-in, stored encrypted. Opting
-- out (in the account, or replying STOP) keeps the row with opted_out_at so we
-- never message them again by mistake.
--
-- Season channel (6.3, CONFLICT-061): broadcast.target_type gains 'season'
-- (target_id = app.season.id) and one ADDITIVE read policy: a learner sees a
-- season post when they hold an active seat in a pod formed in that season (the
-- pod's start_date is its intake's formation_date). Posted by tauka-python: a
-- welcome to each pod when it goes live, and one season post per formation.

CREATE TABLE IF NOT EXISTS app.notice_log (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id  uuid NOT NULL,
  kind     text NOT NULL CHECK (kind ~ '^[a-z0-9_]{3,40}$'),
  ref      text NOT NULL CHECK (length(ref) BETWEEN 1 AND 120),
  channels text[] NOT NULL DEFAULT '{}',
  sent_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_notice_log_sent ON app.notice_log (sent_at);
ALTER TABLE app.notice_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY notice_log_read ON app.notice_log FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.notice_log FROM anon, authenticated;
GRANT SELECT ON app.notice_log TO authenticated;
GRANT ALL ON app.notice_log TO service_role;
COMMENT ON TABLE app.notice_log IS 'Once-only log of reminders and warnings sent outside a feature''s own table (Track 6). tauka-python writes.';

CREATE TABLE IF NOT EXISTS app.whatsapp_contact (
  user_id         uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  phone_encrypted text NOT NULL,
  phone_last4     text NOT NULL CHECK (phone_last4 ~ '^[0-9]{4}$'),
  phone_hash      text NOT NULL,
  opted_in_at     timestamptz NOT NULL DEFAULT now(),
  opted_out_at    timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_whatsapp_contact_hash ON app.whatsapp_contact (phone_hash);
ALTER TABLE app.whatsapp_contact ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.whatsapp_contact FROM anon, authenticated;
GRANT ALL ON app.whatsapp_contact TO service_role;
COMMENT ON TABLE app.whatsapp_contact IS 'WhatsApp opt-in (plan 6.1, L-011): the phone encrypted, a hash to match inbound STOP. Service role only.';

-- CONFLICT-061: the season channel.
ALTER TABLE app.broadcast DROP CONSTRAINT IF EXISTS broadcast_target_type_check;
ALTER TABLE app.broadcast ADD CONSTRAINT broadcast_target_type_check
  CHECK (target_type IN ('program','level','cohort','course','season'));
CREATE POLICY broadcast_season_read ON app.broadcast FOR SELECT TO authenticated
  USING (
    target_type = 'season' AND deleted_at IS NULL AND (published_at IS NULL OR published_at <= now())
    AND EXISTS (
      SELECT 1 FROM app.cohort_memberships m
        JOIN app.cohorts c ON c.id = m.cohort_id
        JOIN app.intake i ON i.formation_date = c.start_date
       WHERE m.user_id = auth.uid() AND m.status = 'active' AND i.season_id = broadcast.target_id)
  );
