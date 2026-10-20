-- ============================================================================
-- §143 (2026-10-20)  Learner-language summaries (features addendum, Feature B)
-- ============================================================================
--
-- Course domain (lesson_summary*, tauka-python writes) and three columns on
-- user_profiles (tauka-flutter's table, X-15; the learner sets them from the
-- app's settings and onboarding, so the client may update its own row as it
-- already does for its other profile fields).
--
-- Behind 'lesson_summaries' (B1-B3, B5) and 'summary_questions' (B4), both 'coming'.
--
-- X-14  Flags go in lesson_summary_flag, not a `corrections` table (none exists).
-- X-16  The AI never writes Fidel: vocabulary is copied from the lesson JSON and
--       a post-check rejects a summary with Fidel the lesson doesn't contain.
--       Enforced in tauka-python (summary_service), recorded here in `checks`.
-- B4    One live summary per lesson per language; a new content_version (or a
--       flag) supersedes it and a regeneration is queued.

ALTER TABLE app.user_profiles ADD COLUMN IF NOT EXISTS first_language text;
ALTER TABLE app.user_profiles ADD COLUMN IF NOT EXISTS summary_language text;
ALTER TABLE app.user_profiles ADD COLUMN IF NOT EXISTS summaries_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE app.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_first_language_check;
ALTER TABLE app.user_profiles ADD CONSTRAINT user_profiles_first_language_check
  CHECK (first_language IS NULL OR first_language ~ '^[a-z]{2,3}$');
ALTER TABLE app.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_summary_language_check;
ALTER TABLE app.user_profiles ADD CONSTRAINT user_profiles_summary_language_check
  CHECK (summary_language IS NULL OR summary_language ~ '^[a-z]{2,3}$');
COMMENT ON COLUMN app.user_profiles.first_language IS 'ISO 639-1/3 code, asked at onboarding (§143, X-15). Feeds summaries (B2) and the exchange English-level match (A8.3).';

CREATE TABLE IF NOT EXISTS app.lesson_summary (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id         uuid NOT NULL REFERENCES app.unit(id) ON DELETE CASCADE,
  language        text NOT NULL CHECK (language ~ '^[a-z]{2,3}$'),
  content_version int  NOT NULL DEFAULT 1,
  -- {bullets:[..], vocabulary:[{fidel, translit, gloss}]}
  content_json    jsonb NOT NULL,
  model_version   text NOT NULL,
  status          text NOT NULL DEFAULT 'live' CHECK (status IN ('live','flagged','superseded')),
  generated_at    timestamptz NOT NULL DEFAULT now(),
  reviewed_by     uuid,
  reviewed_at     timestamptz
);
-- One live summary per lesson and language. A regeneration (new content
-- version, or after a flag) supersedes the old row rather than overwriting it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_summary_live ON app.lesson_summary (unit_id, language) WHERE status = 'live';

-- Display data only (E-16): nothing gates on it. Feeds B5's dashboard.
CREATE TABLE IF NOT EXISTS app.summary_event (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  unit_id    uuid REFERENCES app.unit(id) ON DELETE CASCADE,
  language   text NOT NULL,
  event      text NOT NULL CHECK (event IN ('view','expand','question','flag')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_summary_event_user_day ON app.summary_event (user_id, event, created_at);
CREATE INDEX IF NOT EXISTS idx_summary_event_lang ON app.summary_event (language, created_at);

CREATE TABLE IF NOT EXISTS app.lesson_summary_flag (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  summary_id  uuid NOT NULL REFERENCES app.lesson_summary(id) ON DELETE CASCADE,
  flagged_by  uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  reason      text CHECK (reason IS NULL OR length(reason) <= 500),
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  resolved_by uuid,
  outcome     text CHECK (outcome IS NULL OR outcome IN ('regenerated','kept','edited')),
  CONSTRAINT lesson_summary_flag_resolved CHECK ((resolved_at IS NULL) = (outcome IS NULL))
);

ALTER TABLE app.lesson_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.summary_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.lesson_summary_flag ENABLE ROW LEVEL SECURITY;
-- Live summaries are course content: any signed-in learner may read them.
CREATE POLICY lesson_summary_read ON app.lesson_summary FOR SELECT TO authenticated
  USING (status = 'live' OR app.is_admin());
CREATE POLICY summary_event_own ON app.summary_event FOR SELECT TO authenticated USING (user_id = auth.uid());
CREATE POLICY lesson_summary_flag_own ON app.lesson_summary_flag FOR SELECT TO authenticated
  USING (flagged_by = auth.uid() OR app.is_admin());
REVOKE ALL ON app.lesson_summary, app.summary_event, app.lesson_summary_flag FROM anon, authenticated;
GRANT SELECT ON app.lesson_summary, app.summary_event, app.lesson_summary_flag TO authenticated;
GRANT ALL ON app.lesson_summary, app.summary_event, app.lesson_summary_flag TO service_role;

COMMENT ON TABLE app.lesson_summary IS 'AI key points of a lesson in the learner''s language (features addendum B, §143). Fidel only from the lesson (X-16). tauka-python writes.';
COMMENT ON TABLE app.lesson_summary_flag IS 'A learner or tutor says a summary is wrong: it is hidden and regenerated (X-14). tauka-python writes.';

INSERT INTO app.feature_flag (key, status, description) VALUES
  ('lesson_summaries',  'coming', 'Features addendum B1-B3/B5: lesson key points in the learner''s language (§143).'),
  ('summary_questions', 'coming', 'Features addendum B4: ask about a lesson in your own language.')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app.config (key, value) VALUES
  ('summary.languages', '["he","ar","zh","de","fr"]'),
  ('summary.question_limit', '20')
ON CONFLICT (key) DO NOTHING;
