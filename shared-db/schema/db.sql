-- ============================================================
-- SHARED DATABASE SCHEMA — TAUKA
-- Last updated by: tauka-flutter on 2026-05-26
-- ============================================================
-- RULES:
-- 1. Each table is tagged with its owner project and domain
-- 2. Do NOT modify tables owned by other projects
-- 3. If you find a conflict, do NOT fix it here — log it in CONFLICT_LOG.md
-- 4. Tables are grouped by domain, alphabetical within domain
-- ============================================================
-- SOURCE OF TRUTH:
--   This file is the annotated reference schema.
--   Full DDL (including RLS policies, indexes, triggers, and functions):
--     tauka_full_schema.sql  (Parts A + B + §00 – §49, fully consolidated)
--   This file is fully applied to the remote Supabase database.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS app;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- DOMAIN: Identity & Access (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Identity & Access] [OWNER: tauka-flutter] [SPEC: §2.1 User Roles, §3.1 First-Time Sign-In]
-- PURPOSE: Extends auth.users with onboarding fields, subscription tier cache, streak/XP, and device limit.
--          This is the primary profile row read by every part of the app after auth.
-- SHARED WRITE: subscription_tier and tier_updated_at are written by trigger trg_sync_student_tier only.
-- USED BY: tauka-python (reads id, subscription_tier, user_type for billing and tier checks)
CREATE TABLE app.user_profiles (
  id                    uuid        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  first_name            text        NOT NULL,
  last_name             text        NULL,
  avatar_url            text        NULL,
  location              text        NULL,
  phone_number          text        NULL,
  deleted_at            timestamptz NULL,
  created_at            timestamptz DEFAULT now(),
  known_languages       jsonb       NULL DEFAULT '[]'::jsonb,
  user_type             text        NULL CHECK (user_type IS NULL OR user_type = ANY(ARRAY['tutor','student'])),
  study_method          text        NULL,
  teaching_options      jsonb       NULL DEFAULT '[]'::jsonb,
  completed_at          timestamptz NULL,
  onboarding_created_at timestamptz NULL,
  onboarding_location   jsonb       NULL,
  -- Added via §04 (tauka_full_schema.sql)
  subscription_tier     text        DEFAULT 'free'
                        CHECK (subscription_tier IN ('free','learner','tutor','intensive') OR subscription_tier IS NULL),
  tier_updated_at       timestamptz,
  streak_days           int         DEFAULT 0,
  last_active_date      date,
  xp_total              int         DEFAULT 0,
  xp_week               int         DEFAULT 0,
  subscription_expires_at timestamptz,
  device_limit          int         DEFAULT 2,
  devices               jsonb       DEFAULT '[]',
  proficiency_level     text        CHECK (proficiency_level IN ('beginner','intermediate','advanced') OR proficiency_level IS NULL),
  -- Added via §B1 (Part B migrations)
  role                  text        NOT NULL DEFAULT 'student' CHECK (role IN ('student','tutor','admin')),
  suspended_at          timestamptz DEFAULT NULL,
  suspended_by          uuid        REFERENCES app.user_profiles(id) DEFAULT NULL,
  suspension_reason     text        DEFAULT NULL
);

-- [DOMAIN: Identity & Access] [OWNER: tauka-flutter] [SPEC: §2.1, §7 Admin]
-- PURPOSE: Separate admin identity table. Admins also have a user_profiles row.
--          Using app.is_admin() SECURITY DEFINER function prevents 42P17 recursive policy evaluation.
-- USED BY: tauka-python (reads for admin check via is_admin())
CREATE TABLE app.admin_users (
  id          uuid                  PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  role        varchar(50)           DEFAULT 'admin',
  permissions jsonb                 DEFAULT '{"users": true, "content": true, "settings": true}'::jsonb,
  created_at  timestamptz           DEFAULT now(),
  created_by  uuid                  REFERENCES auth.users(id),
  updated_at  timestamptz           DEFAULT now(),
  deleted_at  timestamptz           NULL
);

-- [DOMAIN: Identity & Access] [OWNER: tauka-flutter] [SPEC: §1 Device-pair model, §3.2 Device-Connect]
-- PURPOSE: One row per device per user. Enforces the one-mobile + one-desktop device limit.
--          Supports QR-code device-connect flow (feature flag: device_pairing).
-- NOTE: deleted_at added via §B11 migration.
CREATE TABLE app.devices (
  id         uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  device_id  text    NOT NULL UNIQUE,
  platform   text    NOT NULL CHECK (platform IN ('ios','android','windows','macos','linux','web')),
  is_primary boolean DEFAULT false,
  last_seen  timestamptz DEFAULT now(),
  created_at timestamptz DEFAULT now(),
  deleted_at timestamptz NULL  -- added via §B11
);

-- ============================================================
-- DOMAIN: Student & Tutor Identity (Owner: tauka-flutter; tauka-python writes student.tier cols)
-- ============================================================

-- [DOMAIN: Student & Tutor Identity] [OWNER: tauka-flutter + tauka-python (tier/stripe cols)] [SPEC: §2.1, §9 Subscription]
-- PURPOSE: One row per student. Python backend owns Stripe/tier columns.
--          Flutter owns tutor_id, language, account_status, and basic stats.
-- SHARED WRITE: tier, stripe_customer_id, stripe_subscription_id, subscription_status,
--               active_gift_id, current_period_end — written ONLY by tauka-python via service role.
-- USED BY: tauka-python (billing, tier assignment, milestone detection)
CREATE TABLE app.student (
  id                        uuid        NOT NULL PRIMARY KEY,
  created_at                timestamptz NOT NULL DEFAULT now(),
  is_con_creator            text        NULL,
  account_status            text        NULL DEFAULT 'active',
  tutor_id                  uuid        NULL REFERENCES app.tutor(id) ON DELETE SET NULL,
  deleted_at                timestamptz NULL,
  -- §01 Stripe/tier additions (tauka-python authoritative)
  tier                      text        NOT NULL DEFAULT 'free',
  tier_source               text        NOT NULL DEFAULT 'self',
  stripe_customer_id        text        UNIQUE,
  stripe_subscription_id    text,
  subscription_status       text        DEFAULT 'none',
  active_gift_id            uuid,   -- FK added after §14: gift_subscriptions(id)
  original_tier             text,
  current_period_end        timestamptz,
  -- §01 Flutter-writable stats
  lessons_completed         int         DEFAULT 0,
  current_streak            int         DEFAULT 0,  -- Python-validated streak
  ai_conversations          int         DEFAULT 0,
  cohort_sessions_attended  int         DEFAULT 0,
  assessed_level            text,
  last_unit_milestone       int         DEFAULT 0,
  streak_14_notified        boolean     DEFAULT false,
  first_ai_convo_notified   boolean     DEFAULT false,
  first_cohort_notified     boolean     DEFAULT false,
  last_notified_level       text,
  language                  text,
  tutor_started_at          timestamptz,
  tutor_ended_at            timestamptz,
  updated_at                timestamptz DEFAULT now(),
  CONSTRAINT student_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE
);

