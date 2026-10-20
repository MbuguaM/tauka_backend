-- ============================================================================
-- §104 (2026-10-18)  The slot catalogue and learners' slot choices (plan 3.1, E-12)
-- ============================================================================
--
-- cohort_matching domain (tauka-python owns the DDL and every write).
--
-- Playbook §Scheduling: "Learners choose from a published slot catalogue rather
-- than naming exact times." Four bands, never mixed; every slot anchored in the
-- band's local time (the tutor side absorbs clock changes). A learner ticks
-- every slot they could make and stars favourites; three or more earns the
-- matching guarantee.
--
-- ONE ANCHOR ZONE PER BAND (CHECK below). The matcher turns a chosen slot into
-- an exact weekly window in that zone, and pods never mix bands, so every
-- learner who chose slots in a band shares one zone. Moving a band's anchor is
-- a migration, not a data edit.
--
-- Seeded from the playbook's EXAMPLE catalogue; "the first two or three cohorts
-- set the real catalogue". "Weekday 13:00 lunchtime" in East Africa is seeded as
-- Wednesday 13:00 — one slot, until demand says which days.

CREATE TABLE IF NOT EXISTS app.slot_catalogue (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  band            text NOT NULL CHECK (band IN ('americas','europe','ea_gulf','asia')),
  anchor_timezone text NOT NULL,
  -- 0 = Monday .. 6 = Sunday (FC-037), in anchor_timezone.
  day_of_week     int  NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
  start_time      time NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('weekend','weekday')),
  active          boolean NOT NULL DEFAULT true,
  sort_order      int NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT slot_catalogue_one_anchor_per_band CHECK (
       (band = 'americas' AND anchor_timezone = 'America/New_York')
    OR (band = 'europe'   AND anchor_timezone = 'Europe/London')
    OR (band = 'ea_gulf'  AND anchor_timezone = 'Africa/Nairobi')
    OR (band = 'asia'     AND anchor_timezone = 'Asia/Shanghai')),
  CONSTRAINT slot_catalogue_unique UNIQUE (band, day_of_week, start_time)
);

CREATE TABLE IF NOT EXISTS app.student_slot_choice (
  student_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  -- RESTRICT: retire a slot with active = false; deleting it would silently
  -- shrink learners' choices under them.
  slot_id    uuid NOT NULL REFERENCES app.slot_catalogue(id) ON DELETE RESTRICT,
  favourite  boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, slot_id)
);
CREATE INDEX IF NOT EXISTS idx_student_slot_choice_slot ON app.student_slot_choice (slot_id);

DROP TRIGGER IF EXISTS trg_slot_catalogue_updated_at ON app.slot_catalogue;
CREATE TRIGGER trg_slot_catalogue_updated_at BEFORE UPDATE ON app.slot_catalogue
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE app.slot_catalogue      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.student_slot_choice ENABLE ROW LEVEL SECURITY;
-- Public: the website may show the catalogue to visitors.
DROP POLICY IF EXISTS slot_catalogue_public_read ON app.slot_catalogue;
CREATE POLICY slot_catalogue_public_read ON app.slot_catalogue FOR SELECT TO anon, authenticated USING (true);
DROP POLICY IF EXISTS student_slot_choice_own ON app.student_slot_choice;
CREATE POLICY student_slot_choice_own ON app.student_slot_choice
  FOR SELECT TO authenticated USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.slot_catalogue, app.student_slot_choice FROM anon, authenticated;
GRANT SELECT ON app.slot_catalogue TO anon, authenticated;
GRANT SELECT ON app.student_slot_choice TO authenticated;
GRANT ALL ON app.slot_catalogue, app.student_slot_choice TO service_role;

COMMENT ON TABLE app.slot_catalogue IS
  'The published pod slots (plan 3.1, playbook §Scheduling): band, weekday and time in the '
  'band''s anchor zone. Learners choose from these. tauka-python writes; public read.';
COMMENT ON TABLE app.student_slot_choice IS
  'The slots a learner could make, favourites starred (plan 3.1). Three or more earns the '
  'matching guarantee. Written by tauka-python (POST /enrolment/slots); the learner reads own.';

INSERT INTO app.slot_catalogue (band, anchor_timezone, day_of_week, start_time, kind, sort_order) VALUES
  ('americas', 'America/New_York', 5, '10:00', 'weekend', 1),
  ('americas', 'America/New_York', 5, '13:00', 'weekend', 2),
  ('americas', 'America/New_York', 1, '19:00', 'weekday', 3),
  ('americas', 'America/New_York', 3, '19:00', 'weekday', 4),
  ('europe',   'Europe/London',    5, '10:00', 'weekend', 1),
  ('europe',   'Europe/London',    6, '14:00', 'weekend', 2),
  ('europe',   'Europe/London',    1, '19:00', 'weekday', 3),
  ('europe',   'Europe/London',    3, '19:00', 'weekday', 4),
  ('ea_gulf',  'Africa/Nairobi',   5, '10:00', 'weekend', 1),
  ('ea_gulf',  'Africa/Nairobi',   6, '16:00', 'weekend', 2),
  ('ea_gulf',  'Africa/Nairobi',   1, '19:30', 'weekday', 3),
  ('ea_gulf',  'Africa/Nairobi',   3, '19:30', 'weekday', 4),
  ('ea_gulf',  'Africa/Nairobi',   2, '13:00', 'weekday', 5),
  ('asia',     'Asia/Shanghai',    5, '14:00', 'weekend', 1),
  ('asia',     'Asia/Shanghai',    0, '19:30', 'weekday', 2),
  ('asia',     'Asia/Shanghai',    2, '19:30', 'weekday', 3)
ON CONFLICT (band, day_of_week, start_time) DO NOTHING;

INSERT INTO app.config (key, value)
SELECT 'match.min_catalogue_slots', '1'
 WHERE NOT EXISTS (SELECT 1 FROM app.config WHERE key = 'match.min_catalogue_slots');
-- Applied to production 2026-10-18 as migration s104_slot_catalogue.
