-- =====================================================================
-- PROPOSED DATABASE CHANGES
-- Amendments to full_database_definitions.sql conforming to app models
-- All in app schema with RLS, soft deletes, and indexes
-- =====================================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =====================================================================
-- 1. MODIFICATIONS TO EXISTING TABLES
-- =====================================================================

-- 1a. app.yt_playlist — add title and thumbnail_url
ALTER TABLE app.yt_playlist
  ADD COLUMN IF NOT EXISTS title text,
  ADD COLUMN IF NOT EXISTS thumbnail_url text;

-- 1b. app.yt_video — add display columns used by YouTubeVideo model
ALTER TABLE app.yt_video
  ADD COLUMN IF NOT EXISTS video_title text,
  ADD COLUMN IF NOT EXISTS thumbnail_url text,
  ADD COLUMN IF NOT EXISTS views_count text,
  ADD COLUMN IF NOT EXISTS channel_name text;

-- 1c. app.unit — add unit_order for ordering within a course
ALTER TABLE app.unit
  ADD COLUMN IF NOT EXISTS unit_order integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS section_type text DEFAULT 'content';

-- 1d. app.progress — fix unit_id from bigint to uuid to match app.unit.id
-- NOTE: requires migrating existing data. In production, back up first.
ALTER TABLE app.progress
  ALTER COLUMN unit_id TYPE uuid USING NULL::uuid;

-- 1e. app.unit — add deleted_at RLS index if missing
CREATE INDEX IF NOT EXISTS idx_unit_deleted_at ON app.unit(deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_unit_course_order ON app.unit(course_id, unit_order);

-- =====================================================================
-- 2. NEW TABLES
-- =====================================================================

-- ── Dictionary ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.dictionary (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  created_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  main_lang text NOT NULL,
  target_lang text NOT NULL,
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  created_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.dictionary ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view dictionaries"
  ON app.dictionary FOR SELECT
  USING (deleted_at IS NULL);

CREATE POLICY "Only admins can manage dictionaries"
  ON app.dictionary FOR ALL
  USING (auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL));

CREATE INDEX IF NOT EXISTS idx_dictionary_course_id ON app.dictionary(course_id);
CREATE INDEX IF NOT EXISTS idx_dictionary_deleted_at ON app.dictionary(deleted_at) WHERE deleted_at IS NULL;

-- ── Dictionary Entries ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.dictionary_entry (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dictionary_id uuid REFERENCES app.dictionary(id) ON DELETE CASCADE,
  word text NOT NULL,
  translation text NOT NULL,
  pronunciation text,
  part_of_speech text,
  example text,
  audio_url text,
  created_at timestamp with time zone DEFAULT now()
);

ALTER TABLE app.dictionary_entry ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view dictionary entries"
  ON app.dictionary_entry FOR SELECT
  USING (
    dictionary_id IN (SELECT id FROM app.dictionary WHERE deleted_at IS NULL)
  );

CREATE POLICY "Only admins can manage dictionary entries"
  ON app.dictionary_entry FOR ALL
  USING (auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL));

CREATE INDEX IF NOT EXISTS idx_dictionary_entry_dictionary_id ON app.dictionary_entry(dictionary_id);

-- ── Anki Decks ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.anki_deck (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  created_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.anki_deck ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own Anki decks"
  ON app.anki_deck FOR SELECT
  USING (deleted_at IS NULL AND (user_id = auth.uid() OR
    auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)));

CREATE POLICY "Users can manage their own Anki decks"
  ON app.anki_deck FOR ALL
  USING (user_id = auth.uid() AND deleted_at IS NULL);

CREATE INDEX IF NOT EXISTS idx_anki_deck_user_id ON app.anki_deck(user_id);
CREATE INDEX IF NOT EXISTS idx_anki_deck_course_id ON app.anki_deck(course_id);
CREATE INDEX IF NOT EXISTS idx_anki_deck_deleted_at ON app.anki_deck(deleted_at) WHERE deleted_at IS NULL;

-- ── Word Pairs (Anki Cards) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.word_pair (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  deck_id uuid REFERENCES app.anki_deck(id) ON DELETE CASCADE,
  main text NOT NULL,
  reveal text NOT NULL,
  difficulty integer DEFAULT 1 CHECK (difficulty BETWEEN 1 AND 4),
  last_reviewed timestamp with time zone,
  next_review timestamp with time zone,
  review_count integer DEFAULT 0,
  created_at timestamp with time zone DEFAULT now()
);

