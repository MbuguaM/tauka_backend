-- ============================================================================
-- §118 (2026-10-18)  First-session norms and the two-week fit survey
--                    (enrolment plan 4.1)
-- ============================================================================
--
-- cohort_matching domain (tauka-python).
--
-- Playbook step 12: "The tutor runs introductions and agrees pod norms
-- (camera, lateness, language use)." Step 13: "A short survey after session
-- two checks fit; serious mismatches are moved before the first checkpoint."
-- Decided 2026-10-18 (developer):
--   * norms are FIXED CHOICES the tutor records; every member sees them;
--   * the survey is four taps — level, time, group, staying — no free text;
--   * a mismatch is FLAGGED for ops, who may move the learner to the wait pool
--     (first priority, refill offers); nothing moves automatically;
--   * only ops sees a learner's answers, never the tutor.
--
-- `mismatch` is a stored generated column so the ops queue and any count read
-- the same rule.

CREATE TABLE IF NOT EXISTS app.pod_norms (
  cohort_id  uuid PRIMARY KEY REFERENCES app.cohorts(id) ON DELETE CASCADE,
  camera     text NOT NULL CHECK (camera IN ('on','when_possible','optional')),
  lateness   text NOT NULL CHECK (lateness IN ('on_time','door_5','door_10')),
  language   text NOT NULL CHECK (language IN ('amharic_only','amharic_first','either')),
  agreed_by  uuid NOT NULL,
  agreed_at  timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.pod_fit_survey (
  cohort_id   uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  student_id  uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  level_fit   text NOT NULL CHECK (level_fit IN ('too_easy','right','too_hard')),
  time_fit    text NOT NULL CHECK (time_fit IN ('works','sometimes','doesnt_work')),
  group_fit   text NOT NULL CHECK (group_fit IN ('comfortable','mostly','not_comfortable')),
  staying     text NOT NULL CHECK (staying IN ('stay','not_sure','move')),
  mismatch    boolean GENERATED ALWAYS AS (
                level_fit <> 'right' OR time_fit = 'doesnt_work'
                OR group_fit = 'not_comfortable' OR staying = 'move') STORED,
  -- Ops review of a mismatch: none (leave as is) | moved (to the wait pool).
  action      text CHECK (action IS NULL OR action IN ('none','moved')),
  reviewed_by uuid,
  reviewed_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (cohort_id, student_id),
  CONSTRAINT pod_fit_survey_reviewed CHECK ((action IS NULL) = (reviewed_at IS NULL AND reviewed_by IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_pod_fit_survey_open ON app.pod_fit_survey (created_at) WHERE mismatch AND action IS NULL;

ALTER TABLE app.pod_norms ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.pod_fit_survey ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS pod_norms_read ON app.pod_norms;
CREATE POLICY pod_norms_read ON app.pod_norms FOR SELECT TO authenticated
  USING (app.can_read_cohort(cohort_id));
DROP POLICY IF EXISTS pod_fit_survey_read ON app.pod_fit_survey;
CREATE POLICY pod_fit_survey_read ON app.pod_fit_survey FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.has_admin_capability('ops'));
REVOKE ALL ON app.pod_norms, app.pod_fit_survey FROM anon, authenticated;
GRANT SELECT ON app.pod_norms, app.pod_fit_survey TO authenticated;
GRANT ALL ON app.pod_norms, app.pod_fit_survey TO service_role;

COMMENT ON TABLE app.pod_norms IS 'The norms a pod agreed in its first session (plan 4.1): camera, lateness, language, as fixed choices. Recorded by its tutor via tauka-python; every member reads.';
COMMENT ON TABLE app.pod_fit_survey IS 'Each learner two-week fit survey (plan 4.1): four taps; mismatch flagged for ops, who may move them to the wait pool. Only the learner and ops read; never the tutor.';
-- Applied to production 2026-10-18 in two parts after a dry run: s118_pod_norms_fit_survey,
-- s118_pod_norms_fit_survey_rls.