-- [DOMAIN: Student & Tutor Identity] [OWNER: tauka-flutter] [SPEC: §6 Tutor Features]
-- PURPOSE: One row per tutor. id IS the auth user id (same UUID as user_profiles.id).
--          Extended by §02 with financial/bio fields for tutor portal.
-- USED BY: tauka-python (reads per_session_rate_cents for earnings calculation)
CREATE TABLE app.tutor (
  id                    uuid        NOT NULL PRIMARY KEY,
  created_at            timestamptz NOT NULL DEFAULT now(),
  rating                bigint      NULL,
  status                text        NULL DEFAULT 'active',
  deleted_at            timestamptz NULL,
  -- §02 additions
  per_session_rate_cents int        DEFAULT 0,
  language              text,
  bio                   text,
  updated_at            timestamptz DEFAULT now(),
  CONSTRAINT tutor_id_fkey           FOREIGN KEY (id) REFERENCES auth.users(id)        ON DELETE CASCADE,
  CONSTRAINT tutor_user_profile_fkey FOREIGN KEY (id) REFERENCES app.user_profiles(id) ON DELETE CASCADE
);

-- [DOMAIN: Student & Tutor Identity] [OWNER: tauka-flutter] [SPEC: §5.1 Enrollment]
-- PURPOSE: Many-to-many enrollment junction: which students are enrolled in which courses.
CREATE TABLE app.student_course (
  student_id uuid REFERENCES app.student(id) ON DELETE CASCADE,
  course_id  uuid REFERENCES app.course(id)  ON DELETE CASCADE,
  level      bigint,
  PRIMARY KEY (student_id, course_id)
);

-- [DOMAIN: Student & Tutor Identity] [OWNER: tauka-flutter] [SPEC: §6 Tutor]
-- PURPOSE: Tutors can be associated with courses. NOTE: column named student_id stores tutor id (historical bug).
-- SEE: CONFLICT_LOG.md CONFLICT-012 — rename student_id → tutor_id is pending.
CREATE TABLE app.tutor_course (
  student_id     uuid REFERENCES app.tutor(id)  ON DELETE CASCADE,  -- misleading: stores tutor id
  course_id      uuid REFERENCES app.course(id) ON DELETE CASCADE,
  student_rating bigint,
  PRIMARY KEY (student_id, course_id)
);

-- [DOMAIN: Student & Tutor Identity] [OWNER: tauka-flutter] [SPEC: §6.2 Tutor Assignment]
-- PURPOSE: Admin-managed table that assigns a tutor to a course. Drives RLS for tutor read access.
-- §B3 adds class_id column.
CREATE TABLE app.tutor_assignment (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id    uuid REFERENCES app.tutor(id)   ON DELETE CASCADE,
  course_id   uuid REFERENCES app.course(id)  ON DELETE CASCADE,
  assigned_by uuid REFERENCES auth.users(id)  ON DELETE SET NULL,
  assigned_at timestamptz DEFAULT now(),
  deleted_at  timestamptz NULL,
  class_id    uuid REFERENCES app.classes(id),  -- added via §B3
  UNIQUE(tutor_id, course_id)
);

