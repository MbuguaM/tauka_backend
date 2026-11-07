-- §161 (2026-11-07): an invited learner couldn't read their own live session.
--
-- video_session's "students see invited sessions" looked in video_session_participant, whose
-- "tutor reads session participants" looked back in video_session, so any learner's read of
-- video_session failed with 42P17 "infinite recursion detected in policy". The call screen reads
-- that row before joining, so Join failed for every invited learner. Found in the first LiveKit
-- test call (ALL-24).
--
-- Same cure as fix_user_profiles_policy_recursion: the lookups move into SECURITY DEFINER helpers,
-- which read the other table without running its policies. Who may read what is unchanged.
-- Tables are tauka-flutter's (video domain); filed here like §148-§150.

CREATE OR REPLACE FUNCTION app.is_video_session_participant(p_session uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog' AS $$
  SELECT EXISTS (SELECT 1 FROM app.video_session_participant p
                  WHERE p.session_id = p_session AND p.student_id = auth.uid());
$$;
REVOKE EXECUTE ON FUNCTION app.is_video_session_participant(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.is_video_session_participant(uuid) TO authenticated, service_role;

CREATE OR REPLACE FUNCTION app.is_video_session_tutor(p_session uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog' AS $$
  SELECT EXISTS (SELECT 1 FROM app.video_session v WHERE v.id = p_session AND v.tutor_id = auth.uid());
$$;
REVOKE EXECUTE ON FUNCTION app.is_video_session_tutor(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.is_video_session_tutor(uuid) TO authenticated, service_role;

DROP POLICY IF EXISTS "students see invited sessions" ON app.video_session;
CREATE POLICY "students see invited sessions" ON app.video_session FOR SELECT
  USING (app.is_video_session_participant(id));

DROP POLICY IF EXISTS "tutor reads session participants" ON app.video_session_participant;
CREATE POLICY "tutor reads session participants" ON app.video_session_participant FOR SELECT TO authenticated
  USING (app.is_admin() OR app.is_video_session_tutor(session_id));
