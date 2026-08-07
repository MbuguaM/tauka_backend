-- =====================================================================
-- TAUKA — ADDITIONAL SCHEMA CHANGES
-- =====================================================================
-- Audit date : 2026-05-09
-- Covers     : subscription tiers, video session ratings & notes,
--              AI practice module, broadcast system, language exchange
--              matching, cohort health, tutor payroll, feature flags,
--              notification table expansion.
--
-- DEPENDENCY ORDER
-- ─────────────────────────────────────────────────────────────────────
-- Apply consolidated_full_db_remote_schema.sql Parts A + B FIRST.
-- Specifically:
--   §B1  — adds user_profiles.role + suspension columns
--   §B7  — creates app.video_session + app.video_session_participant
--           (required before §1 and §2 of this file)
--   §B9  — creates app.video_session (if §B7 uses that name)
--
-- All statements use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so this
-- file is safe to re-run.
-- =====================================================================

-- =============================================================================
-- §0  HELPER FUNCTIONS
-- =============================================================================

-- ─── app.is_tutor() ──────────────────────────────────────────────────────────
-- Returns true when the calling user exists in app.tutor.
-- Use alongside app.is_admin() in all mixed-role policies.
CREATE OR REPLACE FUNCTION app.is_tutor()
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = app
AS $$
  SELECT EXISTS (
    SELECT 1 FROM app.tutor
    WHERE id = auth.uid()
  );
$$;

-- =============================================================================
-- §1  SUBSCRIPTION TIER ON USER_PROFILES
-- =============================================================================
-- Adds tier tracking alongside the role column (role added by §B1).
-- Tiers are student-only; tutor and admin rows leave this NULL.
-- Only service role (Stripe webhook Edge Function) or admin can UPDATE.

ALTER TABLE app.user_profiles
  ADD COLUMN IF NOT EXISTS subscription_tier TEXT DEFAULT 'free'
    CHECK (subscription_tier IN ('free', 'learner', 'tutor_tier', 'intensive')
           OR subscription_tier IS NULL),
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
  ADD COLUMN IF NOT EXISTS tier_updated_at    TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_user_profiles_subscription_tier
  ON app.user_profiles (subscription_tier)
  WHERE subscription_tier IS NOT NULL;

-- Policy: users read own tier; admin reads all. UPDATE via service role only.
DROP POLICY IF EXISTS "user_profiles_select_own_or_admin"  ON app.user_profiles;
DROP POLICY IF EXISTS "user_profiles_update_own"           ON app.user_profiles;
DROP POLICY IF EXISTS "user_profiles_update_admin"         ON app.user_profiles;

-- NOTE: These policies intentionally exclude subscription_tier from
-- user self-update. Tier changes must go through the service role
-- (Stripe webhook Edge Function). Use a separate service-role function
-- to update subscription_tier — do NOT grant UPDATE on this column
-- to the 'authenticated' role.

-- =============================================================================
-- §2  NOTIFICATION TABLE EXPANSION
-- =============================================================================
-- Extends the existing app.notifications table with type, action URL,
-- and priority to support the 20-type notification registry (specs.md §11.1).
-- Existing rows retain NULL values for new columns — safe.

ALTER TABLE app.notifications
  ADD COLUMN IF NOT EXISTS notification_type TEXT,
  ADD COLUMN IF NOT EXISTS action_url        TEXT,
  ADD COLUMN IF NOT EXISTS priority          TEXT DEFAULT 'normal'
    CHECK (priority IN ('low', 'normal', 'high', 'critical'));

-- Supported notification_type values (enforced at app layer, not CHECK
-- constraint, to allow future types without a migration):
-- session_reminder_24h | session_reminder_2h | session_reminder_10min
-- session_starting | warmup_incomplete | new_message | raised_hand
-- tutor_response | ai_correction | new_explore_content | flashcards_due
-- streak_at_risk | cohort_change | payment_failure | tier_upgrade
-- achievement_unlocked | tutor_sla_warning | admin_alert

CREATE INDEX IF NOT EXISTS idx_notifications_type
  ON app.notifications (notification_type)
  WHERE deleted_at IS NULL;

-- =============================================================================
-- §3  VIDEO SESSION RATINGS
-- =============================================================================
-- Captures the post-session 1-5 star + optional text rating shown to the
-- student after every video session ends (specs.md §5.12 State 5).
-- Depends on: consolidated_full_db_remote_schema.sql §B7 (video_session table).

CREATE TABLE IF NOT EXISTS app.video_session_rating (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id   UUID NOT NULL,   -- REFERENCES app.video_session(id) — add FK after §B7 applied
  student_id   UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  stars        SMALLINT NOT NULL CHECK (stars BETWEEN 1 AND 5),
  feedback     TEXT,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (session_id, student_id)
);

-- Add FK to video_session when §B7 is confirmed applied:
-- ALTER TABLE app.video_session_rating
--   ADD CONSTRAINT fk_vsr_session
--   FOREIGN KEY (session_id) REFERENCES app.video_session(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_vsr_session   ON app.video_session_rating (session_id);
CREATE INDEX IF NOT EXISTS idx_vsr_student   ON app.video_session_rating (student_id);

ALTER TABLE app.video_session_rating ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "vsr_select_participant_or_tutor_or_admin" ON app.video_session_rating;
DROP POLICY IF EXISTS "vsr_insert_own"                           ON app.video_session_rating;

CREATE POLICY "vsr_select_participant_or_tutor_or_admin"
  ON app.video_session_rating FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "vsr_insert_own"
  ON app.video_session_rating FOR INSERT TO authenticated
  WITH CHECK (student_id = auth.uid());

-- =============================================================================
-- §4  VIDEO SESSION NOTES
-- =============================================================================
-- Per-student notes written by the tutor at the end of each session.
-- Students can read notes written about them (specs.md §5.12 §6.9).

CREATE TABLE IF NOT EXISTS app.video_session_note (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        UUID NOT NULL,   -- REFERENCES app.video_session(id) — add FK after §B7
  student_id        UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  tutor_id          UUID REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  body              TEXT NOT NULL DEFAULT '',
  lesson_completed  BOOLEAN DEFAULT FALSE,
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (session_id, student_id)
);

-- Add FK when §B7 is confirmed applied:
-- ALTER TABLE app.video_session_note
--   ADD CONSTRAINT fk_vsn_session
--   FOREIGN KEY (session_id) REFERENCES app.video_session(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_vsn_session ON app.video_session_note (session_id);
CREATE INDEX IF NOT EXISTS idx_vsn_student ON app.video_session_note (student_id);
CREATE INDEX IF NOT EXISTS idx_vsn_tutor   ON app.video_session_note (tutor_id);

ALTER TABLE app.video_session_note ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "vsn_select_student_or_tutor_or_admin" ON app.video_session_note;
DROP POLICY IF EXISTS "vsn_insert_tutor_or_admin"            ON app.video_session_note;
DROP POLICY IF EXISTS "vsn_update_tutor_or_admin"            ON app.video_session_note;

CREATE POLICY "vsn_select_student_or_tutor_or_admin"
  ON app.video_session_note FOR SELECT TO authenticated
  USING (
    student_id = auth.uid()
    OR tutor_id = auth.uid()
    OR app.is_admin()
  );

CREATE POLICY "vsn_insert_tutor_or_admin"
  ON app.video_session_note FOR INSERT TO authenticated
  WITH CHECK (tutor_id = auth.uid() OR app.is_admin());

CREATE POLICY "vsn_update_tutor_or_admin"
  ON app.video_session_note FOR UPDATE TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin())
  WITH CHECK (tutor_id = auth.uid() OR app.is_admin());

-- =============================================================================
-- §5  AI PRACTICE SESSION
-- =============================================================================
-- Records each AI practice session (conversation / drill / pronunciation).
-- Distinct from app.ai_conversation — this tracks structured learning sessions
-- with measurable outcomes (specs.md §5.13).

CREATE TABLE IF NOT EXISTS app.ai_practice_session (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  mode                TEXT NOT NULL CHECK (mode IN ('conversation', 'drill', 'pronunciation')),
  lesson_number       INTEGER,
  duration_seconds    INTEGER,
  turns_count         INTEGER DEFAULT 0,
  error_count         INTEGER DEFAULT 0,
  vocabulary_added    INTEGER DEFAULT 0,
  summary_text        TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  completed_at        TIMESTAMPTZ,
  deleted_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ai_ps_user        ON app.ai_practice_session (user_id);
CREATE INDEX IF NOT EXISTS idx_ai_ps_mode        ON app.ai_practice_session (mode);
CREATE INDEX IF NOT EXISTS idx_ai_ps_created     ON app.ai_practice_session (created_at DESC)
  WHERE deleted_at IS NULL;

ALTER TABLE app.ai_practice_session ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "ai_ps_select_own_or_tutor_or_admin" ON app.ai_practice_session;
DROP POLICY IF EXISTS "ai_ps_insert_own"                   ON app.ai_practice_session;
DROP POLICY IF EXISTS "ai_ps_update_own"                   ON app.ai_practice_session;

CREATE POLICY "ai_ps_select_own_or_tutor_or_admin"
  ON app.ai_practice_session FOR SELECT TO authenticated
  USING (
    user_id = auth.uid()
    OR app.is_tutor()   -- tutors see assigned students' sessions via app-layer filter
    OR app.is_admin()
  );

CREATE POLICY "ai_ps_insert_own"
  ON app.ai_practice_session FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "ai_ps_update_own"
  ON app.ai_practice_session FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================================
-- §6  AI PRACTICE TURN
-- =============================================================================
-- Each exchange (user turn + assistant reply) within an ai_practice_session.

CREATE TABLE IF NOT EXISTS app.ai_practice_turn (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      UUID REFERENCES app.ai_practice_session(id) ON DELETE CASCADE NOT NULL,
  turn_index      INTEGER NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content         TEXT NOT NULL,
  audio_url       TEXT,           -- for pronunciation mode: user recording or AI playback
  grammar_error   BOOLEAN DEFAULT FALSE,
  correction_text TEXT,
  tokens_used     INTEGER,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_pt_session ON app.ai_practice_turn (session_id);

ALTER TABLE app.ai_practice_turn ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "ai_pt_select_via_session_owner" ON app.ai_practice_turn;
DROP POLICY IF EXISTS "ai_pt_insert_via_session_owner" ON app.ai_practice_turn;

-- Inherit access from ai_practice_session — user can only access turns
-- belonging to their own sessions.
CREATE POLICY "ai_pt_select_via_session_owner"
  ON app.ai_practice_turn FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM app.ai_practice_session s
      WHERE s.id = session_id
        AND (s.user_id = auth.uid() OR app.is_tutor() OR app.is_admin())
    )
  );

CREATE POLICY "ai_pt_insert_via_session_owner"
  ON app.ai_practice_turn FOR INSERT TO authenticated
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM app.ai_practice_session s
      WHERE s.id = session_id AND s.user_id = auth.uid()
    )
  );

