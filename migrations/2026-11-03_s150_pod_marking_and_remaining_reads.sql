-- §150 Finishes the pod rule (2026-11-03, developer). Needs §149.
--
-- Attendance: the tutor leading a session may mark it, because tutor pay counts delivered
-- sessions and a guest-led session otherwise can't be marked by whoever taught it. Admins
-- need ops (an owner holds it), matching who may read attendance.
-- Assignments, pod norms and session recordings: the pod's members, plus
-- app.can_read_pod_members. Writes to assignments need ops or the home tutor, because a
-- FOR ALL policy also grants SELECT.

CREATE OR REPLACE FUNCTION app.can_read_pod(p_cohort uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog' AS $$
  SELECT EXISTS (SELECT 1 FROM app.cohort_memberships cm
                  WHERE cm.cohort_id = p_cohort AND cm.user_id = auth.uid())
      OR app.can_read_pod_members(p_cohort);
$$;
REVOKE EXECUTE ON FUNCTION app.can_read_pod(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.can_read_pod(uuid) TO authenticated, service_role;

CREATE OR REPLACE FUNCTION app.mark_attendance(p_session uuid, p_user uuid, p_status text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app', 'public' AS $$
BEGIN
  IF p_status NOT IN ('pending','attended','absent','excused') THEN
    RAISE EXCEPTION 'invalid attendance status: %', p_status;
  END IF;
  -- Attendance is the TUTOR's record. A student marking their own is a self-reported
  -- number, which is the defect app.ai_usage exists to avoid on the billing side.
  IF NOT (app.has_admin_capability('ops') OR EXISTS (
            SELECT 1 FROM app.cohort_session cs JOIN app.cohorts c ON c.id = cs.cohort_id
             WHERE cs.id = p_session AND (c.tutor_id = auth.uid() OR cs.lead_tutor_id = auth.uid()))) THEN
    RAISE EXCEPTION 'not permitted to mark attendance for this session';
  END IF;
  INSERT INTO app.cohort_session_attendance (cohort_session_id, user_id, status)
  VALUES (p_session, p_user, p_status)
  ON CONFLICT (cohort_session_id, user_id) DO UPDATE SET status = EXCLUDED.status;
END $$;

DROP POLICY IF EXISTS cohort_session_attendance_write_tutor_admin ON app.cohort_session_attendance;
CREATE POLICY cohort_session_attendance_write_tutor_admin ON app.cohort_session_attendance FOR ALL TO authenticated
  USING (app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.cohort_session cs JOIN app.cohorts c ON c.id = cs.cohort_id
                     WHERE cs.id = cohort_session_attendance.cohort_session_id
                       AND (c.tutor_id = auth.uid() OR cs.lead_tutor_id = auth.uid())))
  WITH CHECK (app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.cohort_session cs JOIN app.cohorts c ON c.id = cs.cohort_id
                     WHERE cs.id = cohort_session_attendance.cohort_session_id
                       AND (c.tutor_id = auth.uid() OR cs.lead_tutor_id = auth.uid())));

DROP POLICY IF EXISTS cohort_assignments_select ON app.cohort_assignments;
CREATE POLICY cohort_assignments_select ON app.cohort_assignments FOR SELECT TO authenticated
  USING (app.can_read_pod(cohort_id));
DROP POLICY IF EXISTS cohort_assignments_write ON app.cohort_assignments;
CREATE POLICY cohort_assignments_write ON app.cohort_assignments FOR ALL TO authenticated
  USING (app.has_admin_capability('ops') OR EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = cohort_id AND c.tutor_id = auth.uid()))
  WITH CHECK (app.has_admin_capability('ops') OR EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = cohort_id AND c.tutor_id = auth.uid()));

DROP POLICY IF EXISTS pod_norms_read ON app.pod_norms;
CREATE POLICY pod_norms_read ON app.pod_norms FOR SELECT TO authenticated
  USING (app.can_read_pod(cohort_id));

DROP POLICY IF EXISTS session_recordings_read ON storage.objects;
CREATE POLICY session_recordings_read ON storage.objects FOR SELECT TO authenticated
  USING (bucket_id = 'session-recordings' AND app.recording_cohort(name) IS NOT NULL
         AND app.can_read_pod(app.recording_cohort(name)));