ALTER TABLE app.word_pair ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage word pairs in their decks"
  ON app.word_pair FOR ALL
  USING (
    deck_id IN (SELECT id FROM app.anki_deck WHERE user_id = auth.uid() AND deleted_at IS NULL)
  );

CREATE INDEX IF NOT EXISTS idx_word_pair_deck_id ON app.word_pair(deck_id);
CREATE INDEX IF NOT EXISTS idx_word_pair_next_review ON app.word_pair(deck_id, next_review);

-- ── Lyrics / Video Annotations ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.lyrics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_id uuid REFERENCES app.yt_video(id) ON DELETE CASCADE,
  json_file_path text NOT NULL,   -- Supabase storage path for the lyrics JSON
  title text,
  artist text,
  created_at timestamp with time zone DEFAULT now(),
  created_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.lyrics ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view lyrics"
  ON app.lyrics FOR SELECT
  USING (deleted_at IS NULL);

CREATE POLICY "Only admins can manage lyrics"
  ON app.lyrics FOR ALL
  USING (auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL));

CREATE INDEX IF NOT EXISTS idx_lyrics_video_id ON app.lyrics(video_id);
CREATE INDEX IF NOT EXISTS idx_lyrics_deleted_at ON app.lyrics(deleted_at) WHERE deleted_at IS NULL;

-- ── Programs ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.program (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  duration integer,               -- total weeks
  lessons_per_week integer,
  instructor_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  start_date timestamp with time zone,
  end_date timestamp with time zone,
  created_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL,
  CHECK (end_date > start_date)
);

ALTER TABLE app.program ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view programs"
  ON app.program FOR SELECT
  USING (deleted_at IS NULL);

CREATE POLICY "Only admins can manage programs"
  ON app.program FOR ALL
  USING (auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL));

CREATE INDEX IF NOT EXISTS idx_program_course_id ON app.program(course_id);
CREATE INDEX IF NOT EXISTS idx_program_instructor_id ON app.program(instructor_id);
CREATE INDEX IF NOT EXISTS idx_program_deleted_at ON app.program(deleted_at) WHERE deleted_at IS NULL;

-- ── Contemporary Content ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.contemporary (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  type text NOT NULL,             -- 'video', 'article', 'podcast', etc.
  created timestamp with time zone DEFAULT now(),
  created_by_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  content_url text,
  thumbnail_url text,
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.contemporary ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view contemporary content"
  ON app.contemporary FOR SELECT
  USING (deleted_at IS NULL);

CREATE POLICY "Admins can manage contemporary content"
  ON app.contemporary FOR ALL
  USING (auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL));

CREATE INDEX IF NOT EXISTS idx_contemporary_type ON app.contemporary(type);
CREATE INDEX IF NOT EXISTS idx_contemporary_deleted_at ON app.contemporary(deleted_at) WHERE deleted_at IS NULL;

-- ── Tutor Assignments ────────────────────────────────────────────────
-- Links tutors to courses they are assigned to teach (admin-managed).

CREATE TABLE IF NOT EXISTS app.tutor_assignment (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id uuid REFERENCES app.tutor(id) ON DELETE CASCADE,
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  assigned_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  assigned_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL,
  UNIQUE(tutor_id, course_id)
);

ALTER TABLE app.tutor_assignment ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Tutors can view their own assignments"
  ON app.tutor_assignment FOR SELECT
  USING (deleted_at IS NULL AND (
    tutor_id IN (SELECT id FROM app.tutor WHERE user_id = auth.uid())
    OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
  ));

CREATE POLICY "Admins can manage tutor assignments"
  ON app.tutor_assignment FOR ALL
  USING (auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL));

CREATE INDEX IF NOT EXISTS idx_tutor_assignment_tutor_id ON app.tutor_assignment(tutor_id);
CREATE INDEX IF NOT EXISTS idx_tutor_assignment_course_id ON app.tutor_assignment(course_id);

-- ── User Progress (per-unit, per-course) ─────────────────────────────
-- Replaces the existing app.progress table with proper UUID unit_id.

