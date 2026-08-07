-- =============================================================================
-- Tauka — app.test_questions → v4 question schema
-- Date:   2026-08-06
-- Owner:  tauka-python  (test_referral domain)
-- Spec:   Course creator/Amharic_website_test/QUESTION_SCHEMA.md
--         Course creator/Amharic_website_test/PLAN.md  (Phase 0.2)
--
-- WHY
--   The reauthored bank (tauka_question_bank_v4.json, 150 questions) carries
--   eight interaction modes whose answers are objects and arrays, not letters,
--   and human-readable ids. Three columns cannot hold it as typed:
--
--     id              uuid  → text   ids are "a1_v_001", and audio filenames
--                                    (audio/<id>_m.mp3) derive from them
--     correct_answer  text  → jsonb  match/bucket answers are objects,
--                                    order/rank/build answers are arrays
--     (new)           source_unit, explanation, distractor_rationale
--
--   `explanation` and `distractor_rationale` are REVIEWER-ONLY. They are never
--   shown to a test taker (explaining an answer to someone who got it right is
--   condescending, and this is an assessment, not a lesson) and are stripped
--   server-side by _strip_answers alongside correct_answer. They exist so a
--   reviewer can confirm the stated answer is right and the distractors honest.
--
-- SAFETY
--   app.test_questions and app.test_sessions are read and written by
--   tauka-python ONLY (shared-db/OWNERS.md). tauka-flutter and tauka-react-web
--   never touch either — the domain file explicitly forbids client reads of
--   test_questions to prevent answer scraping. Nothing FKs to test_questions.id;
--   test_sessions.question_ids is a plain array, so it is retyped in step 2.
--
--   Idempotent and safe to re-run.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1  test_questions.id : uuid → text
--    The bank's ids are stable, human-readable, and permanent — they key the
--    generated audio files, so they must never be renumbered.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'app' AND table_name = 'test_questions'
      AND column_name = 'id' AND data_type = 'uuid'
  ) THEN
    ALTER TABLE app.test_questions ALTER COLUMN id DROP DEFAULT;
    ALTER TABLE app.test_questions ALTER COLUMN id TYPE text USING id::text;
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2  test_sessions.question_ids : uuid[] → text[]
--    Must move with test_questions.id or every session lookup breaks.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'app' AND table_name = 'test_sessions'
      AND column_name = 'question_ids' AND udt_name = '_uuid'
  ) THEN
    ALTER TABLE app.test_sessions
      ALTER COLUMN question_ids TYPE text[] USING question_ids::text[];
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3  test_questions.correct_answer : text → jsonb
--    Existing letter answers ("C") become JSON strings ("C"), which the new
--    comparator still scores correctly for `choice`.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'app' AND table_name = 'test_questions'
      AND column_name = 'correct_answer' AND data_type <> 'jsonb'
  ) THEN
    ALTER TABLE app.test_questions
      ALTER COLUMN correct_answer TYPE jsonb USING to_jsonb(correct_answer);
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4  New columns
--    source_unit is a real column rather than jsonb because selection and
--    reporting group on it; digging into content jsonb for that is needless
--    friction. It supersedes the unused text column fsi_lesson_ref, which is
--    left in place rather than dropped (no data loss in a migration).
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE app.test_questions
  ADD COLUMN IF NOT EXISTS source_unit          int,
  ADD COLUMN IF NOT EXISTS explanation          text,
  ADD COLUMN IF NOT EXISTS distractor_rationale text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'test_questions_source_unit_positive'
  ) THEN
    ALTER TABLE app.test_questions
      ADD CONSTRAINT test_questions_source_unit_positive
      CHECK (source_unit IS NULL OR source_unit > 0);
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5  Selection index
--    _fetch_pool now reads the FULL candidate pool per bucket instead of the
--    old LIMIT 2 / LIMIT 5, which is what made bank growth invisible to
--    learners. This index keeps that read cheap.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_test_questions_selection
  ON app.test_questions (language, active, cefr_level, skill_area);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6  Column documentation
-- ─────────────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN app.test_questions.id IS
  'Stable human-readable id, e.g. a1_v_001. PERMANENT — audio filenames '
  '(audio/<id>_m.mp3) and scored sessions key on it. Never renumber, and never '
  '"correct" a level prefix after a CEFR retag.';
COMMENT ON COLUMN app.test_questions.question_type IS
  'Interaction mode: choice | multi_choice | order | rank | match | bucket | '
  'build | select_token. See QUESTION_SCHEMA.md §4.';
COMMENT ON COLUMN app.test_questions.correct_answer IS
  'jsonb. Shape follows question_type: string for choice/select_token, array '
  'for multi_choice/order/rank/build, object for match/bucket. NEVER sent to '
  'the client.';
COMMENT ON COLUMN app.test_questions.content IS
  'jsonb holding tag, prompt, stimulus and interaction. See QUESTION_SCHEMA.md §2.';
COMMENT ON COLUMN app.test_questions.source_unit IS
  'FSI course unit that introduces the tested feature.';
COMMENT ON COLUMN app.test_questions.explanation IS
  'REVIEWER-ONLY. Never shown to a test taker; stripped server-side before any '
  'question reaches the client.';
COMMENT ON COLUMN app.test_questions.distractor_rationale IS
  'REVIEWER-ONLY. Why each distractor is plausible. Never shown to a test taker '
  '— it leaks the key by implication. Stripped server-side.';

COMMIT;
