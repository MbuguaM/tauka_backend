-- ============================================================================
-- §98 (2026-10-18)  Enrolment spine: season, intake, enrolment_state (plan 1.1)
-- ============================================================================
--
-- DOMAIN: enrolment (tauka-python owns DDL and every write). E-01: a COHORT is
-- a season ("Bega 2019") and a POD is an app.cohorts row; the season is never
-- called `cohort` in the schema. E-02: formation is monthly on Ethiopic month
-- day 1, from an explicit calendar table, not a day-of-month rule. §2c: the
-- playbook's Gregorian "deposit cutoff" is now a LESSON DEADLINE (finish Phase 0
-- by this date to join this formation), so the columns say cutoff, not deposit.
--
-- Seeded with the playbook's 11 intakes for E.C. 2019 → Meskerem 2020. Every
-- formation date was recomputed from the Ethiopic calendar (JDN, Amete Mihret
-- epoch) on 2026-10-18 and matches, weekdays included.

-- ── season ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.season (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  season_name            text NOT NULL CHECK (season_name IN ('tsedey','bega','belg','kiremt')),
  ec_year                int  NOT NULL CHECK (ec_year BETWEEN 2000 AND 2100),
  -- 'founding' = Hidar 2019, the last month of Tsedey 2019, run standalone.
  kind                   text NOT NULL DEFAULT 'regular' CHECK (kind IN ('regular','founding')),
  display_name           text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 60),
  -- The founding cohort joins Bega 2019's gatherings.
  gathers_with_season_id uuid REFERENCES app.season(id) ON DELETE SET NULL,
  opening_gathering_at   timestamptz,
  mid_gathering_at       timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT season_one_regular_per_year UNIQUE (season_name, ec_year, kind)
);

-- ── intake ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.intake (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  season_id      uuid NOT NULL REFERENCES app.season(id) ON DELETE RESTRICT,
  ethiopic_month text NOT NULL CHECK (ethiopic_month IN
                   ('meskerem','tikimt','hidar','tahsas','tir','yekatit','megabit',
                    'miyazya','ginbot','sene','hamle','nehase')),  -- never pagume
  ec_year        int  NOT NULL,
  -- Matching day. Normally Ethiopic month day 1, but an admin may move it
  -- (Megabit 2019 moves to 11 Mar if Eid al-Fitr lands on the 10th), so no
  -- CHECK ties it to the calendar.
  formation_date date NOT NULL,
  -- The lesson deadline window (§2c): finish Phase 0 inside it to join.
  cutoff_opens   date NOT NULL,
  cutoff_closes  date NOT NULL,
  tier           text NOT NULL CHECK (tier IN ('founding','flagship','major','standard','light')),
  status         text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','matching','formed','cancelled')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT intake_one_per_month UNIQUE (ethiopic_month, ec_year),
  CONSTRAINT intake_cutoff_before_formation CHECK (cutoff_opens <= cutoff_closes AND cutoff_closes < formation_date)
);
CREATE INDEX IF NOT EXISTS idx_intake_upcoming ON app.intake (formation_date) WHERE status = 'scheduled';