CREATE TABLE IF NOT EXISTS app.unit_progress (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id uuid REFERENCES app.unit(id) ON DELETE CASCADE,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'not_started'
      CHECK (status IN ('not_started', 'in_progress', 'completed')),
  completion_percentage numeric(5,2) DEFAULT 0.0
      CHECK (completion_percentage BETWEEN 0 AND 100),
  sections_completed jsonb DEFAULT '{}'::jsonb,
  started_at timestamp with time zone,
  completed_at timestamp with time zone,
  last_accessed_at timestamp with time zone DEFAULT now(),
  UNIQUE(unit_id, user_id)
);

ALTER TABLE app.unit_progress ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own unit progress"
  ON app.unit_progress FOR ALL
  USING (user_id = auth.uid());

CREATE POLICY "Tutors can view student progress for their courses"
  ON app.unit_progress FOR SELECT
  USING (
    course_id IN (
      SELECT ta.course_id FROM app.tutor_assignment ta
      JOIN app.tutor t ON t.id = ta.tutor_id
      WHERE t.user_id = auth.uid() AND ta.deleted_at IS NULL
    )
  );

CREATE INDEX IF NOT EXISTS idx_unit_progress_user_id ON app.unit_progress(user_id);
CREATE INDEX IF NOT EXISTS idx_unit_progress_unit_id ON app.unit_progress(unit_id);
CREATE INDEX IF NOT EXISTS idx_unit_progress_course_id ON app.unit_progress(course_id);
CREATE INDEX IF NOT EXISTS idx_unit_progress_status ON app.unit_progress(user_id, status);

-- ── Onboarding State ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.onboarding (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  known_languages jsonb DEFAULT '[]'::jsonb,
  user_type text,                 -- 'learner' or 'teacher'
  study_method text,
  teaching_options jsonb DEFAULT '[]'::jsonb,
  completed_at timestamp with time zone,
  created_at timestamp with time zone DEFAULT now()
);

ALTER TABLE app.onboarding ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own onboarding"
  ON app.onboarding FOR ALL
  USING (id = auth.uid());

-- =====================================================================
-- 3. RLS POLICY ADDITIONS FOR EXISTING TABLES
-- =====================================================================

-- Allow tutors to view student course enrollments for their courses
CREATE POLICY "Tutors can view student enrollments for their courses"
  ON app.student_course FOR SELECT
  USING (
    course_id IN (
      SELECT ta.course_id FROM app.tutor_assignment ta
      JOIN app.tutor t ON t.id = ta.tutor_id
      WHERE t.user_id = auth.uid() AND ta.deleted_at IS NULL
    )
  );

-- Students can insert their own enrollment
CREATE POLICY "Students can enroll in courses"
  ON app.student_course FOR INSERT
  WITH CHECK (student_id = auth.uid());

-- Students can manage their own enrollment
CREATE POLICY "Students can manage their own enrollment"
  ON app.student_course FOR DELETE
  USING (student_id = auth.uid());

-- =====================================================================
-- 4. HELPER FUNCTIONS
-- =====================================================================

-- Compute overall course completion percentage for a student
CREATE OR REPLACE FUNCTION app.get_course_completion(
  p_user_id uuid,
  p_course_id uuid
)
RETURNS numeric
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT COALESCE(
    ROUND(
      COUNT(*) FILTER (WHERE status = 'completed') * 100.0 / NULLIF(COUNT(*), 0),
      2
    ),
    0
  )
  FROM app.unit_progress
  WHERE user_id = p_user_id AND course_id = p_course_id;
$$;

