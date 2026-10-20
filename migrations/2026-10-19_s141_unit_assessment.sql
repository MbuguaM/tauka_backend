-- ============================================================================
-- §141 (2026-10-19)  Unit assessments (playbook "At each checkpoint", tracker)
-- ============================================================================
--
-- enrolment domain; tauka-python writes.
--
-- Decided 2026-10-19 (developer):
--   * one assessment per BLOCK of ten content lessons (after lessons 10, 20 ...
--     60; block 6 lines up with B2 = lesson 60);
--   * two parts: an auto-scored quiz in the app, built from the block's own
--     drills, and a short speaking check rated by one of the learner's tutors
--     (home, guest or pod tutor) or ops on a four-point rubric;
--   * the result INFORMS only: shown to the learner, their tutors and ops at
--     the checkpoint, and it nudges the tracker's projected date. It never
--     locks a lesson or moves anyone by itself (E-16).
--
-- A row is opened by tauka-python when the learner has completed the block's
-- last lesson. `outcome` is set once both parts are in: the weaker of the two
-- (quiz >= 80% secure, >= 60% developing; speaking mean >= 3 secure, >= 2
-- developing; otherwise not_yet).

CREATE TABLE IF NOT EXISTS app.unit_assessment (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  course_id     uuid NOT NULL,
  block         int  NOT NULL CHECK (block BETWEEN 1 AND 20),
  lesson_from   int  NOT NULL,
  lesson_to     int  NOT NULL,
  opened_at     timestamptz NOT NULL DEFAULT now(),
  quiz_total    int  CHECK (quiz_total IS NULL OR quiz_total BETWEEN 5 AND 40),
  quiz_correct  int  CHECK (quiz_correct IS NULL OR quiz_correct >= 0),
  quiz_at       timestamptz,
  weak_lessons  int[] NOT NULL DEFAULT '{}',
  speaking      jsonb,
  speaking_by   uuid,
  speaking_at   timestamptz,
  outcome       text CHECK (outcome IS NULL OR outcome IN ('secure','developing','not_yet')),
  decided_at    timestamptz,
  UNIQUE (student_id, course_id, block),
  CONSTRAINT unit_assessment_span CHECK (lesson_to >= lesson_from),
  CONSTRAINT unit_assessment_quiz CHECK (
    (quiz_total IS NULL) = (quiz_correct IS NULL) AND (quiz_total IS NULL) = (quiz_at IS NULL)
    AND (quiz_correct IS NULL OR quiz_correct <= quiz_total)),
  CONSTRAINT unit_assessment_speaking CHECK ((speaking IS NULL) = (speaking_at IS NULL)),
  CONSTRAINT unit_assessment_outcome CHECK (
    (outcome IS NULL) OR (quiz_at IS NOT NULL AND speaking_at IS NOT NULL AND decided_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_unit_assessment_open
  ON app.unit_assessment (student_id) WHERE outcome IS NULL;

ALTER TABLE app.unit_assessment ENABLE ROW LEVEL SECURITY;
CREATE POLICY unit_assessment_read ON app.unit_assessment FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.unit_assessment FROM anon, authenticated;
GRANT SELECT ON app.unit_assessment TO authenticated;
GRANT ALL ON app.unit_assessment TO service_role;
COMMENT ON TABLE app.unit_assessment IS
  'Unit assessment per block of 10 content lessons (playbook checkpoint review): an app quiz from the block''s drills + a tutor speaking check. Informs only; never gates. tauka-python writes; tutors read through tauka-python.';
