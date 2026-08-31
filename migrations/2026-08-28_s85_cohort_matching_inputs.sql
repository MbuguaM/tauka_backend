-- ============================================================================
-- §85  PHASE 1 — the inputs the cohort matcher reads
--      Plan: tutor-cohort-matching-plan.md §5.3, §5.4, Phase 1. Backlog O-16.
--      DOMAIN: cohort_matching (NEW — approved as D-16, 2026-08-27).
--      OWNER:  tauka-python. Two columns here belong to other domains and are
--              marked as such: app.user_profiles.timezone (identity_access,
--              tauka-flutter) and app.unit.level (course_content,
--              tauka-flutter).
-- ============================================================================
--
-- WHY THIS IS PHASE 1 AND NOT PHASE 3. The algorithm in plan §3 reads eleven
-- student and tutor attributes and **seven of them have nowhere to live**
-- (plan §2). Two of the four hard constraints — schedule overlap and the ±2h
-- cohort-mate band — are timezone arithmetic, and a student's timezone is not
-- recorded anywhere machine-readable: `user_profiles.location` is free text and
-- `onboarding_location` is jsonb of unknown shape. Nothing downstream can be
-- right until it is. So the data lands first, the capture UI second, and the
-- matcher only once it has something to read.
--
-- NONE OF THESE TABLES EXIST AND NONE HAS EVER HELD A ROW, so there is no
-- backfill and no compatibility burden — the same reason §65 could correct the
-- cohort schema rather than patch it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 85.1  app.user_profiles.timezone  — DOMAIN identity_access, OWNER tauka-flutter
-- ---------------------------------------------------------------------------
-- ONE ZONE PER PERSON, AND THIS IS A DELIBERATE DEVIATION FROM THE PLAN.
--
-- Plan §5.3 specified `student_time_preference` as
-- `(student_id, day_of_week, start_time, timezone)`, mirroring
-- `app.tutor_availability` so "one overlap function serves both sides". The
-- mirror is kept for day/time; **the per-row timezone is not**, and the column
-- lives here instead.
--
-- Two reasons. First, a per-row zone is two sources of truth for "where is this
-- student", and the group constraints (offset spread <= tz_band, local start
-- inside civil_hours) need exactly one answer per person — the same shape of
-- defect as the two streak columns (CONFLICT-032). Second, it is the correct
-- behaviour on a move: a student who relocates changes one field and all their
-- windows travel with them, because "I am free at 19:00 my time" is a statement
-- about their evening, not about a fixed UTC instant.
--
-- The tutor side legitimately differs and keeps its per-row zone: a tutor may
-- enter one window as 09:00 Nairobi and another as 09:00 London. The overlap
-- function still serves both — it takes rows plus a zone resolver, and the
-- resolver is a column on one side and a profile lookup on the other.
--
-- IANA ZONE NAMES ONLY, NEVER A FIXED OFFSET (plan §3 Stage 0). An intake on
-- the 1st placing a cohort that starts after a DST change would otherwise slip
-- by an hour for half its members. The CHECK is a shape test; the real registry
-- lookup is app.is_valid_timezone() below, called before a write.
ALTER TABLE app.user_profiles
  ADD COLUMN IF NOT EXISTS timezone text;

ALTER TABLE app.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_timezone_check;
ALTER TABLE app.user_profiles
  ADD CONSTRAINT user_profiles_timezone_check
  CHECK (timezone IS NULL OR timezone ~ '^[A-Za-z][A-Za-z0-9+_-]*(/[A-Za-z0-9+._-]+){1,2}$');

COMMENT ON COLUMN app.user_profiles.timezone IS
  'IANA zone name (e.g. Africa/Nairobi), never a fixed offset. The single source of truth for where this person is; app.student_time_preference rows are expressed in it. NULL means not captured, which defers a student with NO_TIMEZONE rather than guessing.';

-- A real validity check, callable from the API before writing.
-- `pg_timezone_names` is the registry Postgres itself uses for `AT TIME ZONE`,
-- so a name that passes here is a name the session generator can convert with.
CREATE OR REPLACE FUNCTION app.is_valid_timezone(p_zone text)
 RETURNS boolean
 LANGUAGE sql
 STABLE
AS $fn$
  SELECT p_zone IS NOT NULL
     AND EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = p_zone);
$fn$;