-- Upsert unit progress
CREATE OR REPLACE FUNCTION app.upsert_unit_progress(
  p_user_id uuid,
  p_unit_id uuid,
  p_course_id uuid,
  p_status text,
  p_completion_percentage numeric,
  p_sections_completed jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_id uuid;
BEGIN
  INSERT INTO app.unit_progress (
    unit_id, user_id, course_id, status,
    completion_percentage, sections_completed, last_accessed_at
  )
  VALUES (
    p_unit_id, p_user_id, p_course_id, p_status,
    p_completion_percentage, p_sections_completed, now()
  )
  ON CONFLICT (unit_id, user_id) DO UPDATE SET
    status = EXCLUDED.status,
    completion_percentage = EXCLUDED.completion_percentage,
    sections_completed = EXCLUDED.sections_completed,
    last_accessed_at = now(),
    started_at = COALESCE(app.unit_progress.started_at, now()),
    completed_at = CASE
      WHEN EXCLUDED.status = 'completed' THEN now()
      ELSE app.unit_progress.completed_at
    END
  RETURNING id INTO v_id;

  RETURN v_id;
END;
$$;

-- =====================================================================
-- 5. STORAGE BUCKET POLICIES (reference — applied in Supabase dashboard)
-- =====================================================================
-- Bucket: unit-audio          → public read, admin write
-- Bucket: unit-json           → public read, admin write
-- Bucket: app-content/lyrics  → public read, admin write  (lyric JSON files)
-- Bucket: course-covers       → public read, admin write
-- Bucket: avatars             → authenticated read, owner write

-- =====================================================================
-- 6. VIDEO VENDOR ORCHESTRATION
-- Enables server-side control of which live-video SDK (Daily.co / Agora /
-- LiveKit) is used in the mobile client without a client release.
-- =====================================================================

-- 6a. lyric_file column on app.yt_video
--     Stores the basename of the lyric JSON file (e.g. "keber.json") so
--     the client can load the correct lyric file for each video.
ALTER TABLE app.yt_video
  ADD COLUMN IF NOT EXISTS lyric_file text;

CREATE INDEX IF NOT EXISTS idx_yt_video_lyric_file
  ON app.yt_video(lyric_file) WHERE lyric_file IS NOT NULL;

-- 6b. app.video_vendor_config
--     Exactly ONE row should be active (is_active = true) at any time.
--     The client reads this on startup and caches the result.
CREATE TABLE IF NOT EXISTS app.video_vendor_config (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor           text        NOT NULL DEFAULT 'daily'
                               CHECK (vendor IN ('daily', 'agora', 'livekit')),
  is_active        boolean     NOT NULL DEFAULT true,

  -- Name of the Supabase Vault secret holding the vendor API key / credentials.
  -- The raw key is NEVER stored in this table.
  api_key_secret_name text,

  -- Optional vendor-specific settings (e.g. {"ws_url": "wss://…"} for LiveKit)
  metadata         jsonb       NOT NULL DEFAULT '{}'::jsonb,

  region           text,

  -- Vendor to fall back to if the primary vendor is unavailable.
  fallback_vendor  text
                   CHECK (fallback_vendor IS NULL OR
                          fallback_vendor IN ('daily', 'agora', 'livekit')),

  -- Soft-delete so audit history is preserved.
  deleted_at       timestamptz,

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Enforce only one active row at a time via a partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_video_vendor_config_single_active
  ON app.video_vendor_config (is_active)
  WHERE is_active = true AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_video_vendor_config_active
  ON app.video_vendor_config (is_active, deleted_at)
  WHERE deleted_at IS NULL;

-- Auto-update updated_at on changes.
CREATE OR REPLACE FUNCTION app.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_video_vendor_config_updated_at
  BEFORE UPDATE ON app.video_vendor_config
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ── RLS ──────────────────────────────────────────────────────────────

ALTER TABLE app.video_vendor_config ENABLE ROW LEVEL SECURITY;

-- Authenticated users (clients) may read the active config.
CREATE POLICY "video_vendor_config_read_active"
  ON app.video_vendor_config
  FOR SELECT
  TO authenticated
  USING (
    is_active = true
    AND deleted_at IS NULL
  );

-- Only service_role / admins may write (insert / update / delete).
-- In Supabase, admin operations are performed via the service_role key
-- or a trusted Edge Function, never the anon key.
CREATE POLICY "video_vendor_config_admin_write"
  ON app.video_vendor_config
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

-- ── Seed row (initial default: Daily.co, no fallback) ────────────────
-- Run once after migration.  Comment out if re-running idempotently.
-- INSERT INTO app.video_vendor_config (vendor, is_active, region, fallback_vendor, metadata)
-- VALUES (
--   'daily',
--   true,
--   'us-east-1',
--   'agora',
--   '{"ws_url": null}'::jsonb
-- )
-- ON CONFLICT DO NOTHING;

-- =====================================================================
-- 7. SUPABASE EDGE FUNCTIONS (reference — implement separately)
-- =====================================================================
-- /functions/v1/create-daily-room
--   POST {room_name, exp_seconds?, privacy?, properties?}
--   → reads DAILY_API_KEY from Vault, calls Daily.co REST API
--   → returns {id, room_name, token, room_url, vendor, created_at, expires_at}
--
-- /functions/v1/create-agora-token
--   POST {channel, uid?, exp_seconds?}
--   → reads AGORA_APP_ID + AGORA_APP_CERTIFICATE from Vault
--   → generates RTC token server-side
--   → returns {id, room_name, token, vendor, created_at, expires_at}
--
-- /functions/v1/create-livekit-token
--   POST {room_name, participant_identity, exp_seconds?}
--   → reads LIVEKIT_API_KEY + LIVEKIT_API_SECRET from Vault
--   → returns {id, room_name, token, room_url, vendor, created_at, expires_at}

-- =====================================================================
-- 8. USAGE LOGS
-- Fire-and-forget token/event logging written by usage_service.log_usage().
-- Intentionally in the public schema (no .schema("app") in the client call).
-- event_type format: ai_tokens:{provider}:{mode}
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.usage_logs (
  id          bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id     uuid    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  event_type  text    NOT NULL,
  value       integer NOT NULL DEFAULT 0,
  created_at  timestamp with time zone NOT NULL DEFAULT now()
);

ALTER TABLE public.usage_logs ENABLE ROW LEVEL SECURITY;

-- Users can only read their own logs; all writes go through service_role.
CREATE POLICY "usage_logs_user_select"
  ON public.usage_logs FOR SELECT
  TO authenticated
  USING (user_id = auth.uid());

CREATE POLICY "usage_logs_service_insert"
  ON public.usage_logs FOR INSERT
  TO service_role
  WITH CHECK (true);

-- Fast look-ups by user (dashboard / rate-limit queries)
CREATE INDEX IF NOT EXISTS idx_usage_logs_user_id
  ON public.usage_logs(user_id);

-- Range scans for daily / monthly aggregates
CREATE INDEX IF NOT EXISTS idx_usage_logs_user_created
  ON public.usage_logs(user_id, created_at DESC);

-- Filter by event type (e.g. all openai rows)
CREATE INDEX IF NOT EXISTS idx_usage_logs_event_type
  ON public.usage_logs(event_type);

-- =====================================================================
-- 9. AI CHAT TABLES
-- Supports user-to-AI conversations (standalone or linked from classic chat).
-- Offline-first: local cache at conversations/ai_index_<userId>.json
-- =====================================================================

-- ── AI Conversations ─────────────────────────────────────────────────
-- Each row is one user-to-AI session. classic_conversation_id links it
-- to the user-to-user chat that originated the AI calls (nullable).

CREATE TABLE IF NOT EXISTS app.ai_conversation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title text NOT NULL,
  classic_conversation_id uuid REFERENCES app.conversation(id) ON DELETE SET NULL,
  created_at timestamp with time zone DEFAULT now(),
  last_message_at timestamp with time zone,
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.ai_conversation ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own AI conversations"
  ON app.ai_conversation FOR ALL
  USING (user_id = auth.uid() AND deleted_at IS NULL);

CREATE INDEX IF NOT EXISTS idx_ai_conversation_user_id
  ON app.ai_conversation(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_conversation_classic_id
  ON app.ai_conversation(classic_conversation_id)
  WHERE classic_conversation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_conversation_deleted_at
  ON app.ai_conversation(deleted_at) WHERE deleted_at IS NULL;

-- ── AI Messages ──────────────────────────────────────────────────────
-- Individual turns in an AI conversation.
-- role: 'user' | 'assistant'
-- shorthand_used: the @command that triggered this exchange (nullable)
-- raw_input: the full text the user typed including the shorthand (nullable)

CREATE TABLE IF NOT EXISTS app.ai_message (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES app.ai_conversation(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL,
  shorthand_used text,
  raw_input text,
  created_at timestamp with time zone DEFAULT now()
);

ALTER TABLE app.ai_message ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage messages in their own AI conversations"
  ON app.ai_message FOR ALL
  USING (
    conversation_id IN (
      SELECT id FROM app.ai_conversation WHERE user_id = auth.uid() AND deleted_at IS NULL
    )
  );

CREATE INDEX IF NOT EXISTS idx_ai_message_conversation_id
  ON app.ai_message(conversation_id);
CREATE INDEX IF NOT EXISTS idx_ai_message_created_at
  ON app.ai_message(conversation_id, created_at);
