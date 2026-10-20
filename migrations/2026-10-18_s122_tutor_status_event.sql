-- ============================================================================
-- §122 (2026-10-18)  Tutor status changes, on the record (enrolment plan §2j J4)
-- ============================================================================
--
-- tutor_management domain (tauka-python).
--
-- §2j: after approval a tutor is `onboarding`; an admin moves them to
-- `active` (once contract, checks and calibration are done), `suspended`
-- (temporarily off: not matched; their pods need cover) or `offboarded` (left;
-- with deleted_at). tauka-python writes app.tutor.status (CONFLICT-057).
--
-- "One decision trail" (§2j Rules): every change is an append-only event —
-- who, when, from, to, and a reason CODE (no free text about the tutor).

CREATE TABLE IF NOT EXISTS app.tutor_status_event (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id    uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  from_status text,
  to_status   text NOT NULL CHECK (to_status IN ('onboarding','active','suspended','offboarded')),
  reason_code text NOT NULL CHECK (reason_code IN
                ('approved','onboarding_complete','returning','leave','conduct','reliability','left','other')),
  actor       uuid,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tutor_status_event_tutor ON app.tutor_status_event (tutor_id, created_at);

CREATE OR REPLACE FUNCTION app.tutor_status_event_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'tutor_status_event is append-only' USING ERRCODE = 'check_violation';
END;
$$;
CREATE TRIGGER trg_tutor_status_event_append_only BEFORE UPDATE OR DELETE ON app.tutor_status_event
  FOR EACH ROW EXECUTE FUNCTION app.tutor_status_event_append_only();

ALTER TABLE app.tutor_status_event ENABLE ROW LEVEL SECURITY;
CREATE POLICY tutor_status_event_read ON app.tutor_status_event FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.tutor_status_event FROM anon, authenticated;
GRANT SELECT ON app.tutor_status_event TO authenticated;
GRANT ALL ON app.tutor_status_event TO service_role;
COMMENT ON TABLE app.tutor_status_event IS 'Append-only trail of app.tutor.status changes (plan §2j J4): from, to, a reason code, who, when. The tutor and admins read. tauka-python writes.';
-- Applied to production 2026-10-18 as migration s122_tutor_status_event.