REVOKE EXECUTE ON FUNCTION app.is_valid_timezone(text) FROM public;
GRANT  EXECUTE ON FUNCTION app.is_valid_timezone(text) TO authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 85.2  app.student_time_preference — the student half of the overlap test
-- ---------------------------------------------------------------------------
-- Plan §3 Stage 1: "the slot intersects one of the student's >=3 preferred
-- times". An INTERVAL, so `end_time` is not optional — a start alone cannot be
-- intersected with a 50-minute slot without inventing a length.
--
-- `preference_rank` is what plan §3 Stage 3's slot-preference term scores:
-- "+10 x mean(1 - (rank - 1)/(ranks - 1)) — honouring first choices beats
-- honouring third". Without it every window is equally good and the term is a
-- constant.
--
-- THE "AT LEAST THREE" RULE IS NOT IN THE SCHEMA, deliberately, and this is the
-- same reasoning `matching_service.validate_link_set` records for the guest
-- pool: a CHECK cannot count sibling rows, and a trigger that refuses the first
-- insert of a set makes the table unwritable in the only order a client can
-- write it. It is enforced in the capture UI and reported by the matcher as the
-- deferral reason TOO_FEW_PREFERENCES.
CREATE TABLE IF NOT EXISTS app.student_time_preference (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id       uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  day_of_week      int  NOT NULL,
  start_time       time NOT NULL,
  end_time         time NOT NULL,
  preference_rank  int  NOT NULL DEFAULT 1,
  active           boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT stp_dow_check   CHECK (day_of_week BETWEEN 0 AND 6),
  CONSTRAINT stp_rank_check  CHECK (preference_rank BETWEEN 1 AND 10),
  -- No midnight wrap. A window that crosses midnight is two windows, and
  -- letting one row mean two makes every intersection test conditional.
  CONSTRAINT stp_window_check CHECK (end_time > start_time),
  -- A window shorter than the shortest session product cannot contain one.
  CONSTRAINT stp_window_length_check CHECK (
    (end_time - start_time) >= interval '25 minutes'),
  CONSTRAINT stp_no_duplicate UNIQUE (student_id, day_of_week, start_time)
);

COMMENT ON TABLE app.student_time_preference IS
  'When a student can attend a live session, in their own profile timezone. Mirrors app.tutor_availability in shape so one overlap function serves both sides; the timezone lives on app.user_profiles.timezone rather than per row - see migration 85.1.';
COMMENT ON COLUMN app.student_time_preference.preference_rank IS
  '1 = first choice. Scored by plan section 3 Stage 3: honouring a first choice beats honouring a third. Ranks need not be dense or unique.';
COMMENT ON COLUMN app.student_time_preference.active IS
  'Soft toggle so a student can park a window without losing it. The matcher reads active rows only.';

CREATE INDEX IF NOT EXISTS idx_stp_student
  ON app.student_time_preference (student_id) WHERE active;
-- The intake pool query: every active window, grouped by day. A small table,
-- but the matcher reads all of it every cycle.
CREATE INDEX IF NOT EXISTS idx_stp_day
  ON app.student_time_preference (day_of_week, start_time) WHERE active;

DROP TRIGGER IF EXISTS trg_stp_updated_at ON app.student_time_preference;
CREATE TRIGGER trg_stp_updated_at BEFORE UPDATE ON app.student_time_preference
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE app.student_time_preference ENABLE ROW LEVEL SECURITY;

-- The student's own answer about their own week. Nobody else's business:
-- deliberately NO tutor read policy. A tutor needs to know when the cohort
-- meets, which is `app.cohorts.slot_*` — not the twelve windows a student
-- offered before one of them was chosen.
DROP POLICY IF EXISTS stp_select_own ON app.student_time_preference;
CREATE POLICY stp_select_own ON app.student_time_preference
  FOR SELECT USING (student_id = auth.uid() OR app.is_admin());

DROP POLICY IF EXISTS stp_write_own ON app.student_time_preference;
CREATE POLICY stp_write_own ON app.student_time_preference
  FOR ALL USING (student_id = auth.uid() OR app.is_admin())
  WITH CHECK (student_id = auth.uid() OR app.is_admin());

