-- ============================================================================
-- §107 (2026-10-18)  Fidel approval (enrolment plan 1.4) + the voice bucket's
--                    missing access rules (FC-040)
-- ============================================================================
--
-- 1. FC-040. The `voice-recordings` bucket was created in §70 (FC-032) with NO
--    storage.objects policies, so every client upload has been refused — the
--    speaking-drill submissions included (app.speech_submission: 0 rows; the
--    bucket: 0 objects, checked 2026-10-18). Added here, mirroring the activity
--    bucket: a learner writes under `speech/<their uid>/…`; the learner and the
--    tutors app.can_read_activity() admits (the speech_submission_tutor_read
--    rule) read. No client update or delete: the 90-day sweep runs as service.
--
-- 2. speech_submission.section_kind gains 'fidelReading' (tauka-flutter's
--    table, exercises_progress): one take of a learner reading an alphabet
--    group aloud, through the existing pipeline and its 90-day sweep.
--
-- 3. consent_record gains purpose 'tutor_voice_review' and source 'in_app'.
--    L-006 recommended an explicit consent at first submission rather than
--    stretching the enrolment assumption (L-001) to voice; the developer chose
--    it 2026-10-18. A Fidel reading goes to a Tauka REVIEWER (rung >= 1), not
--    only the learner's own tutor, which is what the consent sentence says.
--
-- 4. app.fidel_approval (enrolment domain, tauka-python writes): one verdict
--    per submitted group reading — approved, or redo with what to practise.
--    Feedback, NOT a gate (§2d: finishing Phase 0 is what makes a learner
--    ready). Codes only. Outlives the audio (which is swept at 90 days).

-- ── 1. voice bucket access (FC-040) ────────────────────────────────────────
CREATE OR REPLACE FUNCTION app.voice_path_owner(p_name text)
RETURNS uuid
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN p_name ~ '^speech/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/'
    THEN substring(p_name from 8 for 36)::uuid
    ELSE NULL
  END;
$$;

DROP POLICY IF EXISTS voice_objects_insert ON storage.objects;
CREATE POLICY voice_objects_insert ON storage.objects FOR INSERT TO authenticated
  WITH CHECK (bucket_id = 'voice-recordings' AND app.voice_path_owner(name) = auth.uid());
DROP POLICY IF EXISTS voice_objects_read ON storage.objects;
CREATE POLICY voice_objects_read ON storage.objects FOR SELECT TO authenticated
  USING (bucket_id = 'voice-recordings' AND app.voice_path_owner(name) IS NOT NULL
         AND (app.voice_path_owner(name) = auth.uid() OR app.can_read_activity(app.voice_path_owner(name))));

-- ── 2. a Fidel reading is a speech submission ──────────────────────────────
ALTER TABLE app.speech_submission DROP CONSTRAINT IF EXISTS speech_submission_section_kind_check;
ALTER TABLE app.speech_submission ADD CONSTRAINT speech_submission_section_kind_check
  CHECK (section_kind IN ('pronunciationDrills','questionsAndAnswers','fidelReading'));

-- ── 3. the consent ─────────────────────────────────────────────────────────
ALTER TABLE app.consent_record DROP CONSTRAINT IF EXISTS consent_record_purpose_check;
ALTER TABLE app.consent_record ADD CONSTRAINT consent_record_purpose_check
  CHECK (purpose IN ('session_recording','pronunciation_profile','model_training','tutor_voice_review'));
ALTER TABLE app.consent_record DROP CONSTRAINT IF EXISTS consent_record_source_check;
ALTER TABLE app.consent_record ADD CONSTRAINT consent_record_source_check
  CHECK (source IN ('signup','settings','admin','in_app'));

-- ── 4. the verdicts ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.fidel_approval (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  unit_id       uuid NOT NULL,
  -- Which exercise group of the alphabet unit, 0-based in content order.
  group_index   int  NOT NULL CHECK (group_index BETWEEN 0 AND 20),
  group_title   text CHECK (group_title IS NULL OR length(group_title) <= 120),
  -- SET NULL: the audio is swept at 90 days; the verdict stays.
  submission_id uuid REFERENCES app.speech_submission(id) ON DELETE SET NULL,
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','redo','superseded','expired')),
  gap_codes     text[] NOT NULL DEFAULT '{}' CHECK (gap_codes <@ ARRAY[
                  'fidel_recognition','vowel_orders','ejectives','gemination','reading_fluency']::text[]),
  reviewed_by   uuid,
  reviewed_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fidel_approval_reviewed CHECK (
    (status IN ('approved','redo')) = (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
  CONSTRAINT fidel_approval_codes_on_redo CHECK (status = 'redo' OR gap_codes = '{}')
);
-- One reading awaiting review per learner per group: a re-submission
-- supersedes the waiting one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fidel_approval_one_pending
  ON app.fidel_approval (student_id, unit_id, group_index) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_fidel_approval_queue ON app.fidel_approval (created_at) WHERE status = 'pending';

CREATE OR REPLACE FUNCTION app.fidel_approval_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.status <> 'pending' THEN
    RAISE EXCEPTION 'fidel_approval: % is final', OLD.status USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.student_id, NEW.unit_id, NEW.group_index, NEW.created_at)
     IS DISTINCT FROM (OLD.student_id, OLD.unit_id, OLD.group_index, OLD.created_at) THEN
    RAISE EXCEPTION 'fidel_approval: only the verdict may change' USING ERRCODE = 'check_violation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_fidel_approval_guard ON app.fidel_approval;
CREATE TRIGGER trg_fidel_approval_guard BEFORE UPDATE OF status, gap_codes, reviewed_by, reviewed_at,
  student_id, unit_id, group_index, created_at ON app.fidel_approval
  FOR EACH ROW EXECUTE FUNCTION app.fidel_approval_guard();

ALTER TABLE app.fidel_approval ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS fidel_approval_select ON app.fidel_approval;
CREATE POLICY fidel_approval_select ON app.fidel_approval FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.fidel_approval FROM anon, authenticated;
GRANT SELECT ON app.fidel_approval TO authenticated;
GRANT ALL ON app.fidel_approval TO service_role;

COMMENT ON TABLE app.fidel_approval IS
  'A verdict on a learner reading one alphabet group aloud (plan 1.4): approved, or redo '
  'with codes. Feedback, not a gate (§2d). Reviewed by a rung >= 1 tutor or ops through '
  'tauka-python; the audio is the linked speech_submission (90-day sweep). §107.';
-- Applied to production 2026-10-18 as migration s107_fidel_approval.
