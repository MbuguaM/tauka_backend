-- ============================================================================
-- §117 (2026-10-18)  Concern reports (enrolment plan 4.6)
-- ============================================================================
--
-- cohort_matching domain (tauka-python), beside session_complaint (§106).
--
-- Playbook: "Safeguarding or conduct issues are handled immediately by the
-- safeguarding lead, not deferred to a checkpoint", and "a named safeguarding
-- lead who handles reports within 24 hours". Decided 2026-10-18 (developer): a
-- concern report is SEPARATE from a session complaint — for anything that
-- worries a learner, in or out of a session (another learner, a tutor,
-- contact-detail swapping, wellbeing) — goes ONLY to the safeguarding
-- capability, alerts at once and again if not acknowledged within 24 hours,
-- and never grants a credit.
--
-- The note is free text about a person. It is read only by the reporter and
-- safeguarding — never by the person it is about, never by ops — and is not in
-- the alert email. Retention is an open legal question (L-015, beside L-014).

CREATE TABLE IF NOT EXISTS app.concern_report (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  reporter_id           uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  about_kind            text NOT NULL CHECK (about_kind IN ('tutor','learner','someone_else','myself')),
  -- Someone the reporter shares (or shared) a pod or a session with; tauka-python checks it.
  about_user_id         uuid,
  session_type          text CHECK (session_type IS NULL OR session_type IN
                          ('cohort_session','makeup_session','clinic','one_to_one_session')),
  session_id            uuid,
  category              text NOT NULL CHECK (category IN
                          ('harassment','contact_details','inappropriate_content','wellbeing','safety','other')),
  detail                text CHECK (detail IS NULL OR length(detail) <= 1000),
  status                text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','closed')),
  outcome               text CHECK (outcome IS NULL OR outcome IN
                          ('no_action','advice_given','warning_issued','removed','escalated')),
  handled_by            uuid,
  acknowledged_at       timestamptz,
  closed_at             timestamptz,
  alert_sent_at         timestamptz,
  overdue_alert_sent_at timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT concern_report_session_pair CHECK ((session_type IS NULL) = (session_id IS NULL)),
  CONSTRAINT concern_report_about CHECK (
    (about_kind IN ('tutor','learner')) OR about_user_id IS NULL),
  CONSTRAINT concern_report_ack CHECK ((status = 'open') = (acknowledged_at IS NULL)),
  CONSTRAINT concern_report_closed CHECK ((status = 'closed') = (closed_at IS NOT NULL AND outcome IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_concern_report_open ON app.concern_report (created_at) WHERE status <> 'closed';

-- open -> acknowledged -> closed (or open -> closed); the report itself never changes.
CREATE OR REPLACE FUNCTION app.concern_report_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status, NEW.status) IN (
       ('open','acknowledged'), ('open','closed'), ('acknowledged','closed')) THEN
    RAISE EXCEPTION 'concern_report: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.reporter_id, NEW.about_kind, NEW.about_user_id, NEW.session_type, NEW.session_id, NEW.category,
      NEW.detail, NEW.created_at)
     IS DISTINCT FROM (OLD.reporter_id, OLD.about_kind, OLD.about_user_id, OLD.session_type, OLD.session_id,
      OLD.category, OLD.detail, OLD.created_at) THEN
    RAISE EXCEPTION 'concern_report: the report is not edited' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER trg_concern_report_guard BEFORE UPDATE ON app.concern_report
  FOR EACH ROW EXECUTE FUNCTION app.concern_report_guard();

ALTER TABLE app.concern_report ENABLE ROW LEVEL SECURITY;
CREATE POLICY concern_report_read ON app.concern_report FOR SELECT TO authenticated
  USING (reporter_id = auth.uid() OR app.has_admin_capability('safeguarding'));
REVOKE ALL ON app.concern_report FROM anon, authenticated;
GRANT SELECT ON app.concern_report TO authenticated;
GRANT ALL ON app.concern_report TO service_role;
COMMENT ON TABLE app.concern_report IS 'A learner concern (plan 4.6): anything that worries them, in or out of a session. Safeguarding only; alert at once and again after 24 h unacknowledged; never a credit. Never visible to the person it is about. tauka-python writes.';
-- Applied to production 2026-10-18 in three parts after a dry run: s117_concern_report,
-- s117_concern_report_guard, s117_concern_report_rls.