-- ============================================================
-- DOMAIN: Messaging & Classes (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter] [SPEC: §5.4 Chat, §2.5 Branch Contents]
CREATE TABLE app.classes (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  name         text NOT NULL,
  description  text NULL,
  created_at   timestamptz DEFAULT now(),
  created_by   uuid REFERENCES auth.users(id),
  deleted_at   timestamptz NULL,
  -- §07 additions
  health_score  smallint DEFAULT NULL CHECK (health_score BETWEEN 0 AND 100),
  health_status text DEFAULT NULL CHECK (health_status IN ('healthy','attention','intervention')),
  current_lesson integer DEFAULT 1,
  level         text
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
CREATE TABLE app.subclasses (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  class_id    uuid REFERENCES app.classes(id) ON DELETE CASCADE,
  name        text NOT NULL,
  description text NULL,
  start_date  timestamptz NOT NULL,
  end_date    timestamptz NOT NULL,
  deleted_at  timestamptz NULL,
  CHECK (end_date > start_date)
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
CREATE TABLE app.class_members (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  class_id    uuid REFERENCES app.classes(id) ON DELETE CASCADE,
  subclass_id uuid REFERENCES app.subclasses(id) ON DELETE SET NULL,
  user_id     uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('teacher','student')),
  joined_at   timestamptz DEFAULT now(),
  deleted_at  timestamptz NULL,
  UNIQUE(class_id, user_id)
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
CREATE TABLE app.conversations (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  type            text NOT NULL CHECK (type IN ('student_to_student','teacher_to_student','class_group','admin_broadcast')),
  class_id        uuid REFERENCES app.classes(id) ON DELETE SET NULL,
  created_at      timestamptz DEFAULT now(),
  last_message_at timestamptz,
  deleted_at      timestamptz NULL
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
-- INSERT via SECURITY DEFINER functions: get_or_create_student_conversation, upsert_conversation_participant
CREATE TABLE app.conversation_participants (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  conversation_id uuid REFERENCES app.conversations(id) ON DELETE CASCADE,
  user_id         uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  joined_at       timestamptz DEFAULT now(),
  deleted_at      timestamptz NULL,
  UNIQUE(conversation_id, user_id)
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
CREATE TABLE app.messages (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  conversation_id uuid REFERENCES app.conversations(id) ON DELETE CASCADE,
  sender_id       uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  content         text NOT NULL,
  created_at      timestamptz DEFAULT now(),
  deleted_at      timestamptz NULL
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
CREATE TABLE app.message_read_receipts (
  id         uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  message_id uuid REFERENCES app.messages(id) ON DELETE CASCADE,
  user_id    uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  read_at    timestamptz,
  UNIQUE(message_id, user_id)
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter] [SPEC: §15 Notification System]
-- §06 additions: notification_type, action_url, priority
CREATE TABLE app.notifications (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  subclass_id       uuid REFERENCES app.subclasses(id) ON DELETE CASCADE,
  sender_id         uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  title             text NOT NULL,
  content           text NOT NULL,
  created_at        timestamptz DEFAULT now(),
  is_urgent         boolean DEFAULT false,
  deleted_at        timestamptz NULL,
  notification_type text,
  action_url        text,
  priority          text DEFAULT 'normal' CHECK (priority IN ('low','normal','high','critical'))
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
CREATE TABLE app.notification_receipts (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  notification_id uuid REFERENCES app.notifications(id) ON DELETE CASCADE,
  user_id         uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  read_at         timestamptz,
  UNIQUE(notification_id, user_id)
);

-- [DOMAIN: Messaging & Classes] [OWNER: tauka-flutter]
-- Accessed ONLY via check_rate_limit() SECURITY DEFINER function. No direct RLS.
CREATE TABLE app.rate_limits (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  action_type  text    NOT NULL,
  action_count integer DEFAULT 1,
  window_start timestamptz DEFAULT now(),
  UNIQUE(user_id, action_type)
);

-- ============================================================
-- DOMAIN: Language Registry (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Language Registry] [OWNER: tauka-flutter] [SPEC: §1 Product Overview — language targeting]
-- NOTE: Not currently referenced by Dart services. Language data lives in user_profiles.known_languages JSONB.
CREATE TABLE app.languages (
  id         uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
  code       varchar(10)   NOT NULL UNIQUE,
  name       varchar(100)  NOT NULL,
  created_at timestamptz   DEFAULT now()
);

-- [DOMAIN: Language Registry] [OWNER: tauka-flutter]
CREATE TABLE app.user_languages (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  language_id         uuid REFERENCES app.languages(id) ON DELETE CASCADE,
  type                varchar(20) NOT NULL CHECK (type IN ('known','target')),
  proficiency         varchar(50),
  target_proficiency  varchar(50),
  current_proficiency varchar(50),
  is_primary          boolean DEFAULT false,
  learning_since      date,
  created_at          timestamptz DEFAULT now(),
  deleted_at          timestamptz NULL,
  UNIQUE(user_id, language_id, type)
);

-- ============================================================
-- DOMAIN: Course & Content (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §5.1 Learn Branch, §5.2 Dictionary]
CREATE TABLE app.course (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  title       text,
  speaker_for text,
  unit_count  bigint,
  dict_url    text[],   -- DEPRECATED: use app.dictionary.dict instead
  cover_url   text,
  toc_url     text,     -- partial Storage path to courses/<id>/toc.json
  deleted_at  timestamptz NULL
  -- dictionary_id added via §B10 (pending)
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §5.1 Unit Branch]
-- §B2 adds content_version, content_updated_at
CREATE TABLE app.unit (
  id               uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at       timestamptz NOT NULL DEFAULT now(),
  title            text,
  json_file        text,         -- partial Storage path for unit content JSON
  audio_folder     text,         -- partial Storage path prefix for audio files
  course_id        uuid    REFERENCES app.course(id),
  unit_order       integer DEFAULT 0,
  section_type     text    DEFAULT 'content',
  is_preliminary   boolean NOT NULL DEFAULT false,
  deleted_at       timestamptz NULL,
  content_version  integer DEFAULT 1,         -- added via §B2
  content_updated_at timestamptz              -- added via §B2
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §5.1 Unit Progress]
CREATE TABLE app.unit_progress (
  id                    uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id               uuid    NOT NULL REFERENCES app.unit(id) ON DELETE CASCADE,
  user_id               uuid    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  course_id             uuid    NOT NULL REFERENCES app.course(id) ON DELETE CASCADE,
  status                text    NOT NULL DEFAULT 'not_started'
                                CHECK (status IN ('not_started','in_progress','completed')),
  completion_percentage numeric(5,2) DEFAULT 0.0 CHECK (completion_percentage BETWEEN 0 AND 100),
  sections_completed    jsonb   DEFAULT '{}'::jsonb,
  started_at            timestamptz,
  completed_at          timestamptz,
  last_accessed_at      timestamptz DEFAULT now(),
  UNIQUE(unit_id, user_id)
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §5.1 Progress — LEGACY]
-- Superseded by unit_progress. Keep until all Dart code migrated.
CREATE TABLE app.progress (
  id         bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  updated_at timestamptz NOT NULL DEFAULT now(),
  status     boolean NOT NULL DEFAULT true,
  prog_file  text,
  inprogress boolean DEFAULT false,
  unit_id    uuid,
  user_id    uuid REFERENCES auth.users(id) ON UPDATE CASCADE ON DELETE CASCADE DEFAULT gen_random_uuid()
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §5.2 Dictionary Branch]
-- IMPORTANT: dictionary_entry table does NOT exist. Entries live in dict JSON column.
CREATE TABLE app.dictionary (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title       text NOT NULL,
  created_by  uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  main_lang   text NOT NULL,
  target_lang text NOT NULL,
  course_id   uuid NOT NULL REFERENCES app.course(id) ON DELETE CASCADE,
  dict        json NOT NULL,  -- array of DictionaryEntry: {id, word, translation, pronunciation, part_of_speech, example, audio_url}
  created_at  timestamptz DEFAULT now(),
  deleted_at  timestamptz NULL
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §5.3 Flashcards]
-- IMPORTANT: word_pair table does NOT exist. Cards live in deck JSON column.
CREATE TABLE app.anki_deck (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title      text NOT NULL,
  course_id  uuid REFERENCES app.course(id) ON DELETE CASCADE,
  user_id    uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  deck       json NOT NULL,  -- array of WordPair: {id, deck_id, main, reveal, difficulty, last_reviewed, next_review, review_count}
  created_at timestamptz DEFAULT now(),
  deleted_at timestamptz NULL
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §6 Tutor Features — Anki Assignment]
-- §B6: tutor assigns deck to student or class
CREATE TABLE app.anki_deck_assignment (
  id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  deck_id              uuid        NOT NULL REFERENCES app.anki_deck(id),
  assigned_to_user_id  uuid        REFERENCES app.user_profiles(id),
  assigned_to_class_id uuid        REFERENCES app.classes(id),
  assigned_by          uuid        NOT NULL REFERENCES app.user_profiles(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT anki_assignment_target CHECK (assigned_to_user_id IS NOT NULL OR assigned_to_class_id IS NOT NULL)
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §6.3 Course Corrections — content governance]
-- §B4: tutors flag content errors; admins approve/reject
CREATE TABLE app.course_correction (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id         uuid        NOT NULL REFERENCES app.unit(id),
  tutor_id        uuid        NOT NULL REFERENCES app.tutor(id),
  field_path      text        NOT NULL,
  current_value   text,
  suggested_value text        NOT NULL,
  note            text,
  status          text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  admin_note      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  reviewed_at     timestamptz,
  reviewed_by     uuid        REFERENCES app.user_profiles(id),
  deleted_at      timestamptz
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: §14.3 Content Versioning]
-- Tracks the published version of each course/unit/lesson for offline sync conflict detection.
CREATE TABLE app.content_versions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type    text NOT NULL CHECK (entity_type IN ('course','unit','lesson')),
  entity_id      uuid NOT NULL,
  version        int  NOT NULL DEFAULT 1,
  published_at   timestamptz DEFAULT now(),
  change_summary text,
  UNIQUE(entity_type, entity_id)
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: — not currently used]
CREATE TABLE app.program (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  course_id        uuid REFERENCES app.course(id) ON DELETE CASCADE,
  duration         integer,
  lessons_per_week integer,
  instructor_id    uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  start_date       timestamptz,
  end_date         timestamptz,
  created_at       timestamptz DEFAULT now(),
  deleted_at       timestamptz NULL,
  CHECK (end_date > start_date)
);

-- [DOMAIN: Course & Content] [OWNER: tauka-flutter] [SPEC: — not currently used; Explore uses yt_playlist]
CREATE TABLE app.contemporary (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title         text NOT NULL,
  type          text NOT NULL,
  created       timestamptz DEFAULT now(),
  created_by_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  content_url   text,
  thumbnail_url text,
  deleted_at    timestamptz NULL
);

-- ============================================================
-- DOMAIN: Social & Media (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] [SPEC: §5.5 Explore Branch]
-- WARNING D6: user_id has DEFAULT gen_random_uuid() — FK bug. Drop default: ALTER TABLE app.yt_playlist ALTER COLUMN user_id DROP DEFAULT;
-- §B5 adds status, submitted_by, reviewed_by, reviewed_at for tutor submission workflow
CREATE TABLE app.yt_playlist (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  user_id       uuid REFERENCES auth.users(id) DEFAULT gen_random_uuid(),  -- BUG: see D6 / CONFLICT-007
  admin_id      uuid REFERENCES app.admin_users(id) ON UPDATE CASCADE ON DELETE CASCADE,
  title         text,
  thumbnail_url text,
  -- §B5 additions
  status        text DEFAULT 'published' CHECK (status IN ('published','pending','rejected')),
  submitted_by  uuid REFERENCES app.user_profiles(id),
  reviewed_by   uuid REFERENCES app.user_profiles(id),
  reviewed_at   timestamptz
);

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] [SPEC: §5.5 Explore Branch]
-- WARNING D6: playlist_id has DEFAULT gen_random_uuid() — FK bug. Drop default.
CREATE TABLE app.yt_video (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  video_id      text,
  json_file_url text,
  playlist_id   uuid REFERENCES app.yt_playlist(id) ON UPDATE CASCADE ON DELETE CASCADE DEFAULT gen_random_uuid(),  -- BUG: see D6
  video_title   text,
  thumbnail_url text,
  views_count   text,
  channel_name  text,
  lyric_file    text
);

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] [SPEC: §5.5 Explore — lyric annotations]
CREATE TABLE app.lyrics (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_id       uuid REFERENCES app.yt_video(id) ON DELETE CASCADE,
  json_file_path text NOT NULL,
  title          text,
  artist         text,
  created_at     timestamptz DEFAULT now(),
  created_by     uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  deleted_at     timestamptz NULL
);

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] — legacy, not actively used
CREATE TABLE app.langexchange (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  lang_a     text,
  lang_b     text,
  user_a     text,
  user_b     text,
  deleted_at timestamptz NULL
);

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] — not used; no known business purpose
CREATE TABLE app.social (
  id           bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  created_at   timestamptz NOT NULL DEFAULT now(),
  account      text NOT NULL,
  user_name    text,
  permissioned boolean
);

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] — TikTok OAuth, currently disabled
CREATE TABLE app.tk_tokens (
  id                bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  created_at        timestamptz NOT NULL DEFAULT now(),
  auth_token        text,
  auth_toke_exp     timestamp without time zone,
  refresh_token     text,
  refresh_token_exp timestamp without time zone,
  user_id           uuid REFERENCES app.user_profiles(id) ON DELETE CASCADE
);

-- [DOMAIN: Social & Media] [OWNER: tauka-flutter] — TikTok video cache, currently disabled
CREATE TABLE app.tk_video (
  id          bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  created_at  timestamptz NOT NULL DEFAULT now(),
  video_id    text,
  creator_id  text,
  txt_file    text,
  feature_ref bigint
);

-- ============================================================
-- DOMAIN: Video (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Video] [OWNER: tauka-flutter] [SPEC: §8 Video Session System]
-- Exactly ONE row active (is_active=true) at any time. Client reads on startup to select SDK.
CREATE TABLE app.video_vendor_config (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor              text        NOT NULL DEFAULT 'daily' CHECK (vendor IN ('daily','agora','livekit')),
  is_active           boolean     NOT NULL DEFAULT true,
  api_key_secret_name text,
  metadata            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  region              text,
  fallback_vendor     text        CHECK (fallback_vendor IS NULL OR fallback_vendor IN ('daily','agora','livekit')),
  deleted_at          timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- [DOMAIN: Video] [OWNER: tauka-flutter] [SPEC: §8.1 Session Scheduling]
-- §B7: tutor-scheduled video session
-- USED BY: tauka-python (reads for monthly earnings calculation; no-show detection)
CREATE TABLE app.video_session (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id         uuid        NOT NULL REFERENCES app.tutor(id),
  title            text        NOT NULL,
  scheduled_at     timestamptz NOT NULL,
  duration_minutes integer     NOT NULL DEFAULT 60,
  video_call_url   text,
  notes            text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz
);

-- [DOMAIN: Video] [OWNER: tauka-flutter] [SPEC: §8.1 Session Participants]
CREATE TABLE app.video_session_participant (
  session_id uuid NOT NULL REFERENCES app.video_session(id) ON DELETE CASCADE,
  student_id uuid NOT NULL REFERENCES app.student(id),
  PRIMARY KEY (session_id, student_id)
);

-- [DOMAIN: Video] [OWNER: tauka-flutter] [SPEC: §8.3 Post-session rating]
CREATE TABLE app.video_session_rating (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id uuid     NOT NULL REFERENCES app.video_session(id) ON DELETE CASCADE,
  student_id uuid     NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  stars      smallint NOT NULL CHECK (stars BETWEEN 1 AND 5),
  feedback   text,
  created_at timestamptz DEFAULT now(),
  UNIQUE(session_id, student_id)
);

-- [DOMAIN: Video] [OWNER: tauka-flutter] [SPEC: §8.4 Tutor session notes]
CREATE TABLE app.video_session_note (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       uuid NOT NULL REFERENCES app.video_session(id) ON DELETE CASCADE,
  student_id       uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  tutor_id         uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  body             text NOT NULL DEFAULT '',
  lesson_completed boolean DEFAULT false,
  created_at       timestamptz DEFAULT now(),
  updated_at       timestamptz DEFAULT now(),
  UNIQUE(session_id, student_id)
);

-- [DOMAIN: Video] [OWNER: tauka-flutter] [SPEC: §8.5 Session recordings]
CREATE TABLE app.session_recordings (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id   uuid NOT NULL REFERENCES app.video_session(id) ON DELETE CASCADE,
  storage_path text NOT NULL,
  duration_s   int,
  created_at   timestamptz DEFAULT now()
);

-- [DOMAIN: Video] [OWNER: tauka-python] [SPEC: §6 Tutor — earnings source of truth]
-- Completed teaching sessions; distinct from video_session (scheduled) — tutor_sessions is the billing record.
-- USED BY: tauka-python (get_tutor_monthly_earnings RPC)
CREATE TABLE app.tutor_sessions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id         uuid NOT NULL REFERENCES auth.users(id),
  student_ids      uuid[],
  student_count    int  DEFAULT 1,
  session_type     text DEFAULT 'cohort',
  duration_minutes int,
  started_at       timestamptz DEFAULT now(),
  ended_at         timestamptz,
  topic            text,
  notes            text,
  created_at       timestamptz DEFAULT now()
);

-- ============================================================
-- DOMAIN: AI (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: AI] [OWNER: tauka-flutter] [SPEC: §5.4 Chat — AI Tutor tab]
-- COLUMN NAME: conversation_id (NOT ai_conversation_id — see CONFLICT-001)
CREATE TABLE app.ai_conversation (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title                   text NOT NULL,
  classic_conversation_id uuid REFERENCES app.conversations(id) ON DELETE SET NULL,
  created_at              timestamptz DEFAULT now(),
  last_message_at         timestamptz,
  deleted_at              timestamptz NULL
);

-- [DOMAIN: AI] [OWNER: tauka-flutter] [SPEC: §5.4 Chat — AI messages]
-- CANONICAL COLUMN: conversation_id (see CONFLICT-001 — never rename to ai_conversation_id)
CREATE TABLE app.ai_message (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES app.ai_conversation(id) ON DELETE CASCADE,
  role            text NOT NULL CHECK (role IN ('user','assistant')),
  content         text NOT NULL,
  shorthand_used  text,  -- '@meaning' | '@grammar' | '@check' | '@phrase' | '@how' | '@better' | '@compare' | '@word'
  raw_input       text,
  created_at      timestamptz DEFAULT now()
);

-- [DOMAIN: AI] [OWNER: tauka-flutter] [SPEC: §5.4 AI Practice — drill/pronunciation modes]
CREATE TABLE app.ai_practice_session (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  mode             text NOT NULL CHECK (mode IN ('conversation','drill','pronunciation')),
  lesson_number    integer,
  duration_seconds integer,
  turns_count      integer DEFAULT 0,
  error_count      integer DEFAULT 0,
  vocabulary_added integer DEFAULT 0,
  summary_text     text,
  created_at       timestamptz DEFAULT now(),
  completed_at     timestamptz,
  deleted_at       timestamptz
);

-- [DOMAIN: AI] [OWNER: tauka-flutter]
CREATE TABLE app.ai_practice_turn (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      uuid NOT NULL REFERENCES app.ai_practice_session(id) ON DELETE CASCADE,
  turn_index      integer NOT NULL,
  role            text NOT NULL CHECK (role IN ('user','assistant')),
  content         text NOT NULL,
  audio_url       text,
  grammar_error   boolean DEFAULT false,
  correction_text text,
  tokens_used     integer,
  created_at      timestamptz DEFAULT now()
);

-- ============================================================
-- DOMAIN: Exercises & Progress (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Exercises & Progress] [OWNER: tauka-flutter] [SPEC: §12.2 Exercise Tracking]
-- CANONICAL TABLE NAME: exercise_result (singular) — see CONFLICT-002
-- §28 adds lesson_id, started_at, completed_at
CREATE TABLE app.exercise_result (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  unit_id         uuid REFERENCES app.unit(id),
  lesson_id       uuid,                -- added via §28
  session_title   text,
  exercise_type   text NOT NULL,
  difficulty_mode text NOT NULL,
  score           int  NOT NULL,
  total           int  NOT NULL,
  elapsed_seconds int,
  answers         jsonb,
  submitted_at    timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,         -- added via §28
  completed_at    timestamptz,         -- added via §28
  synced_at       timestamptz
);

-- [DOMAIN: Exercises & Progress] [OWNER: tauka-flutter] [SPEC: §5.1 Handwriting Exercises]
-- §B9: handwriting exercise sessions
CREATE TABLE app.handwriting_session (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  unit_id         uuid REFERENCES app.unit(id),
  characters      jsonb NOT NULL,
  score           numeric(5,2),
  completed_at    timestamptz DEFAULT now()
);

-- [DOMAIN: Exercises & Progress] [OWNER: tauka-flutter] [SPEC: §5.3 Spaced Repetition]
-- Per-card SRS state for SM-2 algorithm (grade, ease_factor, interval, due_date)
CREATE TABLE app.flashcard_reviews (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid        NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  card_id       uuid        NOT NULL,
  deck_id       uuid,
  reviewed_at   timestamptz DEFAULT now() NOT NULL,
  grade         int         NOT NULL CHECK (grade BETWEEN 0 AND 5),
  interval_days int         NOT NULL DEFAULT 1,
  ease_factor   numeric(4,2) NOT NULL DEFAULT 2.50,
  due_date      date        NOT NULL,
  UNIQUE(user_id, card_id)
);

-- [DOMAIN: Exercises & Progress] [OWNER: tauka-flutter] [SPEC: §5.1 Pronunciation — voice recordings]
CREATE TABLE app.voice_recordings (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid        NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  lesson_id     uuid,
  storage_path  text        NOT NULL,
  duration_ms   int,
  phoneme_score numeric(4,2) CHECK (phoneme_score BETWEEN 0 AND 100),
  created_at    timestamptz DEFAULT now()
);

-- [DOMAIN: Exercises & Progress] [OWNER: tauka-flutter] [SPEC: §10 Cohort — progress snapshot for tutors]
CREATE TABLE app.progress_snapshots (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  course_id         uuid NOT NULL,
  lessons_completed int  NOT NULL DEFAULT 0,
  accuracy          numeric(5,4) CHECK (accuracy BETWEEN 0 AND 1),
  created_at        timestamptz DEFAULT now(),
  UNIQUE(user_id, course_id)
);

-- ============================================================
-- DOMAIN: Test & Referral (Owner: tauka-python; tauka-flutter reads own rows)
-- ============================================================

-- [DOMAIN: Test & Referral] [OWNER: tauka-python] [SPEC: §9 Subscription — placement test]
-- v4 question schema (2026-08-06, migration 2026-08-06_test_questions_v4.sql).
-- PURPOSE: question bank for the public placement test.
-- USED BY: tauka-python ONLY. tauka-flutter and tauka-react-web must never read
--          this table directly — it holds the answer key (see domain §6).
CREATE TABLE app.test_questions (
  -- Stable human-readable id ("a1_v_001"). PERMANENT: audio filenames
  -- (audio/<id>_m.mp3) and scored sessions key on it. Never renumber.
  id             text  PRIMARY KEY,
  language       text  NOT NULL,
  cefr_level     text  NOT NULL,          -- standard ladder only: A1 A2 B1 B2 C1 C2
  skill_area     text  NOT NULL,          -- reading | listening | vocabulary | grammar
  -- Interaction mode: choice | multi_choice | order | rank | match | bucket |
  -- build | select_token   (QUESTION_SCHEMA.md §4)
  question_type  text  NOT NULL,
  -- tag, prompt, stimulus, interaction  (QUESTION_SCHEMA.md §2)
  content        jsonb NOT NULL,
  -- Shape follows question_type: string (choice/select_token), array
  -- (multi_choice/order/rank/build), object (match/bucket). NEVER sent to client.
  correct_answer jsonb NOT NULL,
  source_unit    int,                     -- FSI unit introducing the feature
  -- REVIEWER-ONLY. Never shown to a test taker; stripped server-side alongside
  -- correct_answer. They let a reviewer confirm the answer is right and the
  -- distractors honest. distractor_rationale leaks the key by implication.
  explanation          text,
  distractor_rationale text,
  audio_url      text,                    -- legacy single-clip; v4 uses content.stimulus.audio {m,f}
  image_url      text,
  fsi_lesson_ref text,                    -- superseded by source_unit; retained, unused
  ai_generated   boolean DEFAULT true,
  human_reviewed boolean DEFAULT false,
  active         boolean DEFAULT true,
  flag_count     int     DEFAULT 0,
  created_at     timestamptz DEFAULT now(),
  updated_at     timestamptz DEFAULT now(),
  CONSTRAINT test_questions_source_unit_positive
    CHECK (source_unit IS NULL OR source_unit > 0)
);

-- Selection reads the full candidate pool per (level, skill) bucket.
CREATE INDEX IF NOT EXISTS idx_test_questions_selection
  ON app.test_questions (language, active, cefr_level, skill_area);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python] [SPEC: §9 Subscription — placement test session]
CREATE TABLE app.test_sessions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  language            text NOT NULL,
  status              text DEFAULT 'phase_1',
  email               text,
  name                text,
  referrer_student_id uuid REFERENCES auth.users(id),
  referral_code       text,
  question_ids        text[] NOT NULL,   -- matches test_questions.id (text)
  answers             jsonb  DEFAULT '{}',
  phase_1_score       jsonb,
  final_score         jsonb,
  cefr_result         text,
  adaptive_state      jsonb  DEFAULT '{}',
  started_at          timestamptz DEFAULT now(),
  phase_2_started_at  timestamptz,
  completed_at        timestamptz,
  ip_hash             text,
  user_agent          text,
  created_at          timestamptz DEFAULT now()
);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python]
CREATE TABLE app.test_share_events (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_session_id uuid NOT NULL REFERENCES app.test_sessions(id),
  channel         text NOT NULL,
  recipient_count int  DEFAULT 1,
  created_at      timestamptz DEFAULT now()
);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python] — tauka-flutter reads own rows
CREATE TABLE app.test_referrals (
  id                            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  referral_code                 text UNIQUE NOT NULL,
  sender_type                   text NOT NULL,
  sender_name                   text,
  sender_email                  text,
  sender_student_id             uuid REFERENCES auth.users(id),
  intent                        text,
  channel                       text,
  recipient_email               text,
  language                      text NOT NULL,
  link_opened                   boolean DEFAULT false,
  link_opened_at                timestamptz,
  test_started                  boolean DEFAULT false,
  test_session_id               uuid REFERENCES app.test_sessions(id),
  test_completed                boolean DEFAULT false,
  approved                      boolean DEFAULT false,
  sender_notified_on_completion boolean DEFAULT false,
  sender_notified_on_approval   boolean DEFAULT false,
  created_at                    timestamptz DEFAULT now()
);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python] — tauka-flutter reads own rows
CREATE TABLE app.test_supporters (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  supporter_email       text NOT NULL,
  supporter_name        text NOT NULL,
  student_id            uuid NOT NULL REFERENCES auth.users(id),
  test_session_id       uuid NOT NULL REFERENCES app.test_sessions(id),
  status                text DEFAULT 'approved',
  opted_in_at           timestamptz,
  student_visible       boolean DEFAULT true,
  last_notified_at      timestamptz,
  milestone_email_count int     DEFAULT 0,
  gift_nudge_shown      boolean DEFAULT false,
  gift_nudge_shown_at   timestamptz,
  engagement_score      int     DEFAULT 0,
  created_at            timestamptz DEFAULT now(),
  UNIQUE(supporter_email, student_id)
);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python] — tauka-flutter reads own rows
CREATE TABLE app.milestone_notifications (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id     uuid NOT NULL REFERENCES auth.users(id),
  supporter_id   uuid NOT NULL REFERENCES app.test_supporters(id),
  milestone_type text NOT NULL,
  milestone_data jsonb NOT NULL,
  status         text DEFAULT 'pending',
  sent_at        timestamptz,
  created_at     timestamptz DEFAULT now()
);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python] [SPEC: §9 Gift Subscription]
-- tauka-flutter reads own rows; active_gift_id FK on app.student references this table
CREATE TABLE app.gift_subscriptions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  supporter_id       uuid NOT NULL REFERENCES app.test_supporters(id),
  student_id         uuid NOT NULL REFERENCES auth.users(id),
  stripe_payment_id  text NOT NULL,
  stripe_receipt_url text,
  tier               text DEFAULT 'tutor',
  duration_months    int  NOT NULL,
  amount_cents       int  NOT NULL,
  currency           text DEFAULT 'usd',
  anonymous          boolean DEFAULT false,
  status             text DEFAULT 'pending',
  activated_at       timestamptz,
  expires_at         timestamptz,
  expiry_warned      boolean DEFAULT false,
  renewal_nudge_sent boolean DEFAULT false,
  refunded_at        timestamptz,
  created_at         timestamptz DEFAULT now()
);

-- [DOMAIN: Test & Referral] [OWNER: tauka-python]
CREATE TABLE app.testimonial_requests (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  supporter_id       uuid NOT NULL REFERENCES app.test_supporters(id),
  gift_id            uuid NOT NULL REFERENCES app.gift_subscriptions(id),
  status             text DEFAULT 'pending',
  sent_at            timestamptz,
  quote_text         text,
  display_name       text,
  display_preference text,
  approved_by_admin  boolean DEFAULT false,
  published_at       timestamptz,
  created_at         timestamptz DEFAULT now()
);

-- ============================================================
-- DOMAIN: Tutor Management (Owner: tauka-python; tauka-flutter writes availability)
-- ============================================================

-- [DOMAIN: Tutor Management] [OWNER: tauka-flutter] [SPEC: §6.2 Tutor Schedule]
-- FK references app.tutor(id) — not auth.users — enforces tutor-ness. [R-1]
CREATE TABLE app.tutor_availability (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id    uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  day_of_week int  NOT NULL,
  start_time  time NOT NULL,
  end_time    time NOT NULL,
  timezone    text NOT NULL DEFAULT 'UTC',
  active      boolean DEFAULT true,
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now(),
  UNIQUE(tutor_id, day_of_week, start_time)
);

-- [DOMAIN: Tutor Management] [OWNER: tauka-python] [SPEC: §6.4 Tutor Payout]
-- Stores encrypted payout details. Flutter reads only details_last4 for display.
CREATE TABLE app.tutor_payout_settings (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id          uuid UNIQUE NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  method            text NOT NULL,
  encrypted_details text NOT NULL,
  details_last4     text,
  verified          boolean DEFAULT false,
  created_at        timestamptz DEFAULT now(),
  updated_at        timestamptz DEFAULT now()
);

