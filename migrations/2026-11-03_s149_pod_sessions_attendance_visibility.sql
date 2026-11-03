-- §149 Pod sessions and attendance follow the §148 rule (2026-11-03, developer). Needs §148.
--
-- Sessions: the pod's members keep their schedule; otherwise app.can_read_pod_members.
-- Attendance: a learner reads only their own row, never a podmate's; otherwise the same rule.
-- FOR ALL policies also grant SELECT, so writes move from any admin to ops (owner holds it).
-- Who may write stays the home tutor; a guest lead still can't (see FC-048).
-- Cohort details gain the same readers, or a guest or senior tutor would see sessions with
-- no pod attached. They hold no learner data, so existing readers keep them.

DROP POLICY IF EXISTS cohort_session_select ON app.cohort_session;
CREATE POLICY cohort_session_select ON app.cohort_session FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM app.cohort_memberships cm
                  WHERE cm.cohort_id = cohort_session.cohort_id AND cm.user_id = auth.uid())
         OR app.can_read_pod_members(cohort_id));

DROP POLICY IF EXISTS cohort_session_write_tutor_admin ON app.cohort_session;
CREATE POLICY cohort_session_write_tutor_admin ON app.cohort_session FOR ALL TO authenticated
  USING (app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = cohort_session.cohort_id AND c.tutor_id = auth.uid()))
  WITH CHECK (app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = cohort_session.cohort_id AND c.tutor_id = auth.uid()));

DROP POLICY IF EXISTS cohort_session_attendance_select ON app.cohort_session_attendance;
CREATE POLICY cohort_session_attendance_select ON app.cohort_session_attendance FOR SELECT TO authenticated
  USING (user_id = auth.uid()
         OR EXISTS (SELECT 1 FROM app.cohort_session cs
                     WHERE cs.id = cohort_session_attendance.cohort_session_id
                       AND app.can_read_pod_members(cs.cohort_id)));

DROP POLICY IF EXISTS cohort_session_attendance_write_tutor_admin ON app.cohort_session_attendance;
CREATE POLICY cohort_session_attendance_write_tutor_admin ON app.cohort_session_attendance FOR ALL TO authenticated
  USING (app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.cohort_session cs JOIN app.cohorts c ON c.id = cs.cohort_id
                     WHERE cs.id = cohort_session_attendance.cohort_session_id AND c.tutor_id = auth.uid()))
  WITH CHECK (app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.cohort_session cs JOIN app.cohorts c ON c.id = cs.cohort_id
                     WHERE cs.id = cohort_session_attendance.cohort_session_id AND c.tutor_id = auth.uid()));

CREATE POLICY cohorts_select_pod_readers ON app.cohorts FOR SELECT TO authenticated
  USING (app.can_read_pod_members(id));
