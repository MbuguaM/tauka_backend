-- ============================================================================
-- §120 (2026-10-18)  Tutor applications (enrolment plan §2j J1; CONFLICT-057; L-017)
-- ============================================================================
--
-- tutor_management domain (tauka-python), plus a CHECK on tauka-flutter's
-- app.tutor.status (CONFLICT-057, approved by the developer 2026-10-18).
--
-- §2j: a person applies on /teach; an admin reviews; ONLY APPROVAL MAKES A
-- TUTOR, and an approved tutor does no paid work until an admin activates
-- them. Approval (J3) is tauka-python, service role: it invites the person (or
-- promotes an existing account once any live subscription is cancelled — J1,
-- decided 2026-10-18), writes user_profiles.user_type = 'tutor' and inserts
-- app.tutor with status 'onboarding'. §94 still stops any client making itself
-- a tutor. Interviews are arranged outside the app (J2, decided).
--
-- 1. app.tutor_application. Statuses submitted -> under_review -> approved |
--    rejected (reason code); withdrawn by the applicant or an admin. One OPEN
--    application per email (a second submission updates it). Fields mirror the
--    existing /teach form. `delete_after` is set on rejection / withdrawal (+12
--    months) and swept daily (L-017).
-- 2. app.tutor.status CHECK: onboarding | active | suspended | offboarded. The
--    one production tutor is 'active'. Everything that earns money or meets
--    learners already filters on status = 'active', so 'onboarding' is safe.

CREATE TABLE IF NOT EXISTS app.tutor_application (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email               text NOT NULL CHECK (email = lower(email) AND position('@' in email) > 1),
  full_name           text NOT NULL CHECK (length(full_name) BETWEEN 1 AND 120),
  location            text CHECK (location IS NULL OR length(location) <= 120),
  languages           text NOT NULL CHECK (length(languages) BETWEEN 1 AND 300),
  experience_band     text CHECK (experience_band IS NULL OR experience_band IN ('none','1_2','3_5','5_plus')),
  specialisations     text[] NOT NULL DEFAULT '{}',
  demo_video_url      text CHECK (demo_video_url IS NULL OR demo_video_url ~ '^https?://'),
  motivation          text CHECK (motivation IS NULL OR length(motivation) <= 3000),
  source              text CHECK (source IS NULL OR length(source) <= 120),
  status              text NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','approved','rejected','withdrawn')),
  reject_reason_code  text CHECK (reject_reason_code IS NULL OR reject_reason_code IN
                        ('language_not_needed','fluency','demo','capacity','other')),
  admin_note          text CHECK (admin_note IS NULL OR length(admin_note) <= 2000),
  withdraw_token_hash text,
  reviewed_by         uuid,
  reviewed_at         timestamptz,
  tutor_id            uuid,
  delete_after        timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tutor_application_rejected_reason CHECK ((status = 'rejected') = (reject_reason_code IS NOT NULL)),
  CONSTRAINT tutor_application_approved_tutor CHECK ((status = 'approved') = (tutor_id IS NOT NULL)),
  CONSTRAINT tutor_application_decided CHECK (
    status IN ('submitted','withdrawn') OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_application_one_open
  ON app.tutor_application (email) WHERE status IN ('submitted','under_review');
CREATE INDEX IF NOT EXISTS idx_tutor_application_status ON app.tutor_application (status, created_at);
CREATE INDEX IF NOT EXISTS idx_tutor_application_sweep ON app.tutor_application (delete_after) WHERE delete_after IS NOT NULL;

-- The lifecycle: only forward, and an approved application is final.
CREATE OR REPLACE FUNCTION app.tutor_application_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status, NEW.status) IN (
       ('submitted','under_review'), ('submitted','approved'), ('submitted','rejected'), ('submitted','withdrawn'),
       ('under_review','approved'), ('under_review','rejected'), ('under_review','withdrawn')) THEN
    RAISE EXCEPTION 'tutor_application: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER trg_tutor_application_guard BEFORE UPDATE ON app.tutor_application
  FOR EACH ROW EXECUTE FUNCTION app.tutor_application_guard();

ALTER TABLE app.tutor_application ENABLE ROW LEVEL SECURITY;
CREATE POLICY tutor_application_admin_read ON app.tutor_application FOR SELECT TO authenticated
  USING (app.is_admin());
REVOKE ALL ON app.tutor_application FROM anon, authenticated;
GRANT SELECT ON app.tutor_application TO authenticated;
GRANT ALL ON app.tutor_application TO service_role;

-- 2. CONFLICT-057: the tutor's own status.
ALTER TABLE app.tutor DROP CONSTRAINT IF EXISTS tutor_status_check;
ALTER TABLE app.tutor ADD CONSTRAINT tutor_status_check
  CHECK (status IN ('onboarding','active','suspended','offboarded'));

COMMENT ON TABLE app.tutor_application IS 'An application to teach (plan §2j): submitted on /teach, reviewed by an admin; approval makes an onboarding tutor (tauka-python, CONFLICT-057). Rejected/withdrawn deleted 12 months after (L-017). Admins read; no client access.';
-- Applied to production 2026-10-18 in three parts after a dry run: s120_tutor_application,
-- s120_tutor_application_guard, s120_tutor_application_rls_tutor_status.
