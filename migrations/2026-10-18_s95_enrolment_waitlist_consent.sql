-- Applied to production 2026-10-18 as migrations s95_enrolment_waitlist_consent
-- and s95b_enrolment_grants. Canonical history: tauka/lib/src/models/additional_changes.sql.
-- ============================================================================
-- §95 (2026-10-18)  Enrolment domain, first slice: waitlist_entry + consent_record
-- ============================================================================
--
-- DOMAIN: enrolment (NEW — approved by the developer 2026-10-18). tauka-python
-- owns the DDL and every write: the waitlist is a public endpoint and a consent
-- is a server-recorded answer. Enrolment plan Track H2 (waitlist v0) and H8
-- (consents v0). Flow: shared-features/flows/enrolment.md.

-- ── waitlist_entry ─────────────────────────────────────────────────────────
-- A visitor with no account asking to hear about the next intake. Email only:
-- no phone or WhatsApp until L-011 is settled, nothing about a child (L-012).
CREATE TABLE IF NOT EXISTS app.waitlist_entry (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Stored lower-cased so a plain UNIQUE works as the upsert target (PostgREST
  -- cannot target an expression index).
  email                text NOT NULL UNIQUE
                         CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 320 AND position('@' in email) > 1),
  -- user_profiles.learning_goal's vocabulary (§85.5), enforced here because
  -- this table, unlike that one, is written from a public form.
  goal                 text CHECK (goal IS NULL OR goal IN
                         ('conversation','family','travel','heritage','business','exam')),
  band                 text CHECK (band IS NULL OR band IN ('americas','europe','ea_gulf','asia')),
  pace_hours           int  CHECK (pace_hours IS NULL OR pace_hours IN (5, 10, 20)),
  -- Explicit and unticked by default. false = keep the entry for an admin to
  -- see, but do not email them about intakes.
  email_opt_in         boolean NOT NULL DEFAULT false,
  source               text CHECK (source IS NULL OR source IN ('home_hero','pricing','other')),
  banner_arm           text CHECK (banner_arm IS NULL OR length(banner_arm) <= 32),
  converted_student_id uuid REFERENCES app.student(id) ON DELETE SET NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  -- 12 months from the latest submission; a re-submission renews it.
  delete_after         timestamptz NOT NULL DEFAULT now() + interval '12 months'
);
CREATE INDEX IF NOT EXISTS idx_waitlist_entry_sweep
  ON app.waitlist_entry (delete_after) WHERE converted_student_id IS NULL;

DROP TRIGGER IF EXISTS trg_waitlist_entry_updated_at ON app.waitlist_entry;
CREATE TRIGGER trg_waitlist_entry_updated_at
  BEFORE UPDATE ON app.waitlist_entry
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- No client access at all. Admins read; the service role writes (POST /waitlist).
ALTER TABLE app.waitlist_entry ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS waitlist_entry_admin_read ON app.waitlist_entry;
CREATE POLICY waitlist_entry_admin_read ON app.waitlist_entry
  FOR SELECT TO authenticated USING (app.is_admin());

COMMENT ON TABLE app.waitlist_entry IS
  'Visitors (no account) asking to hear about the next intake. Email only - no '
  'phone (L-011), nothing about a child (L-012). Written by tauka-python '
  'POST /waitlist only; admins read. Unconverted rows are deleted after '
  'delete_after by the daily waitlist_retention task.';

-- ── consent_record — append-only ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.consent_record (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_id     uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  -- More purposes (whatsapp, employer_reporting) arrive by migration.
  purpose        text NOT NULL CHECK (purpose IN
                   ('session_recording','pronunciation_profile','model_training')),
  granted        boolean NOT NULL,
  policy_version text NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 32),
  source         text NOT NULL CHECK (source IN ('signup','settings','admin')),
  at             timestamptz NOT NULL DEFAULT now()
);
-- The signup answer is given once per policy version. /auth/provision runs on
-- every sign-in until it succeeds, so a retried call must not append twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_record_one_signup_answer
  ON app.consent_record (subject_id, purpose, policy_version)
  WHERE source = 'signup';
CREATE INDEX IF NOT EXISTS idx_consent_record_current
  ON app.consent_record (subject_id, purpose, at DESC);

ALTER TABLE app.consent_record ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS consent_record_select_own ON app.consent_record;
CREATE POLICY consent_record_select_own ON app.consent_record
  FOR SELECT TO authenticated USING (subject_id = auth.uid());
DROP POLICY IF EXISTS consent_record_admin_read ON app.consent_record;
CREATE POLICY consent_record_admin_read ON app.consent_record
  FOR SELECT TO authenticated USING (app.is_admin());

-- The session_credit pattern: DELETE only as the cascade on account erasure.
CREATE OR REPLACE FUNCTION app.consent_record_append_only()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'app', 'pg_catalog'
AS $$
BEGIN
  IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
    RETURN OLD;  -- cascaded from DELETE on app.user_profiles (account erasure)
  END IF;
  RAISE EXCEPTION 'app.consent_record is append-only: a withdrawal is a new row with granted = false, never %', TG_OP;
END;
$$;
DROP TRIGGER IF EXISTS trg_consent_record_append_only ON app.consent_record;
CREATE TRIGGER trg_consent_record_append_only
  BEFORE UPDATE OR DELETE ON app.consent_record
  FOR EACH ROW EXECUTE FUNCTION app.consent_record_append_only();

-- The current answer is the latest row per purpose; no row = never asked =
-- not granted. SECURITY INVOKER: RLS limits a learner to their own answers.
CREATE OR REPLACE FUNCTION app.consent_granted(p_subject_id uuid, p_purpose text)
RETURNS boolean
LANGUAGE sql
STABLE
SET search_path TO 'app', 'pg_catalog'
AS $$
  SELECT COALESCE((
    SELECT granted FROM app.consent_record
     WHERE subject_id = p_subject_id AND purpose = p_purpose
     ORDER BY at DESC, id DESC
     LIMIT 1), false);
$$;

COMMENT ON TABLE app.consent_record IS
  'Append-only answers to per-purpose consents (session_recording, '
  'pronunciation_profile, model_training). Current state = latest row per '
  '(subject, purpose) via app.consent_granted(). Written by tauka-python only. '
  'Does NOT replace speech_submission.consented_to_training / train_ok, which '
  'stay per row (L-007).';

-- §95b  Clients get SELECT only (RLS still limits it). The schema's default
-- privileges grant anon/authenticated ALL, TRUNCATE included, which bypasses
-- RLS and the append-only trigger. See CONFLICT-053.
REVOKE ALL ON app.waitlist_entry, app.consent_record FROM anon, authenticated;
GRANT SELECT ON app.waitlist_entry, app.consent_record TO authenticated;
GRANT ALL ON app.waitlist_entry, app.consent_record TO service_role;