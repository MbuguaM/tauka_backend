-- =============================================================================
-- RECONCILABLE DEFINITIONS
-- SQL that adapts the EXISTING app-schema database to partially serve the
-- Python backend WITHOUT adding new tables or altering existing ones.
-- =============================================================================
--
-- PURPOSE
-- ─────────────────────────────────────────────────────────────────────────────
-- The Python backend (migration.sql) and the Supabase database (current database
-- state.sql) have a schema namespace mismatch:
--   • Python services call  db.table("name")   → hits PUBLIC schema
--   • Existing DB tables    app.tutor, app.user_profiles, etc. → in APP schema
--
-- Two migration.sql tables have PARTIAL equivalents in the existing app schema.
-- This file creates READ-ONLY views in the public schema so those services can
-- return real data while the full tables from missing_definitions.sql are being
-- applied and verified.
--
-- COVERAGE SUMMARY
-- ─────────────────────────────────────────────────────────────────────────────
-- Table                 Covered by existing DB?   What's missing
-- ─────────────────────────────────────────────────────────────────────────────
-- tutor_profiles        PARTIAL via app.tutor     per_session_rate_cents, bio,
--                       + app.user_profiles        updated_at
-- tutor_students        PARTIAL via               language, ended_at; semantics
--                       app.tutor_assignment      differ (course-scoped not direct)
--                       + app.student_course
-- All 12 other tables   NONE — see               Must create full tables from
-- in migration.sql      missing_definitions.sql    missing_definitions.sql
-- ─────────────────────────────────────────────────────────────────────────────
--
-- LIFECYCLE
-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Apply this file to unblock read paths while missing_definitions.sql is
--    being reviewed and applied.
-- 2. Apply missing_definitions.sql (create the real tables).
-- 3. Backfill tutor_profiles and tutor_students from app.tutor and
--    app.tutor_assignment data (see backfill section at the bottom).
-- 4. DROP the two views from this file — they are superseded by the real tables.
-- =============================================================================


-- =============================================================================
-- VIEW 1: public.tutor_profiles_view
-- Exposes existing app.tutor + app.user_profiles data via public schema.
--
-- Python service reads: tutor_service.get_earnings() → per_session_rate_cents
--                       tutor_service.get_assigned_students() → (no profile cols)
--
-- FIELDS AVAILABLE from existing DB:
--   id            ← app.tutor.id
--   created_at    ← app.tutor.created_at
--   rating        ← app.tutor.rating (bonus — not in migration.sql schema)
--   status        ← app.tutor.status (bonus)
--   first_name    ← app.user_profiles.first_name (bonus)
--   last_name     ← app.user_profiles.last_name (bonus)
--
-- FIELDS RETURNING NULL (require real tutor_profiles table):
--   per_session_rate_cents  — not stored anywhere in DB
--   language                — not stored in app.tutor; known_languages jsonb
--                             in app.user_profiles is for the tutor's OWN
--                             languages, not the language they teach
--   bio                     — not stored anywhere in DB
--   updated_at              — app.tutor has no updated_at column
-- =============================================================================

CREATE OR REPLACE VIEW public.tutor_profiles_view AS
SELECT
    t.id,
    NULL::int              AS per_session_rate_cents,   -- not in DB
    NULL::text             AS language,                 -- not in DB
    NULL::text             AS bio,                      -- not in DB
    t.created_at,
    NULL::timestamptz      AS updated_at,               -- not in DB
    -- bonus fields for debugging; not in migration.sql schema
    t.rating,
    t.status,
    p.first_name,
    p.last_name
FROM app.tutor t
LEFT JOIN app.user_profiles p ON p.id = t.id
WHERE t.deleted_at IS NULL;

COMMENT ON VIEW public.tutor_profiles_view IS
    'Interim read-only view over app.tutor + app.user_profiles. '
    'per_session_rate_cents, language, and bio are NULL until tutor_profiles '
    'table is created from missing_definitions.sql. Drop this view once '
    'the real table exists.';


-- =============================================================================
-- VIEW 2: public.tutor_students_view
-- Derives tutor-student relationships from the existing course-based tables.
--
-- Python service reads: tutor_service.get_assigned_students()
--                          → .eq("tutor_id", ...).eq("active", True)
--                       tutor_service.get_student_detail()
--                          → .eq("tutor_id", ...).eq("student_id", ...).eq("active", True)
--
-- HOW IT WORKS:
--   app.tutor_assignment links tutors to courses (one row per tutor+course).
--   app.student_course   links students to courses (one row per student+course).
--   Joining both on course_id gives implicit tutor↔student pairs.
--   DISTINCT eliminates duplicate pairs where a student takes multiple courses
--   with the same tutor.
--
-- FIELDS AVAILABLE from existing DB:
--   tutor_id    ← app.tutor_assignment.tutor_id
--   student_id  ← app.student_course.student_id
--   started_at  ← app.tutor_assignment.assigned_at (closest semantic match)
--   active      ← derived: true when tutor_assignment.deleted_at IS NULL
--
-- FIELDS RETURNING NULL (require real tutor_students table):
--   id          — no surrogate key on the join
--   language    — not captured in course-based tables
--   ended_at    — app schema only has soft-delete (deleted_at), no ended_at
-- =============================================================================

