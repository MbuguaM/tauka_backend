-- ============================================================================
-- §109 (2026-10-18)  Monthly pod checkpoints (enrolment plan 4.2, §2c)
-- ============================================================================
--
-- cohort_matching domain (tauka-python).
--
-- §2c: "Pod checkpoints are monthly, on formation day (the first day of the
-- Ethiopic month): tutor rotation (= guest rota review, E-09), pace moves and
-- refills happen there." One row per live pod per Ethiopic month 1-12 (Pagume,
-- five or six days long, has no formation day and no checkpoint).
--
-- The daily job writes the row with a SNAPSHOT of the month just gone, built
-- only from what E-16 allows a decision to read: tutor-marked attendance
-- (cohort_session_attendance), the sessions and who led them (the guest rota),
-- members, open seats, the lesson each member is on (unit_progress) and their
-- pace pledge. Never activity_summary or ai_usage. The snapshot is for a
-- person to read; nothing acts on it automatically.
--
-- An ops admin reviews it once: an outcome (continue | adjust | pause |
-- dissolve), whether the guest rota was reviewed, and which members move to
-- the wait pool (a pace move). tauka-python carries the decision out through
-- the CONFLICT-048 write path (membership status/left_at/removal_reason, pod
-- status) and enrolment_state (in_pod -> wait_pool, "moved at a checkpoint").
--
-- No free-text note: what is recorded about a learner here is the snapshot
-- and the move, nothing more (legal.md).

CREATE TABLE IF NOT EXISTS app.pod_checkpoint (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id         uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  ec_year           int  NOT NULL CHECK (ec_year > 2000),
  ec_month          int  NOT NULL CHECK (ec_month BETWEEN 1 AND 12),
  -- Gregorian date of Ethiopic day 1 of (ec_year, ec_month).
  due_on            date NOT NULL,
  snapshot          jsonb NOT NULL DEFAULT '{}'::jsonb,
  status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open','reviewed')),
  outcome           text CHECK (outcome IS NULL OR outcome IN ('continue','adjust','pause','dissolve')),
  rota_reviewed     boolean NOT NULL DEFAULT false,
  moved_student_ids uuid[] NOT NULL DEFAULT '{}',
  reviewed_by       uuid,
  reviewed_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pod_checkpoint_one_per_month UNIQUE (cohort_id, ec_year, ec_month),
  CONSTRAINT pod_checkpoint_reviewed CHECK (
    (status = 'open') = (outcome IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_pod_checkpoint_open ON app.pod_checkpoint (due_on) WHERE status = 'open';

-- A review is final, and the snapshot never changes.
CREATE OR REPLACE FUNCTION app.pod_checkpoint_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.status <> 'open' THEN
    RAISE EXCEPTION 'pod_checkpoint: already reviewed' USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.cohort_id, NEW.ec_year, NEW.ec_month, NEW.due_on, NEW.snapshot, NEW.created_at)
     IS DISTINCT FROM
     (OLD.cohort_id, OLD.ec_year, OLD.ec_month, OLD.due_on, OLD.snapshot, OLD.created_at) THEN
    RAISE EXCEPTION 'pod_checkpoint: only the review may change' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_pod_checkpoint_guard ON app.pod_checkpoint;
CREATE TRIGGER trg_pod_checkpoint_guard BEFORE UPDATE ON app.pod_checkpoint
  FOR EACH ROW EXECUTE FUNCTION app.pod_checkpoint_guard();

ALTER TABLE app.pod_checkpoint ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS pod_checkpoint_ops ON app.pod_checkpoint;
CREATE POLICY pod_checkpoint_ops ON app.pod_checkpoint FOR SELECT TO authenticated
  USING (app.has_admin_capability('ops'));
REVOKE ALL ON app.pod_checkpoint FROM anon, authenticated;
GRANT SELECT ON app.pod_checkpoint TO authenticated;
GRANT ALL ON app.pod_checkpoint TO service_role;

COMMENT ON TABLE app.pod_checkpoint IS
  'Monthly pod checkpoint on Ethiopic day 1 (plan 4.2, §2c). Snapshot from tutor-marked '
  'attendance, sessions/guest rota, seats, lesson and pace pledge only (E-16). Reviewed once '
  'by ops: outcome, rota reviewed, members moved to the wait pool. Written by tauka-python.';
-- Applied to production 2026-10-18 as migration s109_pod_checkpoint.