-- ── enrolment_state ───────────────────────────────────────────────────────
-- One row per learner per course: where they are in the protocol. THE SPINE
-- EVERYTHING ELSE READS. Written only by tauka-python; the trigger below
-- refuses any transition not in app.enrolment_transition_ok, so a bug in one
-- caller cannot teleport a learner (say, waitlisted → in_pod).
CREATE TABLE IF NOT EXISTS app.enrolment_state (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id          uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  course_id           uuid NOT NULL REFERENCES app.course(id) ON DELETE CASCADE,
  state               text NOT NULL DEFAULT 'phase0' CHECK (state IN
                        ('waitlisted','phase0','paid','seat_confirmed','matched',
                         'in_pod','paused','wait_pool','cancelled')),
  target_intake_id    uuid REFERENCES app.intake(id) ON DELETE SET NULL,
  phase0_started_at   timestamptz,
  -- §2d: finishing Phase 0 (both lessons) is what makes a learner ready. Set
  -- from a completion record, never from telemetry.
  phase0_completed_at timestamptz,
  pace_pledge_hours   int  CHECK (pace_pledge_hours IS NULL OR pace_pledge_hours IN (5, 10, 20)),
  band                text CHECK (band IS NULL OR band IN ('americas','europe','ea_gulf','asia')),
  state_changed_at    timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT enrolment_state_one_per_course UNIQUE (student_id, course_id),
  -- seat_confirmed and everything after it require a finished Phase 0.
  CONSTRAINT enrolment_state_ready_means_phase0_done CHECK (
    state NOT IN ('seat_confirmed','matched','in_pod','wait_pool') OR phase0_completed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_enrolment_state_target ON app.enrolment_state (target_intake_id, state);

-- The protocol (plan §3; §2c puts payment before the seat). Reviewable in one
-- place; change it here, not in callers.
CREATE OR REPLACE FUNCTION app.enrolment_transition_ok(p_from text, p_to text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT p_from = p_to OR (p_from, p_to) IN (
    ('waitlisted',     'phase0'),
    ('waitlisted',     'cancelled'),
    ('phase0',         'paid'),
    ('phase0',         'cancelled'),
    ('paid',           'seat_confirmed'),   -- Phase 0 finished
    ('paid',           'paused'),
    ('paid',           'cancelled'),
    ('seat_confirmed', 'matched'),          -- placed at formation
    ('seat_confirmed', 'wait_pool'),        -- no pod at formation
    ('seat_confirmed', 'paused'),
    ('seat_confirmed', 'cancelled'),
    ('matched',        'in_pod'),           -- accepted, pod formed
    ('matched',        'wait_pool'),        -- offer declined or expired
    ('matched',        'cancelled'),
    ('wait_pool',      'matched'),
    ('wait_pool',      'paused'),
    ('wait_pool',      'cancelled'),
    ('in_pod',         'wait_pool'),        -- pod dissolved / moved at a checkpoint
    ('in_pod',         'paused'),
    ('in_pod',         'cancelled'),
    ('paused',         'paid'),             -- resume before Phase 0 is done
    ('paused',         'seat_confirmed'),   -- resume after it
    ('paused',         'cancelled'),
    ('cancelled',      'phase0'),           -- §2d: a cancelled learner is Free, and may come back
    ('cancelled',      'paid'),
    ('cancelled',      'seat_confirmed')
  );
$$;

CREATE OR REPLACE FUNCTION app.enrolment_state_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'app', 'pg_catalog'
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NOT app.enrolment_transition_ok(OLD.state, NEW.state) THEN
      RAISE EXCEPTION 'enrolment_state: % -> % is not a legal transition', OLD.state, NEW.state
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN
      NEW.state_changed_at := now();
    END IF;
    -- A finished Phase 0 stays finished.
    IF OLD.phase0_completed_at IS NOT NULL AND NEW.phase0_completed_at IS DISTINCT FROM OLD.phase0_completed_at THEN
      RAISE EXCEPTION 'enrolment_state: phase0_completed_at cannot be changed once set'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.state NOT IN ('waitlisted','phase0') THEN
    -- A row starts at the beginning of the protocol.
    RAISE EXCEPTION 'enrolment_state: a new row must start at waitlisted or phase0, not %', NEW.state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_enrolment_state_guard ON app.enrolment_state;
CREATE TRIGGER trg_enrolment_state_guard
  BEFORE INSERT OR UPDATE ON app.enrolment_state
  FOR EACH ROW EXECUTE FUNCTION app.enrolment_state_guard();

DROP TRIGGER IF EXISTS trg_season_updated_at ON app.season;
CREATE TRIGGER trg_season_updated_at BEFORE UPDATE ON app.season
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
DROP TRIGGER IF EXISTS trg_intake_updated_at ON app.intake;
CREATE TRIGGER trg_intake_updated_at BEFORE UPDATE ON app.intake
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
DROP TRIGGER IF EXISTS trg_enrolment_state_updated_at ON app.enrolment_state;
CREATE TRIGGER trg_enrolment_state_updated_at BEFORE UPDATE ON app.enrolment_state
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ── access ────────────────────────────────────────────────────────────────
-- The calendar is public: the website banner shows the next formation date to
-- visitors with no account. enrolment_state is the learner's own.
ALTER TABLE app.season          ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.intake          ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.enrolment_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS season_public_read ON app.season;
CREATE POLICY season_public_read ON app.season FOR SELECT TO anon, authenticated USING (true);
DROP POLICY IF EXISTS intake_public_read ON app.intake;
CREATE POLICY intake_public_read ON app.intake FOR SELECT TO anon, authenticated USING (true);
DROP POLICY IF EXISTS enrolment_state_select_own ON app.enrolment_state;
CREATE POLICY enrolment_state_select_own ON app.enrolment_state
  FOR SELECT TO authenticated USING (student_id = auth.uid());
DROP POLICY IF EXISTS enrolment_state_admin_read ON app.enrolment_state;
CREATE POLICY enrolment_state_admin_read ON app.enrolment_state
  FOR SELECT TO authenticated USING (app.is_admin());

REVOKE ALL ON app.season, app.intake, app.enrolment_state FROM anon, authenticated;
GRANT SELECT ON app.season, app.intake TO anon, authenticated;
GRANT SELECT ON app.enrolment_state TO authenticated;
GRANT ALL ON app.season, app.intake, app.enrolment_state TO service_role;

-- ── seed: the playbook's calendar, E.C. 2019 → Meskerem 2020 ──────────────
INSERT INTO app.season (season_name, ec_year, kind, display_name) VALUES
  ('bega',   2019, 'regular',  'Bega 2019'),
  ('belg',   2019, 'regular',  'Belg 2019'),
  ('kiremt', 2019, 'regular',  'Kiremt 2019'),
  ('tsedey', 2020, 'regular',  'Tsedey 2020'),
  ('tsedey', 2019, 'founding', 'Hidar 2019 (founding)')
ON CONFLICT (season_name, ec_year, kind) DO NOTHING;

UPDATE app.season f SET gathers_with_season_id = b.id
  FROM app.season b
 WHERE f.kind = 'founding' AND f.ec_year = 2019
   AND b.season_name = 'bega' AND b.ec_year = 2019 AND b.kind = 'regular'
   AND f.gathers_with_season_id IS NULL;

INSERT INTO app.intake (season_id, ethiopic_month, ec_year, formation_date, cutoff_opens, cutoff_closes, tier)
SELECT s.id, v.m, v.y, v.f::date, v.o::date, v.c::date, v.t
  FROM (VALUES
    -- Founding: capped at ~6 pods, two bands (Americas weekend, EA weekday evening).
    ('hidar',   2019, 'tsedey', 'founding', '2026-11-10', '2026-11-01', '2026-11-05', 'founding'),
    ('tahsas',  2019, 'bega',   'regular',  '2026-12-10', '2026-12-01', '2026-12-05', 'light'),
    -- EXCEPTION: opens 28 Dec (Kenyan December pay lands early).
    ('tir',     2019, 'bega',   'regular',  '2027-01-09', '2026-12-28', '2027-01-05', 'flagship'),
    ('yekatit', 2019, 'bega',   'regular',  '2027-02-08', '2027-02-01', '2027-02-05', 'standard'),
    -- EXCEPTION (conditional): move formation to 2027-03-11 if Eid al-Fitr lands
    -- on the 10th. Confirm two weeks ahead; it depends on moon sighting.
    ('megabit', 2019, 'belg',   'regular',  '2027-03-10', '2027-03-01', '2027-03-05', 'standard'),
    ('miyazya', 2019, 'belg',   'regular',  '2027-04-09', '2027-04-01', '2027-04-05', 'standard'),
    -- EXCEPTION: 3–6 May, after Fasika (2 May).
    ('ginbot',  2019, 'belg',   'regular',  '2027-05-09', '2027-05-03', '2027-05-06', 'major'),
    ('sene',    2019, 'kiremt', 'regular',  '2027-06-08', '2027-06-01', '2027-06-05', 'light'),
    ('hamle',   2019, 'kiremt', 'regular',  '2027-07-08', '2027-07-01', '2027-07-05', 'major'),
    ('nehase',  2019, 'kiremt', 'regular',  '2027-08-07', '2027-08-01', '2027-08-05', 'light'),
    -- No Pagume intake. Meskerem 2020 opens Tsedey 2020 on Enkutatash.
    ('meskerem',2020, 'tsedey', 'regular',  '2027-09-12', '2027-09-01', '2027-09-05', 'flagship')
  ) AS v(m, y, sn, sk, f, o, c, t)
  JOIN app.season s ON s.season_name = v.sn AND s.kind = v.sk AND s.ec_year = v.y
ON CONFLICT (ethiopic_month, ec_year) DO NOTHING;

COMMENT ON TABLE app.season IS
  'A COHORT (E-01): an Ethiopian season, e.g. Bega 2019 - every pod formed in it. '
  'Never called cohort in the schema; app.cohorts is the POD. tauka-python writes; public read.';
COMMENT ON TABLE app.intake IS
  'One monthly formation (E-02): Ethiopic month day 1 unless moved. cutoff_* is the '
  'Phase 0 lesson deadline window (§2c), not a deposit. Replaces match.intake_days '
  '(retired by plan 3.4). tauka-python writes; public read.';
COMMENT ON TABLE app.enrolment_state IS
  'One row per learner per course: their place in the protocol. Transitions are '
  'enforced by trg_enrolment_state_guard / app.enrolment_transition_ok. Written by '
  'tauka-python only; the learner and admins read.';
-- Applied to production 2026-10-18 as migration s98_enrolment_spine_season_intake_state.