-- ---------------------------------------------------------------------------
-- 85.3  app.tutor_teaching_profile — the tutor half
-- ---------------------------------------------------------------------------
-- SEPARATE FROM app.tutor ON PURPOSE (plan §5.3): `app.tutor` is a billing and
-- identity record — rate, status, payout linkage — and every column added there
-- would be another reason for a payout query to read a matching document.
-- One row per tutor; absence means "not certified to teach a cohort yet", which
-- is the honest default for a tutor who has only ever taught 1-on-1.
--
-- `min_level`/`max_level` is plan §3 Stage 1's proficiency-band hard constraint,
-- and it is a certification rather than an aspiration. `max_concurrent_cohorts`
-- is the capacity check in the same stage: no TutorSlot is created for a tutor
-- already at their limit.
CREATE TABLE IF NOT EXISTS app.tutor_teaching_profile (
  tutor_id               uuid PRIMARY KEY REFERENCES app.tutor(id) ON DELETE CASCADE,
  min_level              int  NOT NULL DEFAULT 1,
  max_level              int  NOT NULL DEFAULT 1,
  specialisations        text[] NOT NULL DEFAULT '{}',
  teaching_style         text,
  native_language        text,
  years_experience       int,
  max_concurrent_cohorts int  NOT NULL DEFAULT 3,
  intro_clip_path        text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ttp_level_range_check CHECK (
    min_level BETWEEN 1 AND 4 AND max_level BETWEEN 1 AND 4 AND max_level >= min_level),
  CONSTRAINT ttp_experience_check CHECK (
    years_experience IS NULL OR years_experience BETWEEN 0 AND 70),
  -- Zero is a real and useful value: it takes a tutor out of cohort matching
  -- without deleting their certification or their 1-on-1 availability.
  CONSTRAINT ttp_capacity_check CHECK (max_concurrent_cohorts BETWEEN 0 AND 20)
);

COMMENT ON TABLE app.tutor_teaching_profile IS
  'What the cohort matcher needs to know about a tutor. Kept out of app.tutor so a billing/identity record does not become a matching document. No row = not certified for cohort teaching.';
COMMENT ON COLUMN app.tutor_teaching_profile.native_language IS
  'The tutor L1 - the language they SPEAK. app.tutor.language is the language they TEACH; the two are different questions, and conflating them is why plan section 2 lists this as missing.';
COMMENT ON COLUMN app.tutor_teaching_profile.max_concurrent_cohorts IS
  'Capacity for plan section 3 Stage 1. 0 removes the tutor from cohort matching without touching their certification or 1-on-1 availability.';
COMMENT ON COLUMN app.tutor_teaching_profile.intro_clip_path IS
  'Storage path of the 30-second intro shown in an offer. NOT a URL: the object is served by a short-lived signed URL, same as session recordings.';

DROP TRIGGER IF EXISTS trg_ttp_updated_at ON app.tutor_teaching_profile;
CREATE TRIGGER trg_ttp_updated_at BEFORE UPDATE ON app.tutor_teaching_profile
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE app.tutor_teaching_profile ENABLE ROW LEVEL SECURITY;

-- NO BLANKET `authenticated` SELECT, and this is the same call §73.8 made when
-- it narrowed `cohorts_select_all_auth`: a table carrying every tutor's
-- specialisations, style and capacity is a browsable directory of who teaches
-- what. The offer screen genuinely needs some of these fields, and it gets them
-- the way `matching_service._tutor_display` already serves a roster — assembled
-- by tauka-python for the one student being offered, not by opening the table.
DROP POLICY IF EXISTS ttp_select_own ON app.tutor_teaching_profile;
CREATE POLICY ttp_select_own ON app.tutor_teaching_profile
  FOR SELECT USING (tutor_id = auth.uid() OR app.is_admin());

-- A tutor maintains their own style, specialisations and capacity. The
-- CERTIFIED LEVEL RANGE is admin-only: a tutor certifying themselves for level
-- 4 is the same self-declaration hole as a client choosing its own user_type
-- (root CLAUDE.md; backlog O-36). RLS cannot express "these columns only", so
-- the split is enforced by the trigger below.
DROP POLICY IF EXISTS ttp_write_own ON app.tutor_teaching_profile;
CREATE POLICY ttp_write_own ON app.tutor_teaching_profile
  FOR ALL USING (tutor_id = auth.uid() OR app.is_admin())
  WITH CHECK (tutor_id = auth.uid() OR app.is_admin());

