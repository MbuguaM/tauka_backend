-- ============================================================================
-- §124 (2026-10-18)  Tutor onboarding checklist (enrolment plan §2j J5, Track 8.1)
-- ============================================================================
--
-- tutor_management domain; tauka-python writes.
--
-- An approved tutor is `onboarding` until five steps are done: ID check,
-- references, contract, training, calibration. Decided 2026-10-18 (developer):
-- ops ticks each step with a date and a short reference code; calibration is a
-- score ops enters (agreement with the master reviewer, >= 85 passes). No
-- documents are stored in Tauka. onboarding -> active is refused until all five
-- are done (tutor_status_service).
--
-- Append-only: each row is a tick (done = true) or an untick (done = false, a
-- correction); a step's state is its latest row. No free text: `ref_code` is a
-- short code (e.g. a contract number), never a note.

CREATE TABLE IF NOT EXISTS app.tutor_onboarding_check (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id   uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  step       text NOT NULL CHECK (step IN ('id_check','references','contract','training','calibration')),
  done       boolean NOT NULL,
  score      int CHECK (score IS NULL OR score BETWEEN 0 AND 100),
  ref_code   text CHECK (ref_code IS NULL OR ref_code ~ '^[A-Za-z0-9_./-]{1,40}$'),
  actor      uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tutor_onboarding_check_score CHECK (
    (step = 'calibration' AND done AND score >= 85)
    OR (step = 'calibration' AND NOT done)
    OR (step <> 'calibration' AND score IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_tutor_onboarding_check_tutor ON app.tutor_onboarding_check (tutor_id, step, created_at);

CREATE OR REPLACE FUNCTION app.tutor_onboarding_check_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'tutor_onboarding_check is append-only' USING ERRCODE = 'check_violation';
END;
$$;
CREATE TRIGGER trg_tutor_onboarding_check_append_only BEFORE UPDATE OR DELETE ON app.tutor_onboarding_check
  FOR EACH ROW EXECUTE FUNCTION app.tutor_onboarding_check_append_only();

ALTER TABLE app.tutor_onboarding_check ENABLE ROW LEVEL SECURITY;
CREATE POLICY tutor_onboarding_check_read ON app.tutor_onboarding_check FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.tutor_onboarding_check FROM anon, authenticated;
GRANT SELECT ON app.tutor_onboarding_check TO authenticated;
GRANT ALL ON app.tutor_onboarding_check TO service_role;
COMMENT ON TABLE app.tutor_onboarding_check IS 'Append-only onboarding checklist (plan §2j J5): ID check, references, contract, training, calibration (score >= 85). Ops ticks via tauka-python; latest row per step wins. Activation needs all five.';