-- [DOMAIN: Tutor Management] [OWNER: tauka-python] [SPEC: §6.3 Payroll]
CREATE TABLE app.tutor_payroll (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id              uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE RESTRICT,
  period_start          date NOT NULL,
  period_end            date NOT NULL,
  group_sessions        integer DEFAULT 0,
  solo_sessions         integer DEFAULT 0,
  explore_contributions integer DEFAULT 0,
  base_amount           numeric(10,2) NOT NULL DEFAULT 0,
  performance_bonus     numeric(10,2) DEFAULT 0,
  total_amount          numeric(10,2) GENERATED ALWAYS AS (base_amount + COALESCE(performance_bonus, 0)) STORED,
  status                text DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','confirmed')),
  stripe_transfer_id    text,
  payment_method        text DEFAULT 'stripe' CHECK (payment_method IN ('stripe','mpesa','manual')),
  paid_at               timestamptz,
  approved_by           uuid REFERENCES app.user_profiles(id),
  created_at            timestamptz DEFAULT now(),
  UNIQUE(tutor_id, period_start, period_end)
);

-- [DOMAIN: Tutor Management] [OWNER: tauka-python]
CREATE TABLE app.payroll_line_item (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payroll_id   uuid NOT NULL REFERENCES app.tutor_payroll(id) ON DELETE CASCADE,
  item_type    text NOT NULL CHECK (item_type IN ('group_session','solo_session','explore_contribution','performance_bonus')),
  session_id   uuid REFERENCES app.video_session(id) ON DELETE SET NULL,
  reference_id uuid,
  description  text,
  amount       numeric(10,2) NOT NULL,
  created_at   timestamptz DEFAULT now()
);

