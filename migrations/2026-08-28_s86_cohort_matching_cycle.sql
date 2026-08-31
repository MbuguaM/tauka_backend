-- ============================================================================
-- §86  PHASE 2 — the cycle: what a run was, what it offered, what it deferred
--      Plan: tutor-cohort-matching-plan.md §5.3, §3 Stages 5-6, Phase 2.
--      DOMAIN: cohort_matching. OWNER: tauka-python.
--      Also registers tauka-python as a SHARED WRITER of app.cohorts and
--      app.cohort_memberships (plan §5.0) — documentation, not DDL.
-- ============================================================================
--
-- Plan §5.1/§5.2 landed in §65/§66, so Phase 2's schema work is these three
-- tables and nothing else. They are the matcher's output, and the reason the
-- pipeline can be a pure function with persistence as a separate step: Stages
-- 0-4 produce (cohorts, offers, deferrals) in memory, and writing them is one
-- transaction that these tables define the shape of.

BEGIN;

-- ---------------------------------------------------------------------------
-- 86.1  app.intake_cycle — one row per run
-- ---------------------------------------------------------------------------
-- THE CONFIG SNAPSHOT IS THE POINT. Plan §3 Stage 3: "All weights and bands
-- live in a config table, snapshotted into the `intake_cycle` row. They will be
-- tuned, nobody should need a deploy to tune them, and a past cycle's result is
-- inexplicable without knowing what it ran under." A cycle row without its
-- config is a result nobody can reproduce and nobody can defend to the student
-- it deferred.
--
-- `mode` EXISTS SO THE DRY RUN IS FREE RATHER THAN A FEATURE (plan §4, Phase 3).
-- A dry run writes an intake_cycle row and nothing else — no cohorts, no
-- offers, no memberships — so the bands and weights can be tuned against
-- synthetic and adversarial pools before a single real student is placed. Its
-- proposals live in `result` on this row, which is why that column exists and
-- is NULL on a live cycle (whose proposals are real rows elsewhere).
CREATE TABLE IF NOT EXISTS app.intake_cycle (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cycle_date     date NOT NULL,
  mode           text NOT NULL DEFAULT 'dry_run',
  status         text NOT NULL DEFAULT 'running',
  config         jsonb NOT NULL,
  result         jsonb,
  pool_size      int NOT NULL DEFAULT 0,
  students_placed int NOT NULL DEFAULT 0,
  students_deferred int NOT NULL DEFAULT 0,
  cohorts_formed int NOT NULL DEFAULT 0,
  notes          text,
  started_at     timestamptz NOT NULL DEFAULT now(),
  closed_at      timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT intake_cycle_mode_check   CHECK (mode IN ('dry_run', 'live')),
  CONSTRAINT intake_cycle_status_check CHECK (status IN
    ('running', 'offered', 'closed', 'failed')),
  -- A dry run must never carry the counts of a live run's real placements.
  CONSTRAINT intake_cycle_dry_run_check CHECK (
    mode = 'live' OR cohorts_formed = 0)
);

-- ONE LIVE CYCLE PER DATE, UNLIMITED DRY RUNS. Plan §3 Stage 7: the pool is
-- frozen at the start of a run, and a second live run on the same date would
-- re-freeze a pool the first run has already placed people out of.
CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_cycle_live_date
  ON app.intake_cycle (cycle_date) WHERE mode = 'live';

CREATE INDEX IF NOT EXISTS idx_intake_cycle_recent
  ON app.intake_cycle (started_at DESC);

COMMENT ON TABLE app.intake_cycle IS
  'One matching run. The config snapshot makes a past result reproducible and explicable; without it no cycle can be defended to the student it deferred.';
COMMENT ON COLUMN app.intake_cycle.config IS
  'Weights and bands AS USED, not as currently configured. Never backfill or normalise this - its whole value is that it is what ran.';
COMMENT ON COLUMN app.intake_cycle.result IS
  'Dry-run proposals: cohorts, offers and deferrals that were NOT written. NULL on a live cycle, whose output is real rows in cohorts / match_offer / deferred_student.';
COMMENT ON COLUMN app.intake_cycle.status IS
  'running -> offered (holds are out) -> closed (swept: under-filled cohorts dissolved, cycle final). failed is a run that raised; its partial output, if any, is named in notes.';

