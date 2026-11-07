-- §162 (2026-11-07): a pod's tutor can see their learners' first names.
--
-- The tutor's Practice tab lists their pods and learners, to leave notes and set work. Pod
-- tutors could read the memberships (§148, app.can_read_pod_members) but not the learners'
-- user_profiles rows, so every name came back empty. This returns the first name only, and only
-- for a pod the caller may already read; no wider user_profiles policy is added.
-- Tables are tauka-flutter's; filed here like §148-§150 and §161.

CREATE OR REPLACE FUNCTION app.pod_member_names(p_cohort uuid)
RETURNS TABLE (user_id uuid, first_name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog' AS $$
  SELECT m.user_id, p.first_name
    FROM app.cohort_memberships m
    JOIN app.user_profiles p ON p.id = m.user_id
   WHERE m.cohort_id = p_cohort
     AND m.left_at IS NULL
     AND m.status = 'active'
     AND app.can_read_pod_members(p_cohort);
$$;
REVOKE EXECUTE ON FUNCTION app.pod_member_names(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.pod_member_names(uuid) TO authenticated, service_role;
