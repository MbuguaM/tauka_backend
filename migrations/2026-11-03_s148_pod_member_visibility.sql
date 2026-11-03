-- §148 Who can see a pod's members (2026-11-03, developer's rule). Needs §147.
--
-- A learner sees their own membership. Beyond that: the pod's home tutor, a tutor
-- leading one of its sessions, a rung-4 (senior) tutor, and an admin with the ops
-- capability. An owner holds ops through app.admin_can. Other admins (safeguarding,
-- content, finance) no longer see rosters through this table. tauka-python uses the
-- service role and isn't affected.

CREATE OR REPLACE FUNCTION app.can_read_pod_members(p_cohort uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog' AS $$
  SELECT EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = p_cohort AND c.tutor_id = auth.uid())
      OR EXISTS (SELECT 1 FROM app.cohort_session cs
                  WHERE cs.cohort_id = p_cohort AND cs.lead_tutor_id = auth.uid() AND cs.status <> 'cancelled')
      OR (app.tutor_rung_of(auth.uid()) = 4
          AND EXISTS (SELECT 1 FROM app.tutor t WHERE t.id = auth.uid() AND t.status = 'active'))
      OR app.admin_can(auth.uid(), 'ops');
$$;
REVOKE EXECUTE ON FUNCTION app.can_read_pod_members(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.can_read_pod_members(uuid) TO authenticated, service_role;

DROP POLICY IF EXISTS cohort_memberships_select_own_or_tutor_admin ON app.cohort_memberships;
CREATE POLICY cohort_memberships_select ON app.cohort_memberships FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.can_read_pod_members(cohort_id));

-- A FOR ALL policy also grants SELECT, so it has to follow the same rule.
DROP POLICY IF EXISTS cohort_memberships_write_admin ON app.cohort_memberships;
CREATE POLICY cohort_memberships_write ON app.cohort_memberships FOR ALL TO authenticated
  USING (app.has_admin_capability('ops')) WITH CHECK (app.has_admin_capability('ops'));
