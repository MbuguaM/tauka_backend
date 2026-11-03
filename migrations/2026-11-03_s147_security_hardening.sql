-- §147 Phase 1 security fixes (2026-11-03): DB-1, DB-4, DB-5 (search_path part), DB-6, DB-7.
-- See release-resolution-plan.md section 9. Dry-run in a rolled-back transaction before applying.

-- ── DB-1: clients can't change what they're paying for (closes FC-004) ──────────────────────
-- Copies guard_user_profile_privileges. The service role has no auth.uid(), so billing still writes.
CREATE OR REPLACE FUNCTION app.guard_student_privileges() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app', 'public' AS $$
BEGIN
  IF auth.uid() IS NULL OR app.is_admin() THEN RETURN NEW; END IF;
  IF TG_OP = 'INSERT' THEN
    NEW.tier := 'free_tier'; NEW.tier_source := 'self'; NEW.original_tier := NULL;
    NEW.stripe_customer_id := NULL; NEW.stripe_subscription_id := NULL;
    NEW.subscription_status := 'none'; NEW.current_period_end := NULL; NEW.active_gift_id := NULL;
    NEW.account_status := 'active'; NEW.deleted_at := NULL; NEW.is_con_creator := NULL;
    NEW.lessons_completed := 0; NEW.current_streak := 0; NEW.ai_conversations := 0;
    NEW.cohort_sessions_attended := 0; NEW.assessed_level := NULL; NEW.last_unit_milestone := 0;
    NEW.streak_14_notified := false; NEW.first_ai_convo_notified := false;
    NEW.first_cohort_notified := false; NEW.last_notified_level := NULL;
    RETURN NEW;
  END IF;
  NEW.id := OLD.id; NEW.created_at := OLD.created_at;
  NEW.tier := OLD.tier; NEW.tier_source := OLD.tier_source; NEW.original_tier := OLD.original_tier;
  NEW.stripe_customer_id := OLD.stripe_customer_id; NEW.stripe_subscription_id := OLD.stripe_subscription_id;
  NEW.subscription_status := OLD.subscription_status; NEW.current_period_end := OLD.current_period_end;
  NEW.active_gift_id := OLD.active_gift_id; NEW.account_status := OLD.account_status;
  NEW.deleted_at := OLD.deleted_at; NEW.is_con_creator := OLD.is_con_creator;
  NEW.lessons_completed := OLD.lessons_completed; NEW.current_streak := OLD.current_streak;
  NEW.ai_conversations := OLD.ai_conversations; NEW.cohort_sessions_attended := OLD.cohort_sessions_attended;
  NEW.assessed_level := OLD.assessed_level; NEW.last_unit_milestone := OLD.last_unit_milestone;
  NEW.streak_14_notified := OLD.streak_14_notified; NEW.first_ai_convo_notified := OLD.first_ai_convo_notified;
  NEW.first_cohort_notified := OLD.first_cohort_notified; NEW.last_notified_level := OLD.last_notified_level;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_guard_student_privileges ON app.student;
CREATE TRIGGER trg_guard_student_privileges BEFORE INSERT OR UPDATE ON app.student
  FOR EACH ROW EXECUTE FUNCTION app.guard_student_privileges();