-- =============================================================================
-- §7  BROADCAST SYSTEM
-- =============================================================================
-- One-to-many posts from tutors/admins to student audiences.
-- Distinct from app.notifications (passive) and app.messages (peer-to-peer).
-- See specs.md §5.14 for full feature spec.

CREATE TABLE IF NOT EXISTS app.broadcast (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  author_id     UUID REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  content       TEXT NOT NULL,
  attach_url    TEXT,               -- image, audio, or Explore content URL
  attach_type   TEXT CHECK (attach_type IN ('image', 'audio', 'explore')),
  target_type   TEXT NOT NULL DEFAULT 'program'
    CHECK (target_type IN ('program', 'level', 'cohort')),
  target_id     UUID,               -- class_id if target_type = 'cohort'
  target_level  TEXT,               -- e.g. 'A1' if target_type = 'level'
  is_pinned     BOOLEAN DEFAULT FALSE,
  scheduled_at  TIMESTAMPTZ,        -- NULL = publish immediately
  published_at  TIMESTAMPTZ DEFAULT NOW(),
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  deleted_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_broadcast_published
  ON app.broadcast (published_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_broadcast_target
  ON app.broadcast (target_type, target_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_broadcast_pinned
  ON app.broadcast (is_pinned) WHERE deleted_at IS NULL AND is_pinned = TRUE;
CREATE INDEX IF NOT EXISTS idx_broadcast_author
  ON app.broadcast (author_id);

ALTER TABLE app.broadcast ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "broadcast_select_authenticated" ON app.broadcast;
DROP POLICY IF EXISTS "broadcast_insert_tutor_or_admin" ON app.broadcast;
DROP POLICY IF EXISTS "broadcast_update_admin"          ON app.broadcast;
DROP POLICY IF EXISTS "broadcast_delete_admin"          ON app.broadcast;

-- All authenticated users can read published, non-deleted broadcasts.
-- Audience targeting (level, cohort) is enforced at the application layer;
-- RLS only enforces authentication and soft-delete.
CREATE POLICY "broadcast_select_authenticated"
  ON app.broadcast FOR SELECT TO authenticated
  USING (
    deleted_at IS NULL
    AND (scheduled_at IS NULL OR scheduled_at <= NOW())
  );

CREATE POLICY "broadcast_insert_tutor_or_admin"
  ON app.broadcast FOR INSERT TO authenticated
  WITH CHECK (
    author_id = auth.uid()
    AND (app.is_tutor() OR app.is_admin())
  );

CREATE POLICY "broadcast_update_admin"
  ON app.broadcast FOR UPDATE TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- Soft delete only — actual row deletion restricted to service role.
CREATE POLICY "broadcast_delete_admin"
  ON app.broadcast FOR DELETE TO authenticated
  USING (app.is_admin());

-- =============================================================================
-- §8  BROADCAST REACTIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.broadcast_reaction (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  broadcast_id UUID REFERENCES app.broadcast(id) ON DELETE CASCADE NOT NULL,
  user_id      UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  emoji        TEXT NOT NULL,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (broadcast_id, user_id, emoji)
);

CREATE INDEX IF NOT EXISTS idx_br_reaction_broadcast ON app.broadcast_reaction (broadcast_id);
CREATE INDEX IF NOT EXISTS idx_br_reaction_user      ON app.broadcast_reaction (user_id);

ALTER TABLE app.broadcast_reaction ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "br_reaction_select_authenticated" ON app.broadcast_reaction;
DROP POLICY IF EXISTS "br_reaction_insert_own"           ON app.broadcast_reaction;
DROP POLICY IF EXISTS "br_reaction_delete_own"           ON app.broadcast_reaction;

CREATE POLICY "br_reaction_select_authenticated"
  ON app.broadcast_reaction FOR SELECT TO authenticated
  USING (TRUE);

CREATE POLICY "br_reaction_insert_own"
  ON app.broadcast_reaction FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "br_reaction_delete_own"
  ON app.broadcast_reaction FOR DELETE TO authenticated
  USING (user_id = auth.uid() OR app.is_admin());

-- =============================================================================
-- §9  BROADCAST COMMENTS
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.broadcast_comment (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  broadcast_id UUID REFERENCES app.broadcast(id) ON DELETE CASCADE NOT NULL,
  author_id    UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  content      TEXT NOT NULL,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  deleted_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_br_comment_broadcast ON app.broadcast_comment (broadcast_id)
  WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_br_comment_author    ON app.broadcast_comment (author_id);

ALTER TABLE app.broadcast_comment ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "br_comment_select_authenticated" ON app.broadcast_comment;
DROP POLICY IF EXISTS "br_comment_insert_own"           ON app.broadcast_comment;
DROP POLICY IF EXISTS "br_comment_update_own"           ON app.broadcast_comment;

CREATE POLICY "br_comment_select_authenticated"
  ON app.broadcast_comment FOR SELECT TO authenticated
  USING (deleted_at IS NULL);

CREATE POLICY "br_comment_insert_own"
  ON app.broadcast_comment FOR INSERT TO authenticated
  WITH CHECK (author_id = auth.uid());

-- Soft delete own comments; admins can soft-delete any.
CREATE POLICY "br_comment_update_own"
  ON app.broadcast_comment FOR UPDATE TO authenticated
  USING (author_id = auth.uid() OR app.is_admin())
  WITH CHECK (author_id = auth.uid() OR app.is_admin());

-- =============================================================================
-- §10  LANGUAGE EXCHANGE MATCHING
-- =============================================================================
-- Structured match records for the language exchange system (specs.md §5.15).
-- The existing app.langexchange table uses loose text fields and is separate.
-- This table uses UUID FKs and tracks the full match lifecycle including
-- the private conversation channel and re-match eligibility.

CREATE TABLE IF NOT EXISTS app.language_exchange_match (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_a_id            UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  user_b_id            UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  conversation_id      UUID REFERENCES app.conversations(id) ON DELETE SET NULL,
  matched_at           TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  -- Re-match available 14 days after matched_at; computed on read in app layer.
  -- Cannot use GENERATED ALWAYS AS with non-immutable NOW() reference.
  rematch_available_at TIMESTAMPTZ GENERATED ALWAYS AS (matched_at + INTERVAL '14 days') STORED,
  status               TEXT DEFAULT 'active'
    CHECK (status IN ('active', 'ended', 'rematch_requested')),
  ended_at             TIMESTAMPTZ,
  ended_by             UUID REFERENCES app.user_profiles(id),
  UNIQUE (user_a_id, user_b_id)
);

CREATE INDEX IF NOT EXISTS idx_lex_match_user_a  ON app.language_exchange_match (user_a_id);
CREATE INDEX IF NOT EXISTS idx_lex_match_user_b  ON app.language_exchange_match (user_b_id);
CREATE INDEX IF NOT EXISTS idx_lex_match_status  ON app.language_exchange_match (status)
  WHERE status = 'active';

ALTER TABLE app.language_exchange_match ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "lex_match_select_participant_or_admin" ON app.language_exchange_match;
DROP POLICY IF EXISTS "lex_match_insert_service_role"         ON app.language_exchange_match;
DROP POLICY IF EXISTS "lex_match_update_participant_or_admin" ON app.language_exchange_match;

-- Participants can read their own matches.
CREATE POLICY "lex_match_select_participant_or_admin"
  ON app.language_exchange_match FOR SELECT TO authenticated
  USING (
    user_a_id = auth.uid()
    OR user_b_id = auth.uid()
    OR app.is_admin()
  );

-- Matching is performed server-side (Edge Function with service role).
-- Authenticated users cannot directly INSERT their own matches to prevent
-- self-matching abuse. Matching inserts go via service role.
-- The policy below restricts direct INSERT to admins only.
CREATE POLICY "lex_match_insert_admin"
  ON app.language_exchange_match FOR INSERT TO authenticated
  WITH CHECK (app.is_admin());

-- Either participant can update status (request rematch / end exchange).
CREATE POLICY "lex_match_update_participant_or_admin"
  ON app.language_exchange_match FOR UPDATE TO authenticated
  USING (
    user_a_id = auth.uid()
    OR user_b_id = auth.uid()
    OR app.is_admin()
  )
  WITH CHECK (
    user_a_id = auth.uid()
    OR user_b_id = auth.uid()
    OR app.is_admin()
  );

-- =============================================================================
-- §11  COHORT HEALTH COLUMNS ON app.classes
-- =============================================================================
-- Adds health score and lesson tracking to the existing classes table.
-- Health score is computed by a scheduled Edge Function and written via
-- service role. Students/tutors can read; only admin or service role writes.

ALTER TABLE app.classes
  ADD COLUMN IF NOT EXISTS health_score   SMALLINT DEFAULT NULL
    CHECK (health_score BETWEEN 0 AND 100),
  ADD COLUMN IF NOT EXISTS health_status  TEXT DEFAULT NULL
    CHECK (health_status IN ('healthy', 'attention', 'intervention')),
  ADD COLUMN IF NOT EXISTS current_lesson INTEGER DEFAULT 1,
  ADD COLUMN IF NOT EXISTS level          TEXT;

CREATE INDEX IF NOT EXISTS idx_classes_health_status ON app.classes (health_status)
  WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_classes_level          ON app.classes (level)
  WHERE deleted_at IS NULL;

-- No new RLS policies required — existing app.classes policies cover reads.
-- Health score writes use service role (Edge Function cron job).

-- =============================================================================
-- §12  TUTOR PAYROLL
-- =============================================================================
-- Tracks payroll periods per tutor with per-line-item breakdown.
-- Admins generate payroll; tutors can view their own records.

CREATE TABLE IF NOT EXISTS app.tutor_payroll (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id            UUID REFERENCES app.user_profiles(id) ON DELETE RESTRICT NOT NULL,
  period_start        DATE NOT NULL,
  period_end          DATE NOT NULL,
  group_sessions      INTEGER DEFAULT 0,
  solo_sessions       INTEGER DEFAULT 0,
  explore_contributions INTEGER DEFAULT 0,
  base_amount         NUMERIC(10, 2) NOT NULL DEFAULT 0,
  performance_bonus   NUMERIC(10, 2) DEFAULT 0,
  total_amount        NUMERIC(10, 2)
    GENERATED ALWAYS AS (base_amount + COALESCE(performance_bonus, 0)) STORED,
  status              TEXT DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'completed', 'confirmed')),
  stripe_transfer_id  TEXT,
  payment_method      TEXT DEFAULT 'stripe'
    CHECK (payment_method IN ('stripe', 'mpesa', 'manual')),
  paid_at             TIMESTAMPTZ,
  approved_by         UUID REFERENCES app.user_profiles(id),
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tutor_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_tutor_payroll_tutor  ON app.tutor_payroll (tutor_id);
CREATE INDEX IF NOT EXISTS idx_tutor_payroll_status ON app.tutor_payroll (status)
  WHERE status != 'confirmed';

ALTER TABLE app.tutor_payroll ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "payroll_select_tutor_or_admin" ON app.tutor_payroll;
DROP POLICY IF EXISTS "payroll_insert_admin"           ON app.tutor_payroll;
DROP POLICY IF EXISTS "payroll_update_admin"           ON app.tutor_payroll;

CREATE POLICY "payroll_select_tutor_or_admin"
  ON app.tutor_payroll FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());

CREATE POLICY "payroll_insert_admin"
  ON app.tutor_payroll FOR INSERT TO authenticated
  WITH CHECK (app.is_admin());

CREATE POLICY "payroll_update_admin"
  ON app.tutor_payroll FOR UPDATE TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §13  PAYROLL LINE ITEMS
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.payroll_line_item (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  payroll_id   UUID REFERENCES app.tutor_payroll(id) ON DELETE CASCADE NOT NULL,
  item_type    TEXT NOT NULL
    CHECK (item_type IN ('group_session', 'solo_session', 'explore_contribution', 'performance_bonus')),
  session_id   UUID,               -- FK to app.video_session — add after §B7 applied:
                                   -- REFERENCES app.video_session(id)
  reference_id UUID,               -- generic FK: yt_playlist.id for explore contributions
  description  TEXT,
  amount       NUMERIC(10, 2) NOT NULL,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payroll_li_payroll ON app.payroll_line_item (payroll_id);

ALTER TABLE app.payroll_line_item ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "payroll_li_select_tutor_or_admin" ON app.payroll_line_item;
DROP POLICY IF EXISTS "payroll_li_insert_admin"          ON app.payroll_line_item;

-- Join through payroll to enforce tutor ownership.
CREATE POLICY "payroll_li_select_tutor_or_admin"
  ON app.payroll_line_item FOR SELECT TO authenticated
  USING (
    app.is_admin()
    OR EXISTS (
      SELECT 1 FROM app.tutor_payroll p
      WHERE p.id = payroll_id AND p.tutor_id = auth.uid()
    )
  );

CREATE POLICY "payroll_li_insert_admin"
  ON app.payroll_line_item FOR INSERT TO authenticated
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §14  FEATURE FLAGS
-- =============================================================================
-- Controls which product features are 'ready' vs 'coming' (specs.md Appendix A).
-- Clients read flags on startup to gate UI. Only admins can modify flags.

CREATE TABLE IF NOT EXISTS app.feature_flag (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key               TEXT NOT NULL UNIQUE,
  status            TEXT NOT NULL DEFAULT 'coming'
    CHECK (status IN ('ready', 'coming')),
  description       TEXT,
  notify_me_count   INTEGER DEFAULT 0,
  updated_at        TIMESTAMPTZ DEFAULT NOW(),
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feature_flag_key    ON app.feature_flag (key);
CREATE INDEX IF NOT EXISTS idx_feature_flag_status ON app.feature_flag (status);

ALTER TABLE app.feature_flag ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "feature_flag_select_authenticated" ON app.feature_flag;
DROP POLICY IF EXISTS "feature_flag_insert_admin"         ON app.feature_flag;
DROP POLICY IF EXISTS "feature_flag_update_admin"         ON app.feature_flag;

-- All authenticated users can read flags (needed for client-side gating).
CREATE POLICY "feature_flag_select_authenticated"
  ON app.feature_flag FOR SELECT TO authenticated
  USING (TRUE);

CREATE POLICY "feature_flag_insert_admin"
  ON app.feature_flag FOR INSERT TO authenticated
  WITH CHECK (app.is_admin());

CREATE POLICY "feature_flag_update_admin"
  ON app.feature_flag FOR UPDATE TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §15  SEED — FEATURE FLAGS
-- =============================================================================
-- Initial flag set. All features from Additional_specs.md start as 'coming'.
-- Update status to 'ready' when a feature is shipped.

INSERT INTO app.feature_flag (key, status, description)
VALUES
  ('video_sessions',         'coming', 'Live video sessions with tutors (WebRTC via Daily.co)'),
  ('subscription_tiers',     'coming', 'Paid subscription tiers: Learner, Tutor, Intensive'),
  ('ai_practice',            'coming', 'AI practice module: conversation, drill, pronunciation modes'),
  ('ai_pronunciation',       'coming', 'AI pronunciation coaching (Intensive tier only — requires Whisper API)'),
  ('broadcasts',             'coming', 'Tutor/admin broadcast posts in Chat tab'),
  ('language_exchange',      'coming', 'Language exchange peer matching system'),
  ('cohort_management',      'coming', 'Admin cohort health scores and merge/reassign tools'),
  ('payroll',                'coming', 'Tutor payroll calculation and Stripe Connect payout'),
  ('handwriting_desktop',    'coming', 'Desktop handwriting recognition (Windows Ink / PencilKit)'),
  ('handwriting_amharic',    'coming', 'Amharic handwriting recognition (requires custom TFLite model)'),
  ('tiktok_oauth',           'coming', 'TikTok sign-in / content tab'),
  ('explore_annotations',    'coming', 'Word-level annotation viewer with audio sync'),
  ('explore_share_cards',    'coming', 'Branded share card generation from Explore content'),
  ('device_pairing',         'coming', 'QR code device-connect and one-device-per-type limit'),
  ('push_notifications',     'coming', 'FCM push notifications for messages, sessions, and alerts'),
  ('offline_sync_conflict',  'coming', 'Device sync conflict detection and resolution prompt'),
  ('srs_wired',              'coming', 'Spaced repetition scheduling wired into Anki card submit'),
  ('exercise_results_sync',  'coming', 'Exercise results synced to Supabase after each session')
ON CONFLICT (key) DO NOTHING;

-- =============================================================================
-- §16  USER PROFILE — STREAK, XP, DEVICE & SUBSCRIPTION BACKFILL
-- =============================================================================
-- Covers implementation.md A1.1 and A1.11.
-- subscription_tier and stripe_customer_id already added in §1.
-- These are the remaining columns required by B4 (streak/XP) and K2 (devices).

ALTER TABLE app.user_profiles
  ADD COLUMN IF NOT EXISTS streak_days             INT         DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_active_date        DATE,
  ADD COLUMN IF NOT EXISTS xp_total               INT         DEFAULT 0,
  ADD COLUMN IF NOT EXISTS xp_week                INT         DEFAULT 0,
  ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id  TEXT,
  ADD COLUMN IF NOT EXISTS device_limit            INT         DEFAULT 2,
  ADD COLUMN IF NOT EXISTS devices                 JSONB       DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS proficiency_level       TEXT
    CHECK (proficiency_level IN ('beginner', 'intermediate', 'advanced') OR proficiency_level IS NULL);

CREATE INDEX IF NOT EXISTS idx_user_profiles_streak
  ON app.user_profiles (streak_days DESC) WHERE streak_days > 0;
CREATE INDEX IF NOT EXISTS idx_user_profiles_xp_week
  ON app.user_profiles (xp_week DESC) WHERE xp_week > 0;

-- =============================================================================
-- §17  STREAK & XP POSTGRES HELPER FUNCTIONS
-- =============================================================================
-- Called by SupabaseService.updateStreak() and addXp() (impl B4.1).
-- SECURITY DEFINER so the function can update any user row when invoked
-- by the authenticated user for their own id only.

CREATE OR REPLACE FUNCTION app.update_user_streak(p_user_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app
AS $$
DECLARE
  v_last_active DATE;
  v_today       DATE := CURRENT_DATE;
BEGIN
  SELECT last_active_date INTO v_last_active
    FROM app.user_profiles WHERE id = p_user_id;

  IF v_last_active IS NULL OR v_last_active < v_today THEN
    UPDATE app.user_profiles SET
      streak_days       = CASE
                            WHEN v_last_active = v_today - 1 THEN streak_days + 1
                            ELSE 1
                          END,
      last_active_date  = v_today
    WHERE id = p_user_id;
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION app.add_user_xp(p_user_id UUID, p_amount INT)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = app
AS $$
  UPDATE app.user_profiles
    SET xp_total = xp_total + p_amount,
        xp_week  = xp_week  + p_amount
  WHERE id = p_user_id;
$$;

-- Weekly XP reset: schedule via pg_cron (see §34).

-- =============================================================================
-- §18  EXERCISE SESSIONS
-- =============================================================================
-- Records each student exercise session for progress analytics (impl A1.2, B2).

CREATE TABLE IF NOT EXISTS app.exercise_sessions (
  id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID        REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  lesson_id    UUID,
  started_at   TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  completed_at TIMESTAMPTZ,
  score        INT,
  max_score    INT,
  answers      JSONB,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exercise_sessions_user
  ON app.exercise_sessions (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_exercise_sessions_lesson
  ON app.exercise_sessions (lesson_id) WHERE lesson_id IS NOT NULL;

ALTER TABLE app.exercise_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "exercise_sessions_select_own_or_tutor_admin" ON app.exercise_sessions;
DROP POLICY IF EXISTS "exercise_sessions_insert_own"               ON app.exercise_sessions;
DROP POLICY IF EXISTS "exercise_sessions_update_own"               ON app.exercise_sessions;

CREATE POLICY "exercise_sessions_select_own_or_tutor_admin"
  ON app.exercise_sessions FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "exercise_sessions_insert_own"
  ON app.exercise_sessions FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "exercise_sessions_update_own"
  ON app.exercise_sessions FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================================
-- §19  FLASHCARD REVIEWS (SM-2 SPACED REPETITION)
-- =============================================================================
-- Persists SM-2 scheduling data per card per user (impl A1.3, B1).

CREATE TABLE IF NOT EXISTS app.flashcard_reviews (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID        REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  card_id       UUID        NOT NULL,
  deck_id       UUID,
  reviewed_at   TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  grade         INT         NOT NULL CHECK (grade BETWEEN 0 AND 5),
  interval_days INT         NOT NULL DEFAULT 1,
  ease_factor   NUMERIC(4,2) NOT NULL DEFAULT 2.50,
  due_date      DATE        NOT NULL,
  UNIQUE (user_id, card_id)
);

CREATE INDEX IF NOT EXISTS idx_flashcard_reviews_due
  ON app.flashcard_reviews (user_id, due_date)
  WHERE due_date <= CURRENT_DATE;
CREATE INDEX IF NOT EXISTS idx_flashcard_reviews_deck
  ON app.flashcard_reviews (user_id, deck_id) WHERE deck_id IS NOT NULL;

ALTER TABLE app.flashcard_reviews ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "flashcard_reviews_own" ON app.flashcard_reviews;

CREATE POLICY "flashcard_reviews_own"
  ON app.flashcard_reviews FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================================
-- §20  VOICE RECORDINGS
-- =============================================================================
-- Stores metadata for pronunciation recordings uploaded to Supabase Storage
-- (impl A1.4, B3). Actual audio files go in the 'voice-recordings' bucket.

CREATE TABLE IF NOT EXISTS app.voice_recordings (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID        REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  lesson_id     UUID,
  storage_path  TEXT        NOT NULL,
  duration_ms   INT,
  phoneme_score NUMERIC(4,2) CHECK (phoneme_score BETWEEN 0 AND 100),
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_recordings_user
  ON app.voice_recordings (user_id, created_at DESC);

ALTER TABLE app.voice_recordings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "voice_recordings_select_own_or_tutor_admin" ON app.voice_recordings;
DROP POLICY IF EXISTS "voice_recordings_insert_own"                ON app.voice_recordings;

CREATE POLICY "voice_recordings_select_own_or_tutor_admin"
  ON app.voice_recordings FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "voice_recordings_insert_own"
  ON app.voice_recordings FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

-- Storage bucket policy reminder (apply in Supabase dashboard or via CLI):
-- bucket: voice-recordings
-- INSERT policy: (storage.foldername(name))[1] = auth.uid()::text
-- SELECT policy: (storage.foldername(name))[1] = auth.uid()::text OR app.is_tutor() OR app.is_admin()

-- =============================================================================
-- §21  BROADCAST READS
-- =============================================================================
-- Tracks which broadcasts each user has read for unread-count badge (impl A1.5).
-- Distinct from broadcast_reaction (§8) — read != liked.

CREATE TABLE IF NOT EXISTS app.broadcast_reads (
  user_id      UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  broadcast_id UUID REFERENCES app.broadcast(id)      ON DELETE CASCADE NOT NULL,
  read_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, broadcast_id)
);

CREATE INDEX IF NOT EXISTS idx_broadcast_reads_user
  ON app.broadcast_reads (user_id);

ALTER TABLE app.broadcast_reads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "broadcast_reads_own" ON app.broadcast_reads;

CREATE POLICY "broadcast_reads_own"
  ON app.broadcast_reads FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================================
-- §22  PUSH NOTIFICATION TOKENS
-- =============================================================================
-- Stores FCM device tokens for server-side push delivery (impl A1.6, I1-I2).

CREATE TABLE IF NOT EXISTS app.push_tokens (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  token      TEXT NOT NULL,
  platform   TEXT CHECK (platform IN ('android', 'ios', 'web')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, token)
);

CREATE INDEX IF NOT EXISTS idx_push_tokens_user
  ON app.push_tokens (user_id);

ALTER TABLE app.push_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "push_tokens_own"        ON app.push_tokens;
DROP POLICY IF EXISTS "push_tokens_read_admin" ON app.push_tokens;

CREATE POLICY "push_tokens_own"
  ON app.push_tokens FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Edge Functions (send-push) read tokens via service role — no extra policy needed.

-- =============================================================================
-- §23  CONTENT VERSIONS
-- =============================================================================
-- Enables the client-side content version checker (impl A1.7, B6).
-- Written by admin/service role when content is published; read by all auth users.

CREATE TABLE IF NOT EXISTS app.content_versions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type    TEXT NOT NULL CHECK (entity_type IN ('course', 'unit', 'lesson')),
  entity_id      UUID NOT NULL,
  version        INT  NOT NULL DEFAULT 1,
  published_at   TIMESTAMPTZ DEFAULT NOW(),
  change_summary TEXT,
  UNIQUE (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_content_versions_entity
  ON app.content_versions (entity_type, entity_id);

ALTER TABLE app.content_versions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "content_versions_select_authenticated" ON app.content_versions;
DROP POLICY IF EXISTS "content_versions_write_admin"         ON app.content_versions;

CREATE POLICY "content_versions_select_authenticated"
  ON app.content_versions FOR SELECT TO authenticated
  USING (TRUE);

CREATE POLICY "content_versions_write_admin"
  ON app.content_versions FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §24  VIDEO SESSIONS (CREATE IF NOT EXISTS)
-- =============================================================================
-- impl A1.8. The consolidated schema §B7 may create app.video_session (singular).
-- Flutter code references app.video_sessions (plural). Create both forms so
-- SupabaseService.joinVideoSession / leaveVideoSession / submitSessionRating work.
-- If §B7 already created app.video_session, leave it; these are additive.

CREATE TABLE IF NOT EXISTS app.video_sessions (
  id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id            UUID        REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  room_url            TEXT,
  room_token          TEXT,
  status              TEXT        DEFAULT 'scheduled'
    CHECK (status IN ('scheduled', 'active', 'ended', 'cancelled')),
  scheduled_at        TIMESTAMPTZ,
  started_at          TIMESTAMPTZ,
  ended_at            TIMESTAMPTZ,
  payment_processed   BOOLEAN     DEFAULT FALSE,
  cohort_id           UUID,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_video_sessions_tutor
  ON app.video_sessions (tutor_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_video_sessions_status
  ON app.video_sessions (status) WHERE status IN ('scheduled', 'active');

ALTER TABLE app.video_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "video_sessions_select_participant_or_admin" ON app.video_sessions;
DROP POLICY IF EXISTS "video_sessions_insert_admin_or_tutor"       ON app.video_sessions;
DROP POLICY IF EXISTS "video_sessions_update_tutor_or_admin"       ON app.video_sessions;

CREATE POLICY "video_sessions_select_participant_or_admin"
  ON app.video_sessions FOR SELECT TO authenticated
  USING (
    tutor_id = auth.uid()
    OR app.is_admin()
    OR EXISTS (
      SELECT 1 FROM app.session_participants sp
      WHERE sp.session_id = id AND sp.user_id = auth.uid()
    )
  );

CREATE POLICY "video_sessions_insert_admin_or_tutor"
  ON app.video_sessions FOR INSERT TO authenticated
  WITH CHECK (app.is_tutor() OR app.is_admin());

CREATE POLICY "video_sessions_update_tutor_or_admin"
  ON app.video_sessions FOR UPDATE TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin())
  WITH CHECK (tutor_id = auth.uid() OR app.is_admin());

-- Session participants join table
CREATE TABLE IF NOT EXISTS app.session_participants (
  session_id UUID REFERENCES app.video_sessions(id) ON DELETE CASCADE NOT NULL,
  user_id    UUID REFERENCES app.user_profiles(id)  ON DELETE CASCADE NOT NULL,
  joined_at  TIMESTAMPTZ DEFAULT NOW(),
  left_at    TIMESTAMPTZ,
  PRIMARY KEY (session_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_session_participants_user
  ON app.session_participants (user_id);

ALTER TABLE app.session_participants ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "session_participants_own_or_tutor_admin" ON app.session_participants;

CREATE POLICY "session_participants_own_or_tutor_admin"
  ON app.session_participants FOR ALL TO authenticated
  USING (user_id = auth.uid() OR app.is_tutor() OR app.is_admin())
  WITH CHECK (user_id = auth.uid() OR app.is_admin());

-- Session ratings (distinct from video_session_rating in §3 which uses singular table name)
CREATE TABLE IF NOT EXISTS app.session_ratings (
  id         UUID     PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID     REFERENCES app.video_sessions(id) ON DELETE CASCADE NOT NULL,
  student_id UUID     REFERENCES app.user_profiles(id)  ON DELETE CASCADE NOT NULL,
  stars      SMALLINT NOT NULL CHECK (stars BETWEEN 1 AND 5),
  note       TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (session_id, student_id)
);

ALTER TABLE app.session_ratings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "session_ratings_select_own_or_tutor_admin" ON app.session_ratings;
DROP POLICY IF EXISTS "session_ratings_insert_own"                ON app.session_ratings;

CREATE POLICY "session_ratings_select_own_or_tutor_admin"
  ON app.session_ratings FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "session_ratings_insert_own"
  ON app.session_ratings FOR INSERT TO authenticated
  WITH CHECK (student_id = auth.uid());

-- =============================================================================
-- §25  COHORTS, COHORT MEMBERSHIPS & COHORT PAYMENTS
-- =============================================================================
-- impl A1.9. Separate from app.classes — cohorts are admin-managed groups
-- that may map to one or more class sections.

CREATE TABLE IF NOT EXISTS app.cohorts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  description TEXT,
  tutor_id    UUID REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  max_students INT DEFAULT 20,
  start_date  DATE,
  end_date    DATE,
  is_active   BOOLEAN DEFAULT TRUE,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cohorts_tutor    ON app.cohorts (tutor_id);
CREATE INDEX IF NOT EXISTS idx_cohorts_active   ON app.cohorts (is_active) WHERE is_active = TRUE;

ALTER TABLE app.cohorts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "cohorts_select_all_auth"  ON app.cohorts;
DROP POLICY IF EXISTS "cohorts_write_admin"      ON app.cohorts;

CREATE POLICY "cohorts_select_all_auth"
  ON app.cohorts FOR SELECT TO authenticated USING (TRUE);

CREATE POLICY "cohorts_write_admin"
  ON app.cohorts FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- Memberships
CREATE TABLE IF NOT EXISTS app.cohort_memberships (
  cohort_id  UUID REFERENCES app.cohorts(id)        ON DELETE CASCADE NOT NULL,
  user_id    UUID REFERENCES app.user_profiles(id)  ON DELETE CASCADE NOT NULL,
  joined_at  TIMESTAMPTZ DEFAULT NOW(),
  status     TEXT DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'graduated', 'dropped')),
  PRIMARY KEY (cohort_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_cohort_memberships_user
  ON app.cohort_memberships (user_id, status);

ALTER TABLE app.cohort_memberships ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "cohort_memberships_select_own_or_tutor_admin" ON app.cohort_memberships;
DROP POLICY IF EXISTS "cohort_memberships_write_admin"               ON app.cohort_memberships;

CREATE POLICY "cohort_memberships_select_own_or_tutor_admin"
  ON app.cohort_memberships FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "cohort_memberships_write_admin"
  ON app.cohort_memberships FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- Payments
CREATE TABLE IF NOT EXISTS app.cohort_payments (
  id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id     UUID    REFERENCES app.cohorts(id) ON DELETE SET NULL,
  student_id    UUID    REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  tutor_id      UUID    REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  amount        NUMERIC(10,2) NOT NULL,
  currency      TEXT    DEFAULT 'USD',
  status        TEXT    DEFAULT 'pending'
    CHECK (status IN ('pending', 'paid', 'failed', 'refunded')),
  stripe_payment_intent_id TEXT,
  paid_at       TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cohort_payments_student ON app.cohort_payments (student_id);
CREATE INDEX IF NOT EXISTS idx_cohort_payments_tutor   ON app.cohort_payments (tutor_id, status);

ALTER TABLE app.cohort_payments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "cohort_payments_select_own_or_tutor_admin" ON app.cohort_payments;
DROP POLICY IF EXISTS "cohort_payments_write_admin"               ON app.cohort_payments;

CREATE POLICY "cohort_payments_select_own_or_tutor_admin"
  ON app.cohort_payments FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR tutor_id = auth.uid() OR app.is_admin());

CREATE POLICY "cohort_payments_write_admin"
  ON app.cohort_payments FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §26  TIER GATING RULES
-- =============================================================================
-- impl A1.10. Defines which tier is required for each feature key.
-- Flutter TierGatingService reads this on startup and caches locally.

CREATE TABLE IF NOT EXISTS app.tier_gating_rules (
  feature     TEXT PRIMARY KEY,
  min_tier    TEXT NOT NULL
    CHECK (min_tier IN ('free', 'learner', 'tutor_tier', 'intensive')),
  description TEXT,
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE app.tier_gating_rules ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "tier_gating_rules_select_authenticated" ON app.tier_gating_rules;
DROP POLICY IF EXISTS "tier_gating_rules_write_admin"          ON app.tier_gating_rules;

CREATE POLICY "tier_gating_rules_select_authenticated"
  ON app.tier_gating_rules FOR SELECT TO authenticated USING (TRUE);

CREATE POLICY "tier_gating_rules_write_admin"
  ON app.tier_gating_rules FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- Seed tier gating rules
INSERT INTO app.tier_gating_rules (feature, min_tier, description)
VALUES
  ('ai_practice',        'learner',     'Access to AI conversation and drill practice modes'),
  ('ai_unlimited',       'learner',     'Unlimited AI practice sessions (free tier capped at 5/day)'),
  ('ai_pronunciation',   'intensive',   'AI pronunciation coaching with Whisper scoring'),
  ('video_session',      'learner',     'Book live 1:1 or group video sessions with tutors'),
  ('anki_sync',          'learner',     'Sync flashcard reviews to cloud for multi-device access'),
  ('explore_download',   'learner',     'Download Explore content for offline use'),
  ('language_exchange',  'free',        'Match with language exchange partners (free for all)'),
  ('broadcasts',         'free',        'Read tutor/admin broadcasts (free for all)'),
  ('cohort_management',  'tutor_tier',  'Admin: manage cohorts, students, and payroll'),
  ('content_creation',   'tutor_tier',  'Create and publish course content (tutors and admins)'),
  ('admin_dashboard',    'tutor_tier',  'Admin dashboard access (admin role required in addition to tier)')
ON CONFLICT (feature) DO NOTHING;

-- =============================================================================
-- §27  STRIPE EVENTS LOG
-- =============================================================================
-- impl A1.12. Idempotency log for the Stripe webhook Edge Function.

CREATE TABLE IF NOT EXISTS app.stripe_events (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stripe_event_id  TEXT UNIQUE NOT NULL,
  type             TEXT NOT NULL,
  payload          JSONB,
  processed_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_type
  ON app.stripe_events (type, processed_at DESC);

-- Only service role writes to this table — no RLS needed for authenticated users.
-- Enable RLS but grant no authenticated policies (service role bypasses RLS).
ALTER TABLE app.stripe_events ENABLE ROW LEVEL SECURITY;

-- =============================================================================
-- §28  ACHIEVEMENTS
-- =============================================================================
-- impl O2.1. Catalogue of awards + per-user earned table.

CREATE TABLE IF NOT EXISTS app.achievements (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key         TEXT UNIQUE NOT NULL,
  name        TEXT NOT NULL,
  description TEXT,
  icon_name   TEXT,         -- maps to a Flutter Icons constant name
  xp_reward   INT DEFAULT 0,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE app.achievements ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "achievements_select_authenticated" ON app.achievements;

CREATE POLICY "achievements_select_authenticated"
  ON app.achievements FOR SELECT TO authenticated USING (TRUE);

-- Per-user earned achievements
CREATE TABLE IF NOT EXISTS app.user_achievements (
  user_id        UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  achievement_id UUID REFERENCES app.achievements(id)  ON DELETE CASCADE NOT NULL,
  earned_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, achievement_id)
);

CREATE INDEX IF NOT EXISTS idx_user_achievements_user
  ON app.user_achievements (user_id, earned_at DESC);

ALTER TABLE app.user_achievements ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "user_achievements_select_own_or_tutor_admin" ON app.user_achievements;
DROP POLICY IF EXISTS "user_achievements_insert_own"                ON app.user_achievements;

CREATE POLICY "user_achievements_select_own_or_tutor_admin"
  ON app.user_achievements FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "user_achievements_insert_own"
  ON app.user_achievements FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

-- Seed achievement catalogue
INSERT INTO app.achievements (key, name, description, icon_name, xp_reward)
VALUES
  ('first_lesson',       'First Step',         'Complete your first lesson',               'school',          10),
  ('streak_7',           'Week Warrior',        'Maintain a 7-day learning streak',         'local_fire_department', 50),
  ('streak_30',          'Monthly Master',      'Maintain a 30-day learning streak',        'emoji_events',   200),
  ('drills_50',          'Drill Sergeant',      'Complete 50 AI practice drills',           'quiz',            75),
  ('first_video',        'Face to Face',        'Join your first live video session',       'videocam',        30),
  ('first_anki',         'Card Collector',      'Review your first flashcard',              'style',            5),
  ('anki_100',           'Deck Master',         'Review 100 flashcards',                    'auto_stories',    50),
  ('first_recording',    'Voice First',         'Submit your first pronunciation recording','mic',             15),
  ('exchange_partner',   'Language Bridge',     'Connect with a language exchange partner', 'people',          25),
  ('xp_500',             'XP Milestone',        'Earn 500 total XP',                        'stars',           30),
  ('xp_5000',            'XP Legend',           'Earn 5000 total XP',                       'military_tech',  150)
ON CONFLICT (key) DO NOTHING;

-- =============================================================================
-- §29  TUTOR RATES
-- =============================================================================
-- Per-tutor per-session-type rate card used by the payroll Edge Function (G3.1).

CREATE TABLE IF NOT EXISTS app.tutor_rates (
  id           UUID     PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id     UUID     REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  session_type TEXT     NOT NULL
    CHECK (session_type IN ('group', 'solo', 'explore_contribution')),
  rate_amount  NUMERIC(10,2) NOT NULL,
  currency     TEXT     DEFAULT 'USD',
  effective_from DATE   DEFAULT CURRENT_DATE,
  effective_to   DATE,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tutor_id, session_type, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_tutor_rates_tutor
  ON app.tutor_rates (tutor_id, session_type);

ALTER TABLE app.tutor_rates ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "tutor_rates_select_own_or_admin" ON app.tutor_rates;
DROP POLICY IF EXISTS "tutor_rates_write_admin"         ON app.tutor_rates;

CREATE POLICY "tutor_rates_select_own_or_admin"
  ON app.tutor_rates FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());

CREATE POLICY "tutor_rates_write_admin"
  ON app.tutor_rates FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §30  APP CONFIG
-- =============================================================================
-- Key-value store for server-driven config (impl N3.1: min_version check).
-- Written by admins; read by all authenticated users on startup.

CREATE TABLE IF NOT EXISTS app.config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE app.config ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "config_select_authenticated" ON app.config;
DROP POLICY IF EXISTS "config_write_admin"          ON app.config;

CREATE POLICY "config_select_authenticated"
  ON app.config FOR SELECT TO authenticated USING (TRUE);

CREATE POLICY "config_write_admin"
  ON app.config FOR ALL TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- Seed required config keys
INSERT INTO app.config (key, value)
VALUES
  ('min_version',            '1.0.0'),
  ('android_store_url',      'https://play.google.com/store/apps/details?id=app.tauka'),
  ('ios_store_url',          'https://apps.apple.com/app/tauka/id0000000000'),
  ('stripe_checkout_url',    'https://buy.stripe.com/tauka_placeholder'),
  ('maintenance_mode',       'false'),
  ('maintenance_message',    'We are performing scheduled maintenance. Back shortly.')
ON CONFLICT (key) DO NOTHING;

-- =============================================================================
-- §31  CONTENT APPROVAL REQUESTS
-- =============================================================================
-- impl D2.1, F3.1. Tutors submit edit requests; admins approve/reject.

CREATE TABLE IF NOT EXISTS app.content_approval_requests (
  id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  contributor_id UUID    REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  content_type   TEXT    NOT NULL
    CHECK (content_type IN ('lesson', 'unit', 'dictionary_entry', 'explore_content')),
  content_id     UUID,
  title          TEXT    NOT NULL,
  description    TEXT,
  payload        JSONB,              -- full proposed content for preview
  status         TEXT    DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected', 'published')),
  reviewed_by    UUID    REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  reviewed_at    TIMESTAMPTZ,
  review_note    TEXT,
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_car_status       ON app.content_approval_requests (status)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_car_contributor  ON app.content_approval_requests (contributor_id);

ALTER TABLE app.content_approval_requests ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "car_select_own_or_admin"  ON app.content_approval_requests;
DROP POLICY IF EXISTS "car_insert_tutor_or_admin" ON app.content_approval_requests;
DROP POLICY IF EXISTS "car_update_admin"          ON app.content_approval_requests;

CREATE POLICY "car_select_own_or_admin"
  ON app.content_approval_requests FOR SELECT TO authenticated
  USING (contributor_id = auth.uid() OR app.is_admin());

CREATE POLICY "car_insert_tutor_or_admin"
  ON app.content_approval_requests FOR INSERT TO authenticated
  WITH CHECK (
    contributor_id = auth.uid()
    AND (app.is_tutor() OR app.is_admin())
  );

CREATE POLICY "car_update_admin"
  ON app.content_approval_requests FOR UPDATE TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §32  COHORT ASSIGNMENTS
-- =============================================================================
-- impl D4.1. Tutors assign Anki decks (or other content) to cohorts.

CREATE TABLE IF NOT EXISTS app.cohort_assignments (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id   UUID REFERENCES app.cohorts(id)        ON DELETE CASCADE NOT NULL,
  assigned_by UUID REFERENCES app.user_profiles(id)  ON DELETE SET NULL,
  deck_id     UUID,          -- local Anki deck ID (UUID from bundled JSON)
  content_ref TEXT,          -- free-form reference for non-Anki assignments
  due_date    DATE,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cohort_assignments_cohort
  ON app.cohort_assignments (cohort_id, due_date);

ALTER TABLE app.cohort_assignments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "cohort_assignments_select_member_or_tutor_admin" ON app.cohort_assignments;
DROP POLICY IF EXISTS "cohort_assignments_write_tutor_or_admin"         ON app.cohort_assignments;

CREATE POLICY "cohort_assignments_select_member_or_tutor_admin"
  ON app.cohort_assignments FOR SELECT TO authenticated
  USING (
    app.is_tutor() OR app.is_admin()
    OR EXISTS (
      SELECT 1 FROM app.cohort_memberships cm
      WHERE cm.cohort_id = cohort_id AND cm.user_id = auth.uid()
    )
  );

CREATE POLICY "cohort_assignments_write_tutor_or_admin"
  ON app.cohort_assignments FOR ALL TO authenticated
  USING (app.is_tutor() OR app.is_admin())
  WITH CHECK (app.is_tutor() OR app.is_admin());

-- =============================================================================
-- §33  DICTIONARY CONTRIBUTIONS
-- =============================================================================
-- impl D3.1. Tutors/admins submit word entries for approval.

CREATE TABLE IF NOT EXISTS app.dictionary_contributions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contributor_id   UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  word             TEXT NOT NULL,                -- in target script (Amharic/Arabic)
  transliteration  TEXT,
  definition       TEXT NOT NULL,
  example_sentence TEXT,
  audio_path       TEXT,                         -- Supabase Storage path
  language         TEXT NOT NULL
    CHECK (language IN ('amharic', 'arabic')),
  status           TEXT DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected')),
  reviewed_by      UUID REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  reviewed_at      TIMESTAMPTZ,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dict_contrib_status
  ON app.dictionary_contributions (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_dict_contrib_contributor
  ON app.dictionary_contributions (contributor_id);

ALTER TABLE app.dictionary_contributions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "dict_contrib_select_own_or_admin"    ON app.dictionary_contributions;
DROP POLICY IF EXISTS "dict_contrib_insert_tutor_or_admin"  ON app.dictionary_contributions;
DROP POLICY IF EXISTS "dict_contrib_update_admin"           ON app.dictionary_contributions;

CREATE POLICY "dict_contrib_select_own_or_admin"
  ON app.dictionary_contributions FOR SELECT TO authenticated
  USING (contributor_id = auth.uid() OR app.is_admin());

CREATE POLICY "dict_contrib_insert_tutor_or_admin"
  ON app.dictionary_contributions FOR INSERT TO authenticated
  WITH CHECK (
    contributor_id = auth.uid()
    AND (app.is_tutor() OR app.is_admin())
  );

CREATE POLICY "dict_contrib_update_admin"
  ON app.dictionary_contributions FOR UPDATE TO authenticated
  USING (app.is_admin())
  WITH CHECK (app.is_admin());

-- =============================================================================
-- §34  PG_CRON JOBS
-- =============================================================================
-- impl I3.1. Requires pg_cron extension enabled in Supabase dashboard.
-- These are idempotent — running again updates the existing cron entry.

-- Session reminder: every 10 minutes, trigger the send-push Edge Function
-- for sessions starting in 25–35 minutes (handled by the Edge Function logic).
-- Replace <PROJECT_REF> with your Supabase project reference.
-- Replace <ANON_KEY> with your Supabase anon/service-role key.
--
-- SELECT cron.schedule(
--   'session-reminders',
--   '*/10 * * * *',
--   $$
--     SELECT net.http_post(
--       url     := 'https://<PROJECT_REF>.functions.supabase.co/send-push',
--       headers := '{"Content-Type":"application/json","Authorization":"Bearer <SERVICE_ROLE_KEY>"}'::jsonb,
--       body    := '{"trigger":"session_reminder"}'::jsonb
--     );
--   $$
-- );

-- Weekly XP reset: every Monday at 00:00 UTC, zero out xp_week for all users.
SELECT cron.schedule(
  'weekly-xp-reset',
  '0 0 * * 1',
  $$UPDATE app.user_profiles SET xp_week = 0 WHERE xp_week > 0;$$
);

-- =============================================================================
-- §35  ACHIEVEMENTS (O2.1)
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.achievements (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key         TEXT UNIQUE NOT NULL,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,
  icon_name   TEXT NOT NULL DEFAULT 'star',
  xp_reward   INT  NOT NULL DEFAULT 50,
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.user_achievements (
  user_id        UUID NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  achievement_id UUID NOT NULL REFERENCES app.achievements(id) ON DELETE CASCADE,
  earned_at      TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, achievement_id)
);

ALTER TABLE app.user_achievements ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own achievements"
  ON app.user_achievements FOR SELECT
  USING (auth.uid() = user_id);

CREATE POLICY "Service role can insert achievements"
  ON app.user_achievements FOR INSERT
  WITH CHECK (auth.uid() = user_id);

-- Seed achievement definitions
INSERT INTO app.achievements (key, name, description, icon_name, xp_reward)
VALUES
  ('first_lesson',       'First Step',          'Complete your first lesson',          'school',          50),
  ('streak_7',           'Week Warrior',         '7-day study streak',                  'local_fire_department', 100),
  ('streak_30',          'Monthly Master',       '30-day study streak',                 'emoji_events',    300),
  ('drills_50',          'Drill Sergeant',       'Complete 50 AI drills',               'fitness_center',  100),
  ('drills_200',         'Drill Master',         'Complete 200 AI drills',              'military_tech',   250),
  ('first_video',        'Face Time',            'Join your first video session',        'videocam',        75),
  ('anki_100',           'Card Shark',           'Review 100 Anki flashcards',           'style',           100),
  ('dictionary_contrib', 'Word Weaver',          'Submit a dictionary contribution',     'edit_note',       150),
  ('language_exchange',  'Exchange Student',     'Connect with a language partner',      'people',          75),
  ('xp_1000',            'XP Milestone: 1000',  'Earn 1,000 total XP',                 'stars',           100),
  ('xp_5000',            'XP Milestone: 5000',  'Earn 5,000 total XP',                 'workspace_premium', 250)
ON CONFLICT (key) DO NOTHING;

-- =====================================================================
-- END OF additional_changes.sql
-- =====================================================================
--
-- APPLY ORDER REMINDER
-- ─────────────────────────────────────────────────────────────────────
-- 1. consolidated_full_db_remote_schema.sql  Part A  (current schema)
-- 2. consolidated_full_db_remote_schema.sql  Part B  (§B1 through §B12)
-- 3. THIS FILE                               §0 through §34
--
-- After applying §B7 (video_session table), uncomment the FK constraints
-- in §3 (video_session_rating) and §4 (video_session_note).
-- After applying §B8 (exercise_result table), uncomment the FK in §13
-- (payroll_line_item.session_id).
-- For §24 (video_sessions): if §B7 already created app.video_session
-- (singular), the FK in §3/§4 points there. app.video_sessions (plural)
-- in §24 is the table Flutter code queries — reconcile names as needed.
-- For §34 (pg_cron): enable pg_cron extension in Supabase dashboard first.
-- Uncomment the session-reminders cron and fill in PROJECT_REF + key.
-- =====================================================================

-- =============================================================================
-- §36  K3.1 — RLS POLICY AUDIT for Group A tables
-- =============================================================================
-- Run after §0 through §35. Idempotent (uses CREATE POLICY IF NOT EXISTS via
-- DO block + exception handling).
-- =============================================================================

DO $$
BEGIN

  -- ── app.exercise_sessions ───────────────────────────────────────────────────
  ALTER TABLE IF EXISTS app.exercise_sessions ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY es_own_insert ON app.exercise_sessions
      FOR INSERT WITH CHECK (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY es_own_select ON app.exercise_sessions
      FOR SELECT USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY es_own_update ON app.exercise_sessions
      FOR UPDATE USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;

  -- ── app.flashcard_reviews ───────────────────────────────────────────────────
  ALTER TABLE IF EXISTS app.flashcard_reviews ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY fr_own_insert ON app.flashcard_reviews
      FOR INSERT WITH CHECK (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY fr_own_select ON app.flashcard_reviews
      FOR SELECT USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY fr_own_upsert ON app.flashcard_reviews
      FOR UPDATE USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;

  -- ── app.voice_recordings ────────────────────────────────────────────────────
  ALTER TABLE IF EXISTS app.voice_recordings ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY vr_own_insert ON app.voice_recordings
      FOR INSERT WITH CHECK (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY vr_own_select ON app.voice_recordings
      FOR SELECT USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;

  -- ── app.broadcast_reads ─────────────────────────────────────────────────────
  ALTER TABLE IF EXISTS app.broadcast_reads ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY br_own_insert ON app.broadcast_reads
      FOR INSERT WITH CHECK (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY br_own_select ON app.broadcast_reads
      FOR SELECT USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;

  -- ── app.push_tokens ─────────────────────────────────────────────────────────
  ALTER TABLE IF EXISTS app.push_tokens ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY pt_own_insert ON app.push_tokens
      FOR INSERT WITH CHECK (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY pt_own_select ON app.push_tokens
      FOR SELECT USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY pt_own_delete ON app.push_tokens
      FOR DELETE USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;

  -- ── app.content_versions (read-only for all authenticated users) ────────────
  ALTER TABLE IF EXISTS app.content_versions ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY cv_auth_read ON app.content_versions
      FOR SELECT USING (auth.role() = 'authenticated');
  EXCEPTION WHEN duplicate_object THEN NULL; END;

  -- ── app.user_achievements ───────────────────────────────────────────────────
  ALTER TABLE IF EXISTS app.user_achievements ENABLE ROW LEVEL SECURITY;
  BEGIN
    CREATE POLICY ua_own_select ON app.user_achievements
      FOR SELECT USING (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN
    CREATE POLICY ua_service_insert ON app.user_achievements
      FOR INSERT WITH CHECK (user_id = auth.uid());
  EXCEPTION WHEN duplicate_object THEN NULL; END;

END $$;

-- Storage bucket policy: voice-recordings — users can upload only to their own prefix
-- Run in Supabase dashboard under Storage > Policies if not already present:
--   INSERT INTO storage.objects: (bucket_id = 'voice-recordings' AND auth.uid()::text = split_part(name,'/',1))
--   SELECT: (bucket_id = 'voice-recordings' AND auth.uid()::text = split_part(name,'/',1))

-- =============================================================================
-- §37  O1.2 — SESSION RECORDINGS
-- =============================================================================
-- Stores metadata for completed Daily.co recordings uploaded to Storage.
-- Storage path: recordings/{sessionId}/{recordingId}.mp4
-- RLS: session participants and tutor can read; admins can read all.

CREATE TABLE IF NOT EXISTS app.session_recordings (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id   UUID NOT NULL,   -- REFERENCES app.video_sessions(id) — FK after §24 confirmed
  storage_path TEXT NOT NULL,
  duration_s   INT,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_recordings_session
  ON app.session_recordings (session_id);

ALTER TABLE app.session_recordings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "sr_select_participant_or_admin" ON app.session_recordings;

CREATE POLICY "sr_select_participant_or_admin"
  ON app.session_recordings FOR SELECT TO authenticated
  USING (
    app.is_admin()
    OR EXISTS (
      SELECT 1 FROM app.session_participants sp
      WHERE sp.session_id = session_recordings.session_id
        AND sp.user_id = auth.uid()
    )
    OR EXISTS (
      SELECT 1 FROM app.video_sessions vs
      WHERE vs.id = session_recordings.session_id
        AND vs.tutor_id = auth.uid()
    )
  );

-- Only service role (Edge Function) inserts recordings — no authenticated INSERT policy.

-- =============================================================================
-- §38  O2.1 — NO-SHOW DETECTION: column + cron job
-- =============================================================================
-- Adds no_show_notified_at to video_sessions so check-sessions Edge Function
-- can avoid duplicate notifications.

ALTER TABLE app.video_sessions
  ADD COLUMN IF NOT EXISTS no_show_notified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_video_sessions_no_show
  ON app.video_sessions (no_show_notified_at)
  WHERE no_show_notified_at IS NULL AND status = 'scheduled';

-- pg_cron: run check-sessions every minute to catch tutor no-shows
-- Uncomment and fill in PROJECT_REF + SERVICE_ROLE_KEY to activate:
/*
SELECT cron.schedule(
  'check-sessions-no-show',
  '* * * * *',
  $$
    SELECT net.http_post(
      url     := 'https://PROJECT_REF.functions.supabase.co/check-sessions',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer SERVICE_ROLE_KEY'
      ),
      body    := '{}'::jsonb
    );
  $$
);
*/

-- =============================================================================
-- §39  R1.1 — PROGRESS SNAPSHOTS
-- =============================================================================
-- Stores a per-user per-course progress snapshot after each session.
-- Written by the Flutter app via ProgressProvider.endSession().

CREATE TABLE IF NOT EXISTS app.progress_snapshots (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           UUID REFERENCES app.user_profiles(id) ON DELETE CASCADE NOT NULL,
  course_id         UUID NOT NULL,
  lessons_completed INT  NOT NULL DEFAULT 0,
  accuracy          NUMERIC(5,4) CHECK (accuracy BETWEEN 0 AND 1),
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, course_id)
);

CREATE INDEX IF NOT EXISTS idx_progress_snapshots_user
  ON app.progress_snapshots (user_id, created_at DESC);

ALTER TABLE app.progress_snapshots ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "ps_select_own_or_tutor_admin" ON app.progress_snapshots;
DROP POLICY IF EXISTS "ps_upsert_own"                ON app.progress_snapshots;

CREATE POLICY "ps_select_own_or_tutor_admin"
  ON app.progress_snapshots FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_tutor() OR app.is_admin());

CREATE POLICY "ps_upsert_own"
  ON app.progress_snapshots FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================================
-- §40  I3.1 — pg_cron: 30-minute session reminder push notifications
-- =============================================================================
-- Prerequisites:
--   1. Enable pg_cron extension: Supabase dashboard → Extensions → pg_cron
--   2. Enable pg_net extension: Supabase dashboard → Extensions → pg_net
--   3. Deploy send-push Edge Function (supabase/functions/send-push/)
--   4. Replace PROJECT_REF and SERVICE_ROLE_KEY below
-- =============================================================================

-- Schedule: every 10 minutes, look for sessions starting in 25-35 minutes
-- and send push reminders to all participants.

/*  Uncomment and fill in PROJECT_REF and SERVICE_ROLE_KEY to activate:

SELECT cron.schedule(
  'session-reminders',
  '*/10 * * * *',
  $$
    SELECT
      net.http_post(
        url := 'https://PROJECT_REF.functions.supabase.co/session-reminder-worker',
        headers := jsonb_build_object(
          'Content-Type',  'application/json',
          'Authorization', 'Bearer SERVICE_ROLE_KEY'
        ),
        body := '{}'::jsonb
      )
  $$
);

*/

-- Companion worker function (deploy as a separate Edge Function or inline RPC):
-- For each participant in sessions starting in 25-35 min, call send-push.
--
-- supabase/functions/session-reminder-worker/index.ts would:
--   1. SELECT vs.id, sp.user_id FROM app.video_sessions vs
--      JOIN app.session_participants sp ON sp.session_id = vs.id
--      WHERE vs.scheduled_at BETWEEN now() + INTERVAL '25 minutes'
--                                 AND now() + INTERVAL '35 minutes'
--        AND vs.reminder_sent IS DISTINCT FROM true
--   2. For each user_id: POST to send-push { userId, title: 'Session starting soon', body: '...' }

-- =============================================================================
-- §41  U1.1 — RLS: BLOCK SELF-PROMOTION OF user_type AND subscription_tier
-- =============================================================================
-- Users may update their own profile rows but may NOT change user_type or
-- subscription_tier. Those columns are managed exclusively by server-side
-- code (Edge Functions / service-role key).

-- Drop old permissive UPDATE policy if it exists.
DROP POLICY IF EXISTS "users can update own profile" ON app.profiles;

-- Allow authenticated users to update their own row, but only when the
-- protected columns remain unchanged (WITH CHECK pins their current values).
CREATE POLICY "users update own profile"
  ON app.profiles
  FOR UPDATE
  TO authenticated
  USING (auth.uid() = id)
  WITH CHECK (
    user_type          = (SELECT user_type          FROM app.profiles WHERE id = auth.uid()) AND
    subscription_tier  = (SELECT subscription_tier  FROM app.profiles WHERE id = auth.uid())
  );

-- Service-role helper: allows Edge Functions / webhooks to freely update
-- subscription_tier without being blocked by the row-level policy above.
-- (The service role bypasses RLS by default; this comment documents intent.)
-- To update subscription_tier from an Edge Function, always use the
-- service-role key, never the anon key.

-- =============================================================================
-- §42  U2.1 — RLS: RESTRICT TUTORS TO ASSIGNED STUDENTS ONLY
-- =============================================================================

-- ── exercise_sessions ────────────────────────────────────────────────────────
DROP POLICY IF EXISTS "tutor reads assigned students exercise sessions" ON app.exercise_sessions;
CREATE POLICY "tutor reads assigned students exercise sessions"
  ON app.exercise_sessions
  FOR SELECT
  TO authenticated
  USING (
    auth.uid() = user_id
    OR auth.uid() IN (
      SELECT tutor_id FROM app.tutor_assignment
      WHERE student_id = exercise_sessions.user_id
    )
  );

-- ── progress_snapshots ───────────────────────────────────────────────────────
DROP POLICY IF EXISTS "tutor reads assigned students progress" ON app.progress_snapshots;
CREATE POLICY "tutor reads assigned students progress"
  ON app.progress_snapshots
  FOR SELECT
  TO authenticated
  USING (
    auth.uid() = user_id
    OR auth.uid() IN (
      SELECT tutor_id FROM app.tutor_assignment
      WHERE student_id = progress_snapshots.user_id
    )
  );

-- ── session_participants ─────────────────────────────────────────────────────
-- Tutors may read participant records only for sessions they are the host of
-- or where they are directly listed as a participant.
DROP POLICY IF EXISTS "tutor reads session participants" ON app.session_participants;
CREATE POLICY "tutor reads session participants"
  ON app.session_participants
  FOR SELECT
  TO authenticated
  USING (
    auth.uid() = user_id
    OR auth.uid() IN (
      SELECT host_id FROM app.video_sessions1
      WHERE id = session_participants.session_id
    )
  );
--   3. UPDATE app.video_sessions SET reminder_sent = true WHERE id = vs.id

-- =============================================================================
-- §43  F1.1 — pg_cron: DAILY STREAK RESET
-- =============================================================================
-- Runs at 03:00 UTC every day.
-- Zeroes streak_days for any profile whose last_active_date is more than 1 day
-- in the past, complementing the client-side updateStreak() which increments.
-- Requires the pg_cron extension (enabled in Supabase Dashboard → Extensions).
-- Run once in the Supabase SQL editor to register the job.
-- =============================================================================

SELECT cron.schedule(
  'streak-reset',
  '0 3 * * *',
  $$
    UPDATE app.profiles
    SET streak_days = 0
    WHERE last_active_date < CURRENT_DATE - INTERVAL '1 day'
      AND streak_days > 0;
  $$
);