-- ---------------------------------------------------------------------------
-- 86.2  app.match_offer — the soft hold
-- ---------------------------------------------------------------------------
-- Plan §3 Stage 5. The spec's "present 2-3 options and let the student choose"
-- oversells seats when taken literally: offer one 4-seat cohort to nine people
-- and five are told "no longer available" after being promised a place. So an
-- offer is a HOLD with a deadline, a cohort accepts at most
-- capacity + overbook_slack of them, and a released seat is re-offered
-- immediately rather than deferred to the next cycle.
--
-- THERE IS NO `preferred_tutor_id` AND THERE MUST NOT BE ONE.
-- Plan §5.3 and §3 Stage 4 originally carried a student-chosen-tutor flag for
-- the $10 "Choose your tutor" upgrade. That product was **removed entirely**
-- (D-04, finished as D-32; platform plan §3.1c), which warned that if the four
-- places did not go together "the scoring silently keeps a paid input nobody
-- can buy". This table is the fourth place, and it is being created after the
-- removal — so the column is not dropped here, it is never made.
-- `priority` is a DIFFERENT product and it survives: it is the Intensive tier's
-- "priority tutor matching", the same promise `enrollment_commitment
-- .priority_matching` sells by the term.
CREATE TABLE IF NOT EXISTS app.match_offer (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cycle_id         uuid NOT NULL REFERENCES app.intake_cycle(id) ON DELETE CASCADE,
  student_id       uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  cohort_id        uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  tutor_id         uuid REFERENCES app.tutor(id) ON DELETE SET NULL,
  rank             int  NOT NULL DEFAULT 1,
  score            int  NOT NULL DEFAULT 0,
  score_breakdown  jsonb NOT NULL DEFAULT '{}'::jsonb,
  priority         boolean NOT NULL DEFAULT false,
  status           text NOT NULL DEFAULT 'offered',
  expires_at       timestamptz NOT NULL,
  responded_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT match_offer_rank_check   CHECK (rank BETWEEN 1 AND 5),
  CONSTRAINT match_offer_score_check  CHECK (score BETWEEN 0 AND 100),
  CONSTRAINT match_offer_status_check CHECK (status IN
    ('offered', 'accepted', 'declined', 'expired', 'superseded')),
  -- A terminal status without a timestamp is an offer nobody can audit.
  -- `expired` is exempt: it is set by a sweep against `expires_at`, which
  -- already records when it happened.
  CONSTRAINT match_offer_responded_check CHECK (
    status IN ('offered', 'expired') OR responded_at IS NOT NULL),
  CONSTRAINT match_offer_unique UNIQUE (cycle_id, student_id, cohort_id)
);

-- A student holds at most one accepted offer per cycle. The API enforces the
-- rest of Stage 5 (capacity + overbook_slack, immediate re-offer on release);
-- this index is the guard that survives a race between two accepts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_offer_one_accept
  ON app.match_offer (cycle_id, student_id) WHERE status = 'accepted';

CREATE INDEX IF NOT EXISTS idx_match_offer_student
  ON app.match_offer (student_id, status);
-- The expiry sweep: every live hold, oldest deadline first.
CREATE INDEX IF NOT EXISTS idx_match_offer_expiry
  ON app.match_offer (expires_at) WHERE status = 'offered';
CREATE INDEX IF NOT EXISTS idx_match_offer_cohort
  ON app.match_offer (cohort_id, status);

DROP TRIGGER IF EXISTS trg_match_offer_updated_at ON app.match_offer;
CREATE TRIGGER trg_match_offer_updated_at BEFORE UPDATE ON app.match_offer
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.match_offer IS
  'A soft hold on a seat, with a deadline. Not a notification: accepting one is what creates a membership, and expiry releases the seat to the next student waiting.';
COMMENT ON COLUMN app.match_offer.score_breakdown IS
  'Per-term scores from plan section 3 Stage 3. The natural-language summary shown to the student is a TEMPLATE over these two highest terms, never an LLM call - it must be instant, stable and incapable of misstating someone schedule.';
COMMENT ON COLUMN app.match_offer.priority IS
  'Intensive tier "priority tutor matching", and enrollment_commitment.priority_matching. First key in the deterministic tie-break, and a floor in the ejection rules. NOT a paid per-match override - that product was removed (D-04).';