-- ── DB-4: functions that trusted an id the caller supplied ──────────────────────────────────
-- auth.uid() IS NULL is the service role; anon can no longer reach these (see below).
CREATE OR REPLACE FUNCTION app.add_user_xp(p_user_id uuid, p_amount integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
BEGIN
  IF auth.uid() IS NOT NULL AND p_user_id IS DISTINCT FROM auth.uid() THEN
    RAISE EXCEPTION 'add_user_xp: may only add your own XP' USING ERRCODE = '42501';
  END IF;
  -- The largest single award today is an achievement's 200.
  IF auth.uid() IS NOT NULL AND (p_amount IS NULL OR p_amount < 1 OR p_amount > 200) THEN
    RAISE EXCEPTION 'add_user_xp: amount out of range' USING ERRCODE = '22023';
  END IF;
  UPDATE app.user_profiles SET xp_total = xp_total + p_amount, xp_week = xp_week + p_amount WHERE id = p_user_id;
END $$;

CREATE OR REPLACE FUNCTION app.get_course_completion(p_user_id uuid, p_course_id uuid) RETURNS numeric
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v numeric;
BEGIN
  IF auth.uid() IS NOT NULL AND p_user_id IS DISTINCT FROM auth.uid()
     AND NOT app.teaches_student(p_user_id) AND NOT app.is_admin() THEN
    RAISE EXCEPTION 'get_course_completion: not permitted' USING ERRCODE = '42501';
  END IF;
  SELECT COALESCE(ROUND(COUNT(*) FILTER (WHERE status = 'completed') * 100.0 / NULLIF(COUNT(*), 0), 2), 0) INTO v
    FROM app.unit_progress WHERE user_id = p_user_id AND course_id = p_course_id;
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION app.upsert_conversation_participant(p_conversation_id uuid, p_user_id uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v_participant_id uuid;
BEGIN
  -- Only someone already in the conversation (or an admin) adds people. The one
  -- exception is the creator adding themselves to a conversation with nobody in it.
  IF auth.uid() IS NOT NULL AND NOT app.is_admin() AND NOT EXISTS (
       SELECT 1 FROM app.conversation_participants
        WHERE conversation_id = p_conversation_id AND user_id = auth.uid() AND deleted_at IS NULL)
     AND NOT (p_user_id = auth.uid() AND NOT EXISTS (
       SELECT 1 FROM app.conversation_participants WHERE conversation_id = p_conversation_id)) THEN
    RAISE EXCEPTION 'upsert_conversation_participant: not permitted' USING ERRCODE = '42501';
  END IF;
  INSERT INTO app.conversation_participants (conversation_id, user_id) VALUES (p_conversation_id, p_user_id)
  ON CONFLICT (conversation_id, user_id) DO UPDATE SET deleted_at = NULL
  RETURNING id INTO v_participant_id;
  RETURN v_participant_id;
END $$;

CREATE OR REPLACE FUNCTION app.check_rate_limit(p_user_id uuid, p_action_type text, p_max_actions integer, p_window_minutes integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v_current_count integer; v_window_start timestamptz;
BEGIN
  IF auth.uid() IS NOT NULL AND p_user_id IS DISTINCT FROM auth.uid() THEN
    RAISE EXCEPTION 'check_rate_limit: may only check your own limit' USING ERRCODE = '42501';
  END IF;
  SELECT action_count, window_start INTO v_current_count, v_window_start
    FROM app.rate_limits WHERE user_id = p_user_id AND action_type = p_action_type;
  IF v_current_count IS NULL OR v_window_start < (now() - (p_window_minutes || ' minutes')::interval) THEN
    INSERT INTO app.rate_limits (user_id, action_type, action_count, window_start)
    VALUES (p_user_id, p_action_type, 1, now())
    ON CONFLICT (user_id, action_type) DO UPDATE SET action_count = 1, window_start = now();
    RETURN true;
  END IF;
  IF v_current_count >= p_max_actions THEN RETURN false; END IF;
  UPDATE app.rate_limits SET action_count = action_count + 1 WHERE user_id = p_user_id AND action_type = p_action_type;
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION app.create_class_with_teacher(p_name text, p_description text, p_creator_id uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v_class_id uuid;
BEGIN
  IF auth.uid() IS NOT NULL AND (p_creator_id IS DISTINCT FROM auth.uid() OR NOT app.is_tutor_or_admin()) THEN
    RAISE EXCEPTION 'create_class_with_teacher: not permitted' USING ERRCODE = '42501';
  END IF;
  INSERT INTO app.classes (name, description, created_by) VALUES (p_name, p_description, p_creator_id)
  RETURNING id INTO v_class_id;
  INSERT INTO app.class_members (class_id, user_id, role) VALUES (v_class_id, p_creator_id, 'teacher');
  RETURN v_class_id;
END $$;

CREATE OR REPLACE FUNCTION app.create_subclass_notification(p_subclass_id uuid, p_sender_id uuid, p_title text, p_content text, p_is_urgent boolean)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v_notification_id uuid; v_is_admin boolean;
BEGIN
  IF auth.uid() IS NOT NULL AND p_sender_id IS DISTINCT FROM auth.uid() THEN
    RAISE EXCEPTION 'create_subclass_notification: may only send as yourself' USING ERRCODE = '42501';
  END IF;
  SELECT EXISTS (SELECT 1 FROM app.admin_users WHERE id = p_sender_id AND deleted_at IS NULL) INTO v_is_admin;
  IF NOT v_is_admin AND NOT EXISTS (
    SELECT 1 FROM app.class_members
     WHERE user_id = p_sender_id AND subclass_id = p_subclass_id AND role = 'teacher' AND deleted_at IS NULL) THEN
    RAISE EXCEPTION 'Only teachers in this subclass or admins can send notifications';
  END IF;
  INSERT INTO app.notifications (subclass_id, sender_id, title, content, is_urgent)
  VALUES (p_subclass_id, p_sender_id, p_title, p_content, p_is_urgent) RETURNING id INTO v_notification_id;
  INSERT INTO app.notification_receipts (notification_id, user_id)
  SELECT v_notification_id, user_id FROM app.class_members
   WHERE subclass_id = p_subclass_id AND role = 'student' AND deleted_at IS NULL;
  RETURN v_notification_id;
END $$;

CREATE OR REPLACE FUNCTION app.students_share_class(student1_id uuid, student2_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v boolean;
BEGIN
  -- The caller may only ask about themselves. get_or_create_student_conversation calls
  -- this after its own check, with the same two ids.
  IF auth.uid() IS NOT NULL AND auth.uid() NOT IN (student1_id, student2_id) AND NOT app.is_admin() THEN
    RAISE EXCEPTION 'students_share_class: not permitted' USING ERRCODE = '42501';
  END IF;
  SELECT EXISTS (SELECT 1 FROM app.class_members cm1 JOIN app.class_members cm2 ON cm1.class_id = cm2.class_id
                  WHERE cm1.user_id = student1_id AND cm2.user_id = student2_id
                    AND cm1.role = 'student' AND cm2.role = 'student'
                    AND cm1.deleted_at IS NULL AND cm2.deleted_at IS NULL) INTO v;
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION app.get_or_create_student_conversation(student1_id uuid, student2_id uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'app' AS $$
DECLARE v_conversation_id uuid;
BEGIN
  IF auth.uid() IS NOT NULL AND auth.uid() NOT IN (student1_id, student2_id) THEN
    RAISE EXCEPTION 'get_or_create_student_conversation: not permitted' USING ERRCODE = '42501';
  END IF;
  IF NOT app.students_share_class(student1_id, student2_id) THEN
    RAISE EXCEPTION 'Students do not share any classes';
  END IF;
  SELECT c.id INTO v_conversation_id
    FROM app.conversations c
    JOIN app.conversation_participants cp1 ON c.id = cp1.conversation_id
    JOIN app.conversation_participants cp2 ON c.id = cp2.conversation_id
   WHERE c.type = 'student_to_student' AND c.deleted_at IS NULL
     AND cp1.user_id = student1_id AND cp2.user_id = student2_id
     AND cp1.deleted_at IS NULL AND cp2.deleted_at IS NULL
   LIMIT 1;
  IF v_conversation_id IS NULL THEN
    INSERT INTO app.conversations (type) VALUES ('student_to_student') RETURNING id INTO v_conversation_id;
    INSERT INTO app.conversation_participants (conversation_id, user_id)
    VALUES (v_conversation_id, student1_id), (v_conversation_id, student2_id);
  END IF;
  RETURN v_conversation_id;
END $$;

-- Reads app.tutor_sessions, which production doesn't have; nothing calls it.
DROP FUNCTION IF EXISTS app.get_tutor_monthly_earnings(uuid, integer, integer);

-- p_by is the acting user and was trusted as given, so this was never safe to expose.
-- tauka-python calls it with the service role, as it does book and cancel.
REVOKE EXECUTE ON FUNCTION app.reschedule_one_to_one(uuid, timestamptz, uuid, uuid, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION app.reschedule_one_to_one(uuid, timestamptz, uuid, uuid, uuid) TO service_role;

-- Default EXECUTE comes through PUBLIC, so revoking it means re-granting who keeps it.
-- anon keeps only what public-role policies and public storage reads evaluate.
DO $$
DECLARE r record; v_trigger boolean; v_authn boolean;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig, p.oid, p.proname, n.nspname, t.typname
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_type t ON t.oid = p.prorettype
     WHERE n.nspname IN ('app', 'public') AND p.prokind = 'f'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
  LOOP
    CONTINUE WHEN r.nspname = 'app' AND r.proname IN
      ('is_admin', 'is_tutor', 'teaches_student', 'can_read_message', 'can_read_course_object');
    v_trigger := r.typname = 'trigger';
    v_authn := has_function_privilege('authenticated', r.oid, 'EXECUTE');
    IF NOT has_function_privilege('anon', r.oid, 'EXECUTE') AND NOT v_trigger THEN CONTINUE; END IF;
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, anon', r.sig);
    IF v_trigger THEN
      EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM authenticated', r.sig);
    ELSIF v_authn THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO authenticated', r.sig);
    END IF;
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
  END LOOP;
END $$;
-- So a new function doesn't bring the problem back.
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE EXECUTE ON FUNCTIONS FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT EXECUTE ON FUNCTIONS TO authenticated, service_role;

-- ── DB-5 (the code part): pin search_path. The Postgres upgrade and leaked-password
-- protection are dashboard steps for the developer. ─────────────────────────────────────────
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig, n.nspname
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('app', 'public') AND p.prokind = 'f'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
       AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(p.proconfig, '{}')) c WHERE c LIKE 'search_path=%')
  LOOP
    EXECUTE format('ALTER FUNCTION %s SET search_path = %s', r.sig,
                   CASE r.nspname WHEN 'app' THEN 'app, public' ELSE 'public, app' END);
  END LOOP;
END $$;

-- ── DB-6: two cron jobs that call nothing and fail thousands of times a week ────────────────
SELECT cron.unschedule(jobname) FROM cron.job WHERE jobname IN ('check-sessions-no-show', 'session-reminders');

-- ── DB-7: read rules with no subject (the CONFLICT-041 pattern) ─────────────────────────────
DROP POLICY IF EXISTS cohort_assignments_select_member_or_tutor_admin ON app.cohort_assignments;
CREATE POLICY cohort_assignments_select ON app.cohort_assignments FOR SELECT TO authenticated
  USING (app.can_read_cohort(cohort_id));
DROP POLICY IF EXISTS cohort_assignments_write_tutor_or_admin ON app.cohort_assignments;
CREATE POLICY cohort_assignments_write ON app.cohort_assignments FOR ALL TO authenticated
  USING (app.is_admin() OR EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = cohort_id AND c.tutor_id = auth.uid()))
  WITH CHECK (app.is_admin() OR EXISTS (SELECT 1 FROM app.cohorts c WHERE c.id = cohort_id AND c.tutor_id = auth.uid()));

-- Found alongside DB-7: any tutor could read any learner's AI practice and achievements.
DROP POLICY IF EXISTS ai_ps_select_own_or_tutor_or_admin ON app.ai_practice_session;
CREATE POLICY ai_ps_select_own_or_tutor_or_admin ON app.ai_practice_session FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.teaches_student(user_id) OR app.is_admin());
DROP POLICY IF EXISTS ai_pt_select_via_session_owner ON app.ai_practice_turn;
CREATE POLICY ai_pt_select_via_session_owner ON app.ai_practice_turn FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM app.ai_practice_session s WHERE s.id = session_id
                  AND (s.user_id = auth.uid() OR app.teaches_student(s.user_id) OR app.is_admin())));
DROP POLICY IF EXISTS user_achievements_select_own_or_tutor_admin ON app.user_achievements;
CREATE POLICY user_achievements_select_own_or_tutor_admin ON app.user_achievements FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.teaches_student(user_id) OR app.is_admin());
