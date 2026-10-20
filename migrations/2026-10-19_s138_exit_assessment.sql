-- ============================================================================
-- §138 (2026-10-19)  Exit assessments, certificates, the milestone guarantee
--                    (enrolment plan Track 10; E-10, E-16; §2b)
-- ============================================================================
--
-- New `assessment` tables (the plan's proposed domain, kept inside enrolment
-- for now: tauka-python writes all of them). Behind the 'exit_assessment' flag.
-- The first B2 assessment is due around Nov 2027.
--
-- exit_assessment: a learner who has completed lesson 60 (B2) asks; ops books
--   two raters at rung 4 who are NOT the learner's own tutors (enforced in
--   tauka-python, as the plan asks); each rates listening, speaking, reading and
--   writing on the CEFR scale and gives an overall verdict. Pass = both raters
--   at the level. Live, not recorded (L-004).
-- certificate: on a pass, with a short public verification code
--   (/verify/<code>: name, level, date — nothing else).
-- guarantee_claim: §2b's milestone guarantee is Intensive's only. A "not yet"
--   for an Intensive learner whose attendance was >= 85% (tutor-marked pod
--   attendance and peer-session attendance — E-16; never telemetry) earns up
--   to 2 free months to try again.

CREATE TABLE IF NOT EXISTS app.exit_assessment (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  level         text NOT NULL CHECK (level IN ('B2','C1')),
  status        text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','scheduled','done','cancelled')),
  scheduled_at  timestamptz,
  rater_1       uuid,
  rater_2       uuid,
  rating_1      jsonb,
  rating_2      jsonb,
  outcome       text CHECK (outcome IS NULL OR outcome IN ('pass','not_yet')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  decided_at    timestamptz,
  CONSTRAINT exit_assessment_two_raters CHECK (rater_1 IS NULL OR rater_2 IS NULL OR rater_1 <> rater_2),
  CONSTRAINT exit_assessment_done CHECK ((status = 'done') = (outcome IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exit_assessment_one_open
  ON app.exit_assessment (student_id, level) WHERE status IN ('requested','scheduled');

CREATE TABLE IF NOT EXISTS app.certificate (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  assessment_id uuid NOT NULL UNIQUE REFERENCES app.exit_assessment(id) ON DELETE CASCADE,
  level         text NOT NULL CHECK (level IN ('B2','C1')),
  verify_code   text NOT NULL UNIQUE CHECK (verify_code ~ '^[A-HJ-NP-Z2-9]{10}$'),
  holder_name   text NOT NULL CHECK (length(holder_name) BETWEEN 1 AND 160),
  issued_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at    timestamptz
);

CREATE TABLE IF NOT EXISTS app.guarantee_claim (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id     uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  assessment_id  uuid NOT NULL UNIQUE REFERENCES app.exit_assessment(id) ON DELETE CASCADE,
  attendance_bp  int  NOT NULL CHECK (attendance_bp BETWEEN 0 AND 10000),
  months_granted int  NOT NULL CHECK (months_granted BETWEEN 0 AND 2),
  status         text NOT NULL DEFAULT 'granted' CHECK (status IN ('granted','applied','declined')),
  applied_ref    text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE app.exit_assessment ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.certificate ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.guarantee_claim ENABLE ROW LEVEL SECURITY;
CREATE POLICY exit_assessment_read ON app.exit_assessment FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR rater_1 = auth.uid() OR rater_2 = auth.uid() OR app.is_admin());
CREATE POLICY certificate_read ON app.certificate FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
CREATE POLICY guarantee_claim_read ON app.guarantee_claim FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.exit_assessment, app.certificate, app.guarantee_claim FROM anon, authenticated;
GRANT SELECT ON app.exit_assessment, app.certificate, app.guarantee_claim TO authenticated;
GRANT ALL ON app.exit_assessment, app.certificate, app.guarantee_claim TO service_role;
COMMENT ON TABLE app.exit_assessment IS 'B2/C1 exit assessment (Track 10): two rung-4 raters who are not the learner''s tutors. tauka-python writes.';
COMMENT ON TABLE app.certificate IS 'A certificate with a public verification code (Track 10). Verified at /verify/<code> through tauka-python.';
COMMENT ON TABLE app.guarantee_claim IS 'The Intensive milestone guarantee (§2b): up to 2 free months after a not-yet at >= 85% attendance. tauka-python writes.';