CREATE OR REPLACE VIEW public.tutor_students_view AS
SELECT DISTINCT ON (ta.tutor_id, sc.student_id)
    NULL::uuid             AS id,                       -- no row identity on join
    ta.tutor_id,
    sc.student_id,
    NULL::text             AS language,                 -- not captured in DB
    (ta.deleted_at IS NULL) AS active,
    ta.assigned_at         AS started_at,
    NULL::timestamptz      AS ended_at                  -- not captured in DB
FROM app.tutor_assignment ta
JOIN app.student_course sc ON sc.course_id = ta.course_id
WHERE ta.deleted_at IS NULL;

COMMENT ON VIEW public.tutor_students_view IS
    'Interim read-only view deriving tutor-student pairs from '
    'app.tutor_assignment + app.student_course. language and ended_at are NULL. '
    'This view returns one row per (tutor, student) pair regardless of how many '
    'courses they share. Drop once tutor_students table is created from '
    'missing_definitions.sql.';


-- =============================================================================
-- SCHEMA MISMATCH REFERENCE
-- The tables below EXIST in the app schema but are queried by name in Python
-- services that target public schema (db.table("name") with no .schema("app")).
-- If any Python service is updated to use supabase.schema("app").table(...),
-- it gains access to these tables without any DB changes.
-- =============================================================================

-- ┌─────────────────────────────────┬──────────────────────────────────────┐
-- │ Python service call             │ Existing app-schema table            │
-- ├─────────────────────────────────┼──────────────────────────────────────┤
-- │ db.table("progress")            │ app.progress  (legacy, deprecated)   │
-- │ db.table("unit_progress")       │ app.unit_progress                    │
-- │ db.table("ai_conversation")     │ app.ai_conversation                  │
-- │ db.table("ai_message")          │ app.ai_message                       │
-- │ db.table("messages")            │ app.messages                         │
-- │ db.table("conversations")       │ app.conversations                    │
-- │ db.table("course")              │ app.course                           │
-- │ db.table("unit")                │ app.unit                             │
-- │ db.table("anki_deck")           │ app.anki_deck                        │
-- │ db.table("dictionary")          │ app.dictionary                       │
-- │ db.table("yt_playlist")         │ app.yt_playlist                      │
-- │ db.table("yt_video")            │ app.yt_video                         │
-- │ db.table("lyrics")              │ app.lyrics                           │
-- │ db.table("rate_limits")         │ app.rate_limits                      │
-- └─────────────────────────────────┴──────────────────────────────────────┘
--
-- TO RECONCILE WITHOUT DB CHANGES: update every Python service that queries
-- these tables to use:
--
--   supabase_admin.schema("app").table("table_name").select(...)
--
-- instead of:
--
--   supabase_admin.table("table_name").select(...)
--
-- No migration needed; no new tables needed. This is a Python code change only.


-- =============================================================================
-- BACKFILL QUERIES (run AFTER missing_definitions.sql is applied)
-- Seed tutor_profiles and tutor_students from existing app-schema data.
-- =============================================================================

-- Backfill tutor_profiles for all active tutors.
-- per_session_rate_cents, language, bio will be 0 / NULL until tutors
-- fill in their profile via the tutor portal.
--
-- INSERT INTO public.tutor_profiles (id, created_at)
-- SELECT t.id, t.created_at
-- FROM app.tutor t
-- WHERE t.deleted_at IS NULL
-- ON CONFLICT (id) DO NOTHING;

-- Backfill tutor_students from course-based assignments.
-- Run this once; tutors can then correct language and dates via the portal.
--
-- INSERT INTO public.tutor_students (tutor_id, student_id, active, started_at)
-- SELECT DISTINCT
--     ta.tutor_id,
--     sc.student_id,
--     (ta.deleted_at IS NULL),
--     ta.assigned_at
-- FROM app.tutor_assignment ta
-- JOIN app.student_course sc ON sc.course_id = ta.course_id
-- ON CONFLICT (tutor_id, student_id) DO NOTHING;
