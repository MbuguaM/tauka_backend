-- ============================================================================
-- §119 (2026-10-18)  Handoff notes (enrolment plan 4.3, E-09)
-- ============================================================================
--
-- tutor_management domain (tauka-python).
--
-- Playbook: "Handoff notes at each rotation: per learner, recurring errors,
-- goals and confidence." E-09: rotation is the guest rota, so the notes are
-- written by the HOME tutor for whoever leads a guest session, and the warm
-- handoff carries them to an incoming home tutor.
--
-- Decided 2026-10-18 (developer):
--   * one LIVING note per learner: recurring errors (<= 280), goals (<= 280),
--     confidence hesitant | building | confident;
--   * the home tutor keeps it current; the dashboard prompts when a guest-led
--     session is within 7 days and a note is missing or older than 30 days —
--     not a hard block;
--   * readable by the learner's own tutors (home and guest links, active or
--     handoff_pending — so an incoming tutor in a warm handoff reads it), any
--     tutor leading one of their pod's sessions in the next 14 days, ops, and
--     THE LEARNER. Written only through tauka-python.

CREATE TABLE IF NOT EXISTS app.handoff_note (
  student_id       uuid PRIMARY KEY REFERENCES app.student(id) ON DELETE CASCADE,
  recurring_errors text CHECK (recurring_errors IS NULL OR length(recurring_errors) <= 280),
  goals            text CHECK (goals IS NULL OR length(goals) <= 280),
  confidence       text NOT NULL CHECK (confidence IN ('hesitant','building','confident')),
  written_by       uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Who may read a learner's note, in one place (the API checks the same rule).
CREATE OR REPLACE FUNCTION app.can_read_handoff_note(p_student uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog'
AS $$
  SELECT auth.uid() = p_student
      OR app.has_admin_capability('ops')
      OR EXISTS (SELECT 1 FROM app.student_tutor_link l
                  WHERE l.student_id = p_student AND l.tutor_id = auth.uid()
                    AND l.state IN ('active','handoff_pending'))
      OR EXISTS (SELECT 1 FROM app.cohort_session s
                   JOIN app.cohort_memberships m ON m.cohort_id = s.cohort_id
                  WHERE m.user_id = p_student AND m.status = 'active'
                    AND s.lead_tutor_id = auth.uid() AND s.status = 'scheduled'
                    AND s.scheduled_at BETWEEN now() - interval '1 day' AND now() + interval '14 days');
$$;
REVOKE ALL ON FUNCTION app.can_read_handoff_note(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.can_read_handoff_note(uuid) TO authenticated, service_role;

ALTER TABLE app.handoff_note ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS handoff_note_read ON app.handoff_note;
CREATE POLICY handoff_note_read ON app.handoff_note FOR SELECT TO authenticated
  USING (app.can_read_handoff_note(student_id));
REVOKE ALL ON app.handoff_note FROM anon, authenticated;
GRANT SELECT ON app.handoff_note TO authenticated;
GRANT ALL ON app.handoff_note TO service_role;

COMMENT ON TABLE app.handoff_note IS 'A home tutor living note on a learner for guest-led sessions and warm handoffs (plan 4.3, E-09): recurring errors, goals, confidence. The learner reads it too. Written by tauka-python.';
-- Applied to production 2026-10-18 in two parts after a dry run (the learner reads their own
-- note, a stranger does not): s119_handoff_note, s119_handoff_note_rls.