COMMENT ON COLUMN app.match_offer.status IS
  'superseded = the cohort filled or dissolved under this hold and the student was re-offered elsewhere. Distinct from declined, which is the student answering no, and from expired, which is the student not answering at all.';

-- ---------------------------------------------------------------------------
-- 86.3  app.deferred_student — the most valuable output of an early cycle
-- ---------------------------------------------------------------------------
-- Plan §3 Stage 6 and §4b: "Deferral is an output, not a failure." This table is
-- simultaneously the student's explanation, the tutor-recruitment brief, and the
-- evidence for whether tz_band and lesson_band are too tight. Early cycles will
-- defer more people than they place, and that is the expected shape rather than
-- a malfunction.
--
-- THE REASON ENUM IS THE PLAN'S SIX PLUS FIVE, AND EACH ADDITION IS DELIBERATE.
-- The plan lists six reasons and calls the enum closed. Five more are needed
-- before it can describe a real cycle, and folding any of them into an existing
-- reason would make that reason lie:
--
--   NO_TIMEZONE          - the student has no profile timezone, so no window of
--                          theirs can be converted. Filing this as
--                          NO_SLOT_OVERLAP would send a recruitment signal
--                          ("a rota gap") for what is a one-field form.
--   TOO_FEW_PREFERENCES  - fewer than the minimum windows. Same reasoning; it
--                          is also the only reason on this list the student can
--                          fix in a minute.
--   NO_LEVEL             - no placement result, so there is no level to match
--                          at. Distinct from NO_TUTOR_FOR_LEVEL, which is a
--                          hiring brief with a number on it.
--   COHORT_DID_NOT_FILL  - they accepted, and the cohort dissolved under them at
--                          cycle close for want of a third member (Stage 5).
--                          This person did everything right and must not be
--                          counted among those who declined.
--   OFFERS_EXPIRED       - they were offered and never answered. Silence is not
--                          a decline: DECLINED_ALL_OFFERS is explicitly "a
--                          scoring signal" in the plan, and pouring non-response
--                          into it poisons the one signal that says the offers
--                          were unattractive.
CREATE TABLE IF NOT EXISTS app.deferred_student (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cycle_id       uuid NOT NULL REFERENCES app.intake_cycle(id) ON DELETE CASCADE,
  student_id     uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  reason         text NOT NULL,
  detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
  cycles_waited  int  NOT NULL DEFAULT 0,
  bands_relaxed  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT deferred_student_reason_check CHECK (reason IN (
    'NO_TUTOR_FOR_LEVEL',
    'NO_SLOT_OVERLAP',
    'TZ_ISOLATE',
    'TOO_FEW_PEERS_AT_LEVEL',
    'TUTORS_AT_CAPACITY',
    'DECLINED_ALL_OFFERS',
    'NO_TIMEZONE',
    'TOO_FEW_PREFERENCES',
    'NO_LEVEL',
    'COHORT_DID_NOT_FILL',
    'OFFERS_EXPIRED')),
  CONSTRAINT deferred_student_unique UNIQUE (cycle_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_deferred_student_student
  ON app.deferred_student (student_id, created_at DESC);
-- The recruitment brief: "how many at level N had no certified tutor".
CREATE INDEX IF NOT EXISTS idx_deferred_student_reason
  ON app.deferred_student (reason, created_at DESC);

COMMENT ON TABLE app.deferred_student IS
  'Why a student was not placed this cycle. The student explanation, the tutor-recruitment brief and the evidence on whether the bands are too tight, in one table.';
COMMENT ON COLUMN app.deferred_student.cycles_waited IS
  'The value USED as a tie-break input in this cycle, recorded for audit. The live figure is a fold over this table since the student last joined a cohort - never a mutable counter.';
COMMENT ON COLUMN app.deferred_student.bands_relaxed IS
  'Cold-start ladder Stage 2 (plan section 4b): after two failed cycles tz_band widens 2h->3h and lesson_band 5->8. Recorded per deferral so a looser attempt that still failed is distinguishable from a strict one.';
COMMENT ON COLUMN app.deferred_student.detail IS
  'The diagnostic that would resolve the reason - which window had no tutor, how many more peers would open a cohort, which offset is stranded. Ids and numbers only.';

-- ---------------------------------------------------------------------------
-- 86.4  RLS
-- ---------------------------------------------------------------------------
ALTER TABLE app.intake_cycle      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.match_offer       ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.deferred_student  ENABLE ROW LEVEL SECURITY;

-- A cycle row carries the whole platform's supply picture and every weight it
-- ran under. Admins only; a student's own outcome reaches them through
-- match_offer and deferred_student.
DROP POLICY IF EXISTS intake_cycle_select_admin ON app.intake_cycle;
CREATE POLICY intake_cycle_select_admin ON app.intake_cycle
  FOR SELECT USING (app.is_admin());

-- A student reads their own offers. THERE IS DELIBERATELY NO CLIENT WRITE
-- POLICY: accepting is not "set status = accepted". It has to check the
-- cohort's live capacity, invalidate the student's other holds, create the
-- membership through the rules in `matching_service`, and re-offer the seats it
-- releases. A client that could write this column would skip all four, and the
-- first symptom would be a five-member cohort.
DROP POLICY IF EXISTS match_offer_select_own ON app.match_offer;
CREATE POLICY match_offer_select_own ON app.match_offer
  FOR SELECT USING (student_id = auth.uid() OR app.is_admin());

-- The student is owed the reason (plan §4b Stage 1: "a plain-language reason
-- from the deferral enum, not 'no match found'"). They are not owed, and must
-- not be shown, anyone else's.
DROP POLICY IF EXISTS deferred_student_select_own ON app.deferred_student;
CREATE POLICY deferred_student_select_own ON app.deferred_student
  FOR SELECT USING (student_id = auth.uid() OR app.is_admin());

-- ---------------------------------------------------------------------------
-- 86.5  Configuration — every band and weight, tunable without a deploy
-- ---------------------------------------------------------------------------
-- Plan §4: "The bands need to be tunable per cycle. A ±2h rule is right at
-- scale and may be strangling at launch; that should be a config change with a
-- recorded rationale, not a code change." The recorded rationale is the
-- snapshot in `intake_cycle.config`.
--
-- Every value below is the number the plan chose. Nothing here is new policy.
INSERT INTO app.config (key, value) VALUES
  ('match.intake_days',          '1,15'),
  ('match.tz_band_hours',        '2'),
  ('match.lesson_band',          '5'),
  ('match.civil_hours',          '07:00-22:00'),
  ('match.cohort_min',           '3'),
  ('match.cohort_max',           '4'),
  ('match.min_preferences',      '3'),
  ('match.session_minutes',      '50'),
  ('match.offer_ranks',          '3'),
  ('match.offer_ttl_hours',      '48'),
  ('match.overbook_slack',       '2'),
  ('match.relax_after_cycles',   '2'),
  ('match.tz_band_hours_relaxed','3'),
  ('match.lesson_band_relaxed',  '8'),
  ('match.experience_years',     '3'),
  ('match.referral_preference',  'hard'),
  ('match.weights', '{"goal":30,"style":20,"l1":15,"experience":10,'
                 || '"size_4":25,"size_3":15,"tightness_lesson":10,'
                 || '"tightness_tz":5,"slot_pref":10,"tier_cohesion":20}')
ON CONFLICT (key) DO NOTHING;

COMMIT;

-- ============================================================================
-- 86.6  OWNERSHIP — documentation, not DDL
-- ============================================================================
--
-- tauka-python becomes a WRITER of app.cohorts and app.cohort_memberships,
-- which tauka-flutter owns. This is the app.student.tier pattern exactly: DDL
-- owned by one project, one specific write path owned by another via the
-- service role. Plan §5.0 requires it be registered rather than assumed, and it
-- is - in shared-db/OWNERS.md and CONFLICT-048.
--
-- NO POLICY CHANGE IS NEEDED OR MADE. Both tables' write policies are
-- app.is_admin(), an authenticated-user predicate; the service role bypasses
-- RLS. Adding a policy for tauka-python would widen the authenticated surface
-- to grant something the service role already has, which is how a shared-write
-- path becomes a client-write path by accident.
--
-- The columns tauka-python writes, and no others:
--   app.cohorts             INSERT (formation) and UPDATE of status,
--                           band_lesson_low/high, intake_cycle
--   app.cohort_memberships  INSERT (on offer acceptance) and UPDATE of
--                           status/left_at/removal_reason
--
-- It must NEVER write: health_score / health_status / health_computed_at (the
-- cohort-health function's, §84), tutor_id after formation (a tutor change is a
-- §1.7 handoff, not a rematch), or recording_consent (the members' answer).