-- [DOMAIN: Tutor Management] [OWNER: tauka-python]
CREATE TABLE app.tutor_rates (
  id             uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id       uuid     NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  session_type   text     NOT NULL CHECK (session_type IN ('group','solo','explore_contribution')),
  rate_amount    numeric(10,2) NOT NULL,
  currency       text     DEFAULT 'USD',
  effective_from date     DEFAULT current_date,
  effective_to   date,
  created_at     timestamptz DEFAULT now(),
  UNIQUE(tutor_id, session_type, effective_from)
);

-- ============================================================
-- DOMAIN: Cohort (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Cohort] [OWNER: tauka-flutter] [SPEC: §10 Cohort Management]
-- Intentionally separate from app.classes — cohorts are admin-managed groups spanning multiple class sections.
CREATE TABLE app.cohorts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL,
  description  text,
  tutor_id     uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  max_students int  DEFAULT 20,
  start_date   date,
  end_date     date,
  is_active    boolean DEFAULT true,
  created_at   timestamptz DEFAULT now()
);

-- [DOMAIN: Cohort] [OWNER: tauka-flutter]
CREATE TABLE app.cohort_memberships (
  cohort_id uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  user_id   uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  joined_at timestamptz DEFAULT now(),
  status    text DEFAULT 'active' CHECK (status IN ('active','paused','graduated','dropped')),
  PRIMARY KEY(cohort_id, user_id)
);

-- [DOMAIN: Cohort] [OWNER: tauka-flutter]
CREATE TABLE app.cohort_payments (
  id                       uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id                uuid    REFERENCES app.cohorts(id) ON DELETE SET NULL,
  student_id               uuid    NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  tutor_id                 uuid    REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  amount                   numeric(10,2) NOT NULL,
  currency                 text    DEFAULT 'USD',
  status                   text    DEFAULT 'pending' CHECK (status IN ('pending','paid','failed','refunded')),
  stripe_payment_intent_id text,
  paid_at                  timestamptz,
  created_at               timestamptz DEFAULT now()
);

-- [DOMAIN: Cohort] [OWNER: tauka-flutter]
CREATE TABLE app.cohort_assignments (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id   uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  assigned_by uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  deck_id     uuid,
  content_ref text,
  due_date    date,
  created_at  timestamptz DEFAULT now()
);

