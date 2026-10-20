-- ============================================================================
-- §106 (2026-10-18)  Session complaints (enrolment plan §2f item 7)
-- ============================================================================
--
-- cohort_matching domain (tauka-python), beside session_credit.
--
-- §2e: "Carry-forward exists only for our failures: a tutor issue (no-show,
-- late cancel) or a student complaint that is upheld." Until now the upheld
-- half had a function (makeup_service.grant_credit) and nowhere to start from.
--
-- A learner complains about one live session they were in (pod, make-up,
-- clinic or 1-on-1), once, within 14 days, with a reason code and an optional
-- short note. Conduct complaints are routed to SAFEGUARDING (§2k: a different
-- person from ops once there are two admins); the rest to OPS. An upheld
-- complaint grants one session credit, referenced to the complaint so a second
-- "uphold" can never mint a second credit (idx_session_credit_one_per_reference).
--
-- The note is free text about a tutor: it is shown only to the routed admins
-- and the learner who wrote it, never to the tutor. Retention is an open legal
-- question (L-014).

CREATE TABLE IF NOT EXISTS app.session_complaint (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id   uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  session_type text NOT NULL CHECK (session_type IN ('cohort_session','makeup_session','clinic','one_to_one_session')),
  -- No FK: four possible parents. tauka-python checks the learner was in it.
  session_id   uuid NOT NULL,
  reason_code  text NOT NULL CHECK (reason_code IN
                 ('tutor_absent','tutor_late','cut_short','connection','conduct','other')),
  detail       text CHECK (detail IS NULL OR length(detail) <= 1000),
  routed_to    text NOT NULL CHECK (routed_to IN ('ops','safeguarding')),
  status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open','upheld','rejected')),
  reviewed_by  uuid,
  reviewed_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT session_complaint_one_per_session UNIQUE (student_id, session_type, session_id),
  CONSTRAINT session_complaint_conduct_is_safeguarding CHECK ((reason_code = 'conduct') = (routed_to = 'safeguarding')),
  CONSTRAINT session_complaint_reviewed CHECK ((status = 'open') = (reviewed_at IS NULL AND reviewed_by IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_session_complaint_open ON app.session_complaint (routed_to, created_at) WHERE status = 'open';

-- A decision is final: open -> upheld | rejected, once.
CREATE OR REPLACE FUNCTION app.session_complaint_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.status <> 'open' THEN
    RAISE EXCEPTION 'session_complaint: % is final', OLD.status USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.student_id, NEW.session_type, NEW.session_id, NEW.reason_code, NEW.detail, NEW.routed_to, NEW.created_at)
     IS DISTINCT FROM
     (OLD.student_id, OLD.session_type, OLD.session_id, OLD.reason_code, OLD.detail, OLD.routed_to, OLD.created_at) THEN
    RAISE EXCEPTION 'session_complaint: only the decision may change' USING ERRCODE = 'check_violation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_session_complaint_guard ON app.session_complaint;
CREATE TRIGGER trg_session_complaint_guard BEFORE UPDATE ON app.session_complaint
  FOR EACH ROW EXECUTE FUNCTION app.session_complaint_guard();

ALTER TABLE app.session_complaint ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS session_complaint_own ON app.session_complaint;
CREATE POLICY session_complaint_own ON app.session_complaint FOR SELECT TO authenticated
  USING (student_id = auth.uid()
         OR (routed_to = 'ops' AND app.has_admin_capability('ops'))
         OR (routed_to = 'safeguarding' AND app.has_admin_capability('safeguarding')));
REVOKE ALL ON app.session_complaint FROM anon, authenticated;
GRANT SELECT ON app.session_complaint TO authenticated;
GRANT ALL ON app.session_complaint TO service_role;

-- An upheld complaint's credit references the complaint (one credit, ever).
ALTER TABLE app.session_credit DROP CONSTRAINT IF EXISTS session_credit_reference_type_check;
ALTER TABLE app.session_credit ADD CONSTRAINT session_credit_reference_type_check CHECK (
  reference_type IS NULL OR reference_type IN ('makeup_session','cohort_session','session_complaint'));

COMMENT ON TABLE app.session_complaint IS
  'A learner''s complaint about one live session they were in (§2f item 7). Conduct -> '
  'safeguarding, else ops. Upheld -> one session credit referencing this row. Written by '
  'tauka-python; the learner and the routed admins read. Never shown to the tutor.';
-- Applied to production 2026-10-18 as migration s106_session_complaint.