CREATE OR REPLACE FUNCTION app.ttp_guard_certification()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $fn$
BEGIN
  -- service_role writes (an admin script, a seeding job) bypass RLS entirely
  -- and carry no auth.uid(); they are not the subject this guard is about.
  IF auth.uid() IS NULL OR app.is_admin() THEN
    RETURN NEW;
  END IF;

  IF TG_OP = 'INSERT' THEN
    -- A tutor may create their own row, but only at the floor. Anything above
    -- level 1 is a certification, and an admin grants it.
    IF NEW.min_level <> 1 OR NEW.max_level <> 1 THEN
      RAISE EXCEPTION 'certified level range is set by an admin, not by the tutor'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.min_level IS DISTINCT FROM OLD.min_level
       OR NEW.max_level IS DISTINCT FROM OLD.max_level THEN
      RAISE EXCEPTION 'certified level range is set by an admin, not by the tutor'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;

  RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_ttp_guard_certification ON app.tutor_teaching_profile;
CREATE TRIGGER trg_ttp_guard_certification
  BEFORE INSERT OR UPDATE ON app.tutor_teaching_profile
  FOR EACH ROW EXECUTE FUNCTION app.ttp_guard_certification();

-- ---------------------------------------------------------------------------
-- 85.4  app.unit.level — DOMAIN course_content, OWNER tauka-flutter
-- ---------------------------------------------------------------------------
-- Plan §5.4: four levels of fifteen lessons. STORED rather than derived from
-- `ceil(unit_order/15)` so a future re-cut of the level boundaries does not
-- silently re-band every existing cohort — a cohort's `level` is a promise to
-- four people about who they are studying with.
--
-- PRELIMINARY UNITS GET NULL, NOT LEVEL 1. `app.unit.unit_order` is NOT unique:
-- production carries `introduction` and `alphabet` at orders 1 and 2, colliding
-- with content units 1 and 2 (ARCHITECTURE_INDEX global anti-patterns). Any
-- computation over unit_order must filter `is_preliminary = false` or it counts
-- the first two units twice, and that includes this backfill.
ALTER TABLE app.unit
  ADD COLUMN IF NOT EXISTS level int;

ALTER TABLE app.unit DROP CONSTRAINT IF EXISTS unit_level_check;
ALTER TABLE app.unit
  ADD CONSTRAINT unit_level_check CHECK (level IS NULL OR level BETWEEN 1 AND 4);

UPDATE app.unit
   SET level = LEAST(4, GREATEST(1, ceil(unit_order / 15.0)::int))
 WHERE is_preliminary = false
   AND unit_order IS NOT NULL
   AND deleted_at IS NULL
   AND level IS DISTINCT FROM LEAST(4, GREATEST(1, ceil(unit_order / 15.0)::int));

COMMENT ON COLUMN app.unit.level IS
  'Which of the four 15-lesson levels this unit belongs to. NULL for preliminary units (introduction, alphabet), which sit outside the level structure and share unit_order values with content units.';

COMMIT;

-- ---------------------------------------------------------------------------
-- 85.5  app.user_profiles.learning_goal — DOMAIN identity_access, OWNER tauka-flutter
--       APPLIED separately as migration `s85_5_learning_goal`.
-- ---------------------------------------------------------------------------
-- Plan section 3 Stage 3's HIGHEST-weighted soft term is "+30 if the tutor's
-- specialisations include the student's learning goal", and plan section 2's
-- table of missing inputs omits it: it lists the tutor's specialisations as
-- absent but assumes the student's goal exists. It does not. `user_profiles`
-- carries `study_method` (the style preference, which section 2 does name) and
-- nothing about why the student is learning.
--
-- Left unbuilt, the term scores 0 for everyone and the top weight becomes a
-- constant - quietly inverting the ordering plan section 3 argues for, where
-- goal alignment must outrank tier cohesion so that "a cohort of four Intensive
-- students with mismatched learning goals is not a better outcome than a
-- mixed-tier cohort that wants the same thing".
--
-- FREE TEXT, matched against `tutor_teaching_profile.specialisations`, with the
-- vocabulary owned by the capture UI rather than a CHECK - the same call
-- `exercise_result.exercise_type` makes, so a seventh goal needs no migration.
ALTER TABLE app.user_profiles
  ADD COLUMN IF NOT EXISTS learning_goal text;

COMMENT ON COLUMN app.user_profiles.learning_goal IS
  'Why this student is learning - matched against app.tutor_teaching_profile.specialisations for the +30 goal-alignment term. Vocabulary is owned by the capture UI (conversation, family, travel, heritage, business, exam), not by a CHECK. NULL scores 0 on that term, never a penalty.';