-- ============================================================
-- DOMAIN: Broadcast (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Broadcast] [OWNER: tauka-flutter] [SPEC: §11 Broadcast System]
CREATE TABLE app.broadcast (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  author_id    uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  content      text NOT NULL,
  attach_url   text,
  attach_type  text CHECK (attach_type IN ('image','audio','explore')),
  target_type  text NOT NULL DEFAULT 'program' CHECK (target_type IN ('program','level','cohort')),
  target_id    uuid,
  target_level text,
  is_pinned    boolean DEFAULT false,
  scheduled_at timestamptz,
  published_at timestamptz DEFAULT now(),
  created_at   timestamptz DEFAULT now(),
  deleted_at   timestamptz
);

-- [DOMAIN: Broadcast] [OWNER: tauka-flutter]
CREATE TABLE app.broadcast_reaction (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  broadcast_id uuid NOT NULL REFERENCES app.broadcast(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  emoji        text NOT NULL,
  created_at   timestamptz DEFAULT now(),
  UNIQUE(broadcast_id, user_id, emoji)
);

-- [DOMAIN: Broadcast] [OWNER: tauka-flutter]
CREATE TABLE app.broadcast_comment (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  broadcast_id uuid NOT NULL REFERENCES app.broadcast(id) ON DELETE CASCADE,
  author_id    uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  content      text NOT NULL,
  created_at   timestamptz DEFAULT now(),
  deleted_at   timestamptz
);

-- [DOMAIN: Broadcast] [OWNER: tauka-flutter]
CREATE TABLE app.broadcast_reads (
  user_id      uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  broadcast_id uuid NOT NULL REFERENCES app.broadcast(id) ON DELETE CASCADE,
  read_at      timestamptz DEFAULT now(),
  PRIMARY KEY(user_id, broadcast_id)
);

-- [DOMAIN: Broadcast] [OWNER: tauka-flutter] [SPEC: §11 Language Exchange]
-- rematch_available_at set automatically by trigger (matched_at + 14 days)
CREATE TABLE app.language_exchange_match (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_a_id            uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  user_b_id            uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  conversation_id      uuid REFERENCES app.conversations(id) ON DELETE SET NULL,
  matched_at           timestamptz DEFAULT now() NOT NULL,
  rematch_available_at timestamptz,
  status               text DEFAULT 'active' CHECK (status IN ('active','ended','rematch_requested')),
  ended_at             timestamptz,
  ended_by             uuid REFERENCES app.user_profiles(id),
  UNIQUE(user_a_id, user_b_id)
);

-- ============================================================
-- DOMAIN: Platform Config (Owner: tauka-python; tauka-flutter reads all)
-- ============================================================

-- [DOMAIN: Platform Config] [OWNER: tauka-python] [SPEC: §9 Tier Gating]
-- Feature flags control feature visibility before full rollout.
CREATE TABLE app.feature_flag (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key             text NOT NULL UNIQUE,
  status          text NOT NULL DEFAULT 'coming' CHECK (status IN ('ready','coming')),
  description     text,
  notify_me_count integer DEFAULT 0,
  updated_at      timestamptz DEFAULT now(),
  created_at      timestamptz DEFAULT now()
);

-- [DOMAIN: Platform Config] [OWNER: tauka-python] [SPEC: §9 Subscription & Tier Gating]
-- Tier values: free | learner | tutor | intensive
CREATE TABLE app.tier_gating_rules (
  feature     text PRIMARY KEY,
  min_tier    text NOT NULL CHECK (min_tier IN ('free','learner','tutor','intensive')),
  description text,
  updated_at  timestamptz DEFAULT now()
);

-- [DOMAIN: Platform Config] [OWNER: tauka-python] [SPEC: §20 Production Config]
CREATE TABLE app.config (
  key        text PRIMARY KEY,
  value      text NOT NULL,
  updated_at timestamptz DEFAULT now()
);

-- [DOMAIN: Platform Config] [OWNER: tauka-python] — service role write only; no client access
CREATE TABLE app.stripe_events (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stripe_event_id text UNIQUE NOT NULL,
  type            text NOT NULL,
  payload         jsonb,
  processed_at    timestamptz DEFAULT now()
);

-- [DOMAIN: Platform Config] [OWNER: tauka-flutter] [SPEC: §15 Push Notifications]
-- tauka-python reads tokens for FCM dispatch but never writes
CREATE TABLE app.push_tokens (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  token      text NOT NULL,
  platform   text CHECK (platform IN ('android','ios','web')),
  created_at timestamptz DEFAULT now(),
  UNIQUE(user_id, token)
);

-- [DOMAIN: Platform Config] [OWNER: tauka-python] [SPEC: §5.6 Achievements / XP]
CREATE TABLE app.achievements (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key         text UNIQUE NOT NULL,
  name        text NOT NULL,
  description text NOT NULL,
  icon_name   text NOT NULL DEFAULT 'star',
  xp_reward   int  NOT NULL DEFAULT 50,
  created_at  timestamptz DEFAULT now()
);

-- [DOMAIN: Platform Config] [OWNER: tauka-flutter]
CREATE TABLE app.user_achievements (
  user_id        uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  achievement_id uuid NOT NULL REFERENCES app.achievements(id)  ON DELETE CASCADE,
  earned_at      timestamptz DEFAULT now(),
  PRIMARY KEY(user_id, achievement_id)
);

-- ============================================================
-- DOMAIN: Content Governance (Owner: tauka-flutter)
-- ============================================================

-- [DOMAIN: Content Governance] [OWNER: tauka-flutter] [SPEC: §1 Content governance principle, §7 Admin]
-- Tutors submit new content; admins approve/reject before publishing.
CREATE TABLE app.content_approval_requests (
  id             uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
  contributor_id uuid    NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  content_type   text    NOT NULL CHECK (content_type IN ('lesson','unit','dictionary_entry','explore_content')),
  content_id     uuid,
  title          text    NOT NULL,
  description    text,
  payload        jsonb,
  status         text    DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','published')),
  reviewed_by    uuid    REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  reviewed_at    timestamptz,
  review_note    text,
  created_at     timestamptz DEFAULT now()
);

-- [DOMAIN: Content Governance] [OWNER: tauka-flutter] [SPEC: §5.2 Dictionary — community contributions]
CREATE TABLE app.dictionary_contributions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contributor_id   uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  word             text NOT NULL,
  transliteration  text,
  definition       text NOT NULL,
  example_sentence text,
  audio_path       text,
  language         text NOT NULL CHECK (language IN ('amharic','arabic')),
  status           text DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  reviewed_by      uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  reviewed_at      timestamptz,
  created_at       timestamptz DEFAULT now()
);

-- ============================================================
-- END OF SCHEMA
-- For RLS policies, indexes, triggers, SECURITY DEFINER functions, and initial seeds:
--   tauka_full_schema.sql  (single consolidated file — Parts A + B + §00 – §49)
-- ============================================================
