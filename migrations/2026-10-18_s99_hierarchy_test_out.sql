-- ============================================================================
-- §99 (2026-10-18)  Admin capabilities, tutor rungs, linked-tutor scope,
--                   test-out requests, rehearsal intakes (enrolment plan §2k)
-- ============================================================================
--
-- Approved by the developer 2026-10-18 (plan §2k "Approved"):
--   * Admin CAPABILITIES are a set, not a ladder: owner / ops / safeguarding /
--     content / finance. `owner` implies the others, except safeguarding once
--     there are two or more admins.
--   * With two or more admins, safeguarding is held by someone who holds neither
--     ops nor owner, and someone must hold it (the second half is Claude's
--     addition, flagged in the plan).
--   * Tutor RUNGS 1-4 (work permission), versioned. ops sets rungs 1-3; only an
--     owner promotes to or demotes from rung 4. Nobody sets their own.
--     `tutor_standing` stays the computed quality verdict and never promotes.
--   * SCOPE for judging a learner = the learner's own student_tutor_link, never
--     the course-wide branch of app.teaches_student().
--   * A TEST-OUT may be requested by the learner, a rung-4 tutor, an owner or an
--     ops admin; assessed by a linked tutor of rung >= 2 or an ops admin.
--   * Hidar 2019 is a REHEARSAL (a simulated pod); the real founding intake is
--     Dec 2026 / Jan 2027.
--
-- Ownership: admin_users is tauka-flutter's table (identity_access). The column,
-- policies and triggers on it are a cross-owner change, logged as CONFLICT-054
-- (SCHEMA_REQUEST, approved by the developer the same day). Everything else is
-- tauka-python's: tutor_rung (tutor_management), test_out_request and the
-- intake / enrolment_state columns (enrolment).

-- ── 1. Rehearsal intakes ──────────────────────────────────────────────────
ALTER TABLE app.intake ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'live';
ALTER TABLE app.intake DROP CONSTRAINT IF EXISTS intake_mode_check;
ALTER TABLE app.intake ADD CONSTRAINT intake_mode_check CHECK (mode IN ('live','rehearsal'));
COMMENT ON COLUMN app.intake.mode IS
  'live = learners are aimed at it. rehearsal = a simulated formation run by staff and '
  'test accounts; phase0_service never targets it (§99). Hidar 2019 is a rehearsal.';
UPDATE app.intake SET mode = 'rehearsal' WHERE ethiopic_month = 'hidar' AND ec_year = 2019;

-- ── 2. Admin capabilities ─────────────────────────────────────────────────
ALTER TABLE app.admin_users ADD COLUMN IF NOT EXISTS capabilities text[] NOT NULL DEFAULT '{}';
ALTER TABLE app.admin_users DROP CONSTRAINT IF EXISTS admin_users_capabilities_known;
ALTER TABLE app.admin_users ADD CONSTRAINT admin_users_capabilities_known
  CHECK (capabilities <@ ARRAY['owner','ops','safeguarding','content','finance']::text[]);
COMMENT ON COLUMN app.admin_users.capabilities IS
  'What this admin may do (§99): owner / ops / safeguarding / content / finance, '
  'combinable. Read through app.has_admin_capability(). `permissions` (jsonb) is '
  'superseded: nothing ever enforced it.';
-- The one admin today becomes the owner.
UPDATE app.admin_users SET capabilities = ARRAY['owner']
 WHERE deleted_at IS NULL AND capabilities = '{}';

CREATE OR REPLACE FUNCTION app.admin_can(p_actor uuid, p_cap text)
RETURNS boolean
LANGUAGE sql
STABLE SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
  SELECT EXISTS (
    SELECT 1 FROM app.admin_users a
     WHERE a.id = p_actor
       AND a.deleted_at IS NULL
       AND (p_cap = ANY (a.capabilities)
            OR ('owner' = ANY (a.capabilities)
                AND (p_cap <> 'safeguarding'
                     OR (SELECT count(*) FROM app.admin_users x WHERE x.deleted_at IS NULL) = 1)))
  );
$$;
COMMENT ON FUNCTION app.admin_can(uuid, text) IS
  'Does this admin hold the capability? owner implies all, except safeguarding once '
  'there are two or more admins. Service role only (it would reveal who is an admin); '
  'clients use app.has_admin_capability().';
REVOKE ALL ON FUNCTION app.admin_can(uuid, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION app.admin_can(uuid, text) TO service_role;

CREATE OR REPLACE FUNCTION app.has_admin_capability(p_cap text)
RETURNS boolean
LANGUAGE sql
STABLE SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$ SELECT app.admin_can(auth.uid(), p_cap); $$;
REVOKE ALL ON FUNCTION app.has_admin_capability(text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.has_admin_capability(text) TO authenticated, service_role;

-- Who may change admin_users from a client: an owner, never on their own row.
-- The service role (auth.uid() IS NULL) is the developer's console and is let
-- through; the invariants below still hold for it.
CREATE OR REPLACE FUNCTION app.admin_users_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  v_actor uuid := auth.uid();
BEGIN
  IF v_actor IS NOT NULL THEN
    IF NOT app.admin_can(v_actor, 'owner') THEN
      RAISE EXCEPTION 'admin_users: only an owner may add, change or remove admins'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (TG_OP = 'INSERT' AND NEW.id = v_actor)
       OR (TG_OP = 'DELETE' AND OLD.id = v_actor)
       OR (TG_OP = 'UPDATE' AND OLD.id = v_actor
           AND (NEW.capabilities IS DISTINCT FROM OLD.capabilities
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
                OR NEW.id IS DISTINCT FROM OLD.id)) THEN
      RAISE EXCEPTION 'admin_users: nobody changes their own admin capabilities'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_admin_users_guard ON app.admin_users;
CREATE TRIGGER trg_admin_users_guard
  BEFORE INSERT OR UPDATE OR DELETE ON app.admin_users
  FOR EACH ROW EXECUTE FUNCTION app.admin_users_guard();

-- Checked at COMMIT, so adding a second admin and handing out safeguarding can
-- happen in one transaction in either order.
CREATE OR REPLACE FUNCTION app.admin_users_invariants()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  n int;
BEGIN
  SELECT count(*) INTO n FROM app.admin_users WHERE deleted_at IS NULL;
  IF n = 0 THEN RETURN NULL; END IF;
  IF NOT EXISTS (SELECT 1 FROM app.admin_users
                  WHERE deleted_at IS NULL AND 'owner' = ANY (capabilities)) THEN
    RAISE EXCEPTION 'admin_users: at least one active admin must hold owner'
      USING ERRCODE = 'check_violation';
  END IF;
  IF n >= 2 THEN
    IF EXISTS (SELECT 1 FROM app.admin_users
                WHERE deleted_at IS NULL
                  AND 'safeguarding' = ANY (capabilities)
                  AND capabilities && ARRAY['ops','owner']::text[]) THEN
      RAISE EXCEPTION 'admin_users: with two or more admins, safeguarding must be held by someone who holds neither ops nor owner'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.admin_users
                    WHERE deleted_at IS NULL AND 'safeguarding' = ANY (capabilities)) THEN
      RAISE EXCEPTION 'admin_users: with two or more admins, someone other than ops must hold safeguarding'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS trg_admin_users_invariants ON app.admin_users;
CREATE CONSTRAINT TRIGGER trg_admin_users_invariants
  AFTER INSERT OR UPDATE OR DELETE ON app.admin_users
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION app.admin_users_invariants();

-- Every grant and revocation is a row. Nothing is overwritten.
CREATE TABLE IF NOT EXISTS app.admin_capability_event (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Plain uuid, no FK: the history outlives the admin row.
  admin_id            uuid NOT NULL,
  capabilities_before text[],
  capabilities_after  text[],
  active_before       boolean,
  active_after        boolean,
  -- NULL = the service role / developer console.
  changed_by          uuid,
  -- Optional, from set_config('app.change_reason', ..., true) in the same transaction.
  reason              text CHECK (reason IS NULL OR length(reason) <= 500),
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_capability_event_admin
  ON app.admin_capability_event (admin_id, created_at DESC);

CREATE OR REPLACE FUNCTION app.admin_capability_log()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  v_reason text := NULLIF(current_setting('app.change_reason', true), '');
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO app.admin_capability_event (admin_id, capabilities_after, active_after, changed_by, reason)
    VALUES (NEW.id, NEW.capabilities, NEW.deleted_at IS NULL, auth.uid(), v_reason);
  ELSIF TG_OP = 'DELETE' THEN
    INSERT INTO app.admin_capability_event (admin_id, capabilities_before, active_before, changed_by, reason)
    VALUES (OLD.id, OLD.capabilities, OLD.deleted_at IS NULL, auth.uid(), v_reason);
  ELSIF NEW.capabilities IS DISTINCT FROM OLD.capabilities
        OR (NEW.deleted_at IS NULL) IS DISTINCT FROM (OLD.deleted_at IS NULL) THEN
    INSERT INTO app.admin_capability_event
      (admin_id, capabilities_before, capabilities_after, active_before, active_after, changed_by, reason)
    VALUES (NEW.id, OLD.capabilities, NEW.capabilities, OLD.deleted_at IS NULL,
            NEW.deleted_at IS NULL, auth.uid(), v_reason);
  END IF;
  RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS trg_admin_capability_log ON app.admin_users;
CREATE TRIGGER trg_admin_capability_log
  AFTER INSERT OR UPDATE OR DELETE ON app.admin_users
  FOR EACH ROW EXECUTE FUNCTION app.admin_capability_log();

-- Shared by the two append-only ledgers in this migration.
CREATE OR REPLACE FUNCTION app.refuse_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION '%: rows are append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;
DROP TRIGGER IF EXISTS trg_admin_capability_event_append_only ON app.admin_capability_event;
CREATE TRIGGER trg_admin_capability_event_append_only
  BEFORE UPDATE OR DELETE ON app.admin_capability_event
  FOR EACH ROW EXECUTE FUNCTION app.refuse_change();

ALTER TABLE app.admin_capability_event ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS admin_capability_event_admin_read ON app.admin_capability_event;
CREATE POLICY admin_capability_event_admin_read ON app.admin_capability_event
  FOR SELECT TO authenticated USING (app.is_admin());
REVOKE ALL ON app.admin_capability_event FROM anon, authenticated;
GRANT SELECT ON app.admin_capability_event TO authenticated;
GRANT ALL ON app.admin_capability_event TO service_role;

-- admin_users policies: any admin used to be able to add or edit any admin.
DROP POLICY IF EXISTS admin_users_all_admin    ON app.admin_users;
DROP POLICY IF EXISTS admins_manage_admins     ON app.admin_users;
DROP POLICY IF EXISTS admin_users_select_admin ON app.admin_users;
CREATE POLICY admin_users_select_admin ON app.admin_users
  FOR SELECT TO authenticated USING (app.is_admin());
CREATE POLICY admin_users_insert_owner ON app.admin_users
  FOR INSERT TO authenticated WITH CHECK (app.has_admin_capability('owner'));
CREATE POLICY admin_users_update_owner ON app.admin_users
  FOR UPDATE TO authenticated
  USING (app.has_admin_capability('owner')) WITH CHECK (app.has_admin_capability('owner'));
CREATE POLICY admin_users_delete_owner ON app.admin_users
  FOR DELETE TO authenticated USING (app.has_admin_capability('owner'));
REVOKE ALL ON app.admin_users FROM anon;

-- ── 3. Tutor rungs (tutor_management) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.tutor_rung (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- RESTRICT, like tutor_standing: a tutor leaving must not erase the record.
  tutor_id       uuid NOT NULL REFERENCES app.tutor(id) ON DELETE RESTRICT,
  -- 1 Phase 0 reviewer · 2 session tutor · 3 pod tutor · 4 senior / anchor
  rung           int  NOT NULL CHECK (rung BETWEEN 1 AND 4),
  effective_from timestamptz NOT NULL DEFAULT now(),
  -- NULL = current. Closed by the next promotion or demotion, never deleted.
  effective_to   timestamptz,
  -- The admin who set it. Plain uuid, like other admin references.
  set_by         uuid NOT NULL,
  reason         text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tutor_rung_window CHECK (effective_to IS NULL OR effective_to > effective_from),
  CONSTRAINT tutor_rung_not_self CHECK (set_by <> tutor_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_rung_current
  ON app.tutor_rung (tutor_id) WHERE effective_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_tutor_rung_history
  ON app.tutor_rung (tutor_id, effective_from DESC);

-- The only change a row may ever take is being closed, once.
CREATE OR REPLACE FUNCTION app.tutor_rung_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'tutor_rung: rows are never deleted' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.effective_to IS NOT NULL
     OR NEW.effective_to IS NULL
     OR (NEW.id, NEW.tutor_id, NEW.rung, NEW.effective_from, NEW.set_by, NEW.reason, NEW.created_at)
        IS DISTINCT FROM
        (OLD.id, OLD.tutor_id, OLD.rung, OLD.effective_from, OLD.set_by, OLD.reason, OLD.created_at) THEN
    RAISE EXCEPTION 'tutor_rung: a row can only be closed (effective_to), once'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_tutor_rung_guard ON app.tutor_rung;
CREATE TRIGGER trg_tutor_rung_guard
  BEFORE UPDATE OR DELETE ON app.tutor_rung
  FOR EACH ROW EXECUTE FUNCTION app.tutor_rung_guard();

CREATE OR REPLACE FUNCTION app.tutor_rung_of(p_tutor uuid)
RETURNS int
LANGUAGE sql
STABLE SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
  SELECT rung FROM app.tutor_rung WHERE tutor_id = p_tutor AND effective_to IS NULL;
$$;
REVOKE ALL ON FUNCTION app.tutor_rung_of(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.tutor_rung_of(uuid) TO authenticated, service_role;

-- The one write path. Called by tauka-python with the caller it verified from
-- the JWT as p_actor; service role only, so a client can't name someone else.
CREATE OR REPLACE FUNCTION app.set_tutor_rung(p_actor uuid, p_tutor uuid, p_rung int, p_reason text)
RETURNS app.tutor_rung
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  v_cur  app.tutor_rung;
  v_new  app.tutor_rung;
  v_now  timestamptz := clock_timestamp();
BEGIN
  IF p_actor IS NULL OR p_actor = p_tutor THEN
    RAISE EXCEPTION 'tutor_rung: nobody sets their own rung' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_rung IS NULL OR p_rung NOT BETWEEN 1 AND 4 THEN
    RAISE EXCEPTION 'tutor_rung: rung must be 1-4' USING ERRCODE = 'check_violation';
  END IF;
  -- Serialises two admins acting on the same tutor.
  PERFORM 1 FROM app.tutor WHERE id = p_tutor AND deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'tutor_rung: no such tutor' USING ERRCODE = 'no_data_found';
  END IF;
  SELECT * INTO v_cur FROM app.tutor_rung WHERE tutor_id = p_tutor AND effective_to IS NULL;

  IF p_rung = 4 OR v_cur.rung = 4 THEN
    IF NOT app.admin_can(p_actor, 'owner') THEN
      RAISE EXCEPTION 'tutor_rung: only an owner may promote to or demote from rung 4'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  ELSIF NOT app.admin_can(p_actor, 'ops') THEN
    RAISE EXCEPTION 'tutor_rung: only an ops admin may set rungs 1-3'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  IF v_cur.id IS NOT NULL AND v_cur.rung = p_rung THEN
    RETURN v_cur;   -- idempotent
  END IF;
  IF v_cur.id IS NOT NULL THEN
    UPDATE app.tutor_rung SET effective_to = v_now WHERE id = v_cur.id;
  END IF;
  INSERT INTO app.tutor_rung (tutor_id, rung, effective_from, set_by, reason)
  VALUES (p_tutor, p_rung, v_now, p_actor, btrim(p_reason))
  RETURNING * INTO v_new;
  RETURN v_new;
END;
$$;
REVOKE ALL ON FUNCTION app.set_tutor_rung(uuid, uuid, int, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION app.set_tutor_rung(uuid, uuid, int, text) TO service_role;

ALTER TABLE app.tutor_rung ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tutor_rung_select_own ON app.tutor_rung;
CREATE POLICY tutor_rung_select_own ON app.tutor_rung
  FOR SELECT TO authenticated USING (tutor_id = auth.uid());
DROP POLICY IF EXISTS tutor_rung_admin_read ON app.tutor_rung;
CREATE POLICY tutor_rung_admin_read ON app.tutor_rung
  FOR SELECT TO authenticated USING (app.is_admin());
REVOKE ALL ON app.tutor_rung FROM anon, authenticated;
GRANT SELECT ON app.tutor_rung TO authenticated;
GRANT ALL ON app.tutor_rung TO service_role;

COMMENT ON TABLE app.tutor_rung IS
  'What work a tutor may do (E-10, §99): 1 Phase 0 reviewer, 2 session tutor, 3 pod tutor, '
  '4 senior/anchor. Versioned; written only by app.set_tutor_rung (ops: 1-3, owner: 4). '
  'tutor_standing informs a promotion and never makes one.';

-- ── 4. Scope: the learner''s own tutors ───────────────────────────────────
CREATE OR REPLACE FUNCTION app.is_linked_tutor(p_student uuid)
RETURNS boolean
LANGUAGE sql
STABLE SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
  SELECT EXISTS (
    SELECT 1 FROM app.student_tutor_link
     WHERE tutor_id = auth.uid() AND student_id = p_student AND state = 'active'
  );
$$;
COMMENT ON FUNCTION app.is_linked_tutor(uuid) IS
  'The caller is one of this learner''s own tutors (home or guest, active link). Use it '
  'for anything that JUDGES a learner (test-outs, exit assessments). app.teaches_student() '
  'stays for read access; its course branch makes every tutor on a course "teach" everyone.';
REVOKE ALL ON FUNCTION app.is_linked_tutor(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.is_linked_tutor(uuid) TO authenticated, service_role;

-- ── 5. Test-outs (enrolment) ──────────────────────────────────────────────
ALTER TABLE app.enrolment_state ADD COLUMN IF NOT EXISTS phase0_completed_via text;
ALTER TABLE app.enrolment_state DROP CONSTRAINT IF EXISTS enrolment_state_completed_via;
ALTER TABLE app.enrolment_state ADD CONSTRAINT enrolment_state_completed_via CHECK (
  (phase0_completed_via IS NULL) = (phase0_completed_at IS NULL)
  AND (phase0_completed_via IS NULL OR phase0_completed_via IN ('lessons','test_out')));
COMMENT ON COLUMN app.enrolment_state.phase0_completed_via IS
  'lessons = Phase 0 unit 2 completed (progress tab verdict); test_out = passed a heritage '
  'test-out (§99). Set with phase0_completed_at and, like it, never changed.';

CREATE TABLE IF NOT EXISTS app.test_out_request (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id        uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  course_id         uuid NOT NULL REFERENCES app.course(id)  ON DELETE CASCADE,
  requested_by      uuid NOT NULL,
  requested_as      text NOT NULL CHECK (requested_as IN ('learner','senior_tutor','ops','owner')),
  assessor_tutor_id uuid REFERENCES app.tutor(id) ON DELETE RESTRICT,
  -- The guest link made so the assessor is linked, if one had to be made.
  -- Ended when the request closes, so the access lasts only as long as the job.
  assessor_link_id  uuid REFERENCES app.student_tutor_link(id) ON DELETE SET NULL,
  status            text NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested','assigned','passed','not_yet','cancelled')),
  -- Codes only, never free text (what to practise, shown to the learner).
  gap_codes         text[] NOT NULL DEFAULT '{}' CHECK (gap_codes <@ ARRAY[
                      'fidel_recognition','vowel_orders','ejectives','gemination','reading_fluency']::text[]),
  result_by         uuid,
  result_at         timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT test_out_assessor_when_assigned CHECK (
    status NOT IN ('assigned','passed','not_yet') OR assessor_tutor_id IS NOT NULL),
  CONSTRAINT test_out_result_fields CHECK (
    (status IN ('passed','not_yet')) = (result_at IS NOT NULL AND result_by IS NOT NULL))
);
-- One open request per learner per course.
CREATE UNIQUE INDEX IF NOT EXISTS idx_test_out_one_open
  ON app.test_out_request (student_id, course_id) WHERE status IN ('requested','assigned');
CREATE INDEX IF NOT EXISTS idx_test_out_assessor
  ON app.test_out_request (assessor_tutor_id) WHERE status = 'assigned';

CREATE OR REPLACE FUNCTION app.test_out_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status NOT IN ('requested','assigned') THEN
      RAISE EXCEPTION 'test_out_request: a new request starts requested or assigned' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status IN ('passed','not_yet','cancelled') THEN
    RAISE EXCEPTION 'test_out_request: % is final', OLD.status USING ERRCODE = 'check_violation';
  END IF;
  IF NOT (NEW.status = OLD.status
          OR (OLD.status = 'requested' AND NEW.status IN ('assigned','cancelled'))
          OR (OLD.status = 'assigned'  AND NEW.status IN ('passed','not_yet','cancelled'))) THEN
    RAISE EXCEPTION 'test_out_request: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.student_id, NEW.course_id, NEW.requested_by, NEW.requested_as, NEW.created_at)
     IS DISTINCT FROM (OLD.student_id, OLD.course_id, OLD.requested_by, OLD.requested_as, OLD.created_at) THEN
    RAISE EXCEPTION 'test_out_request: who asked, for whom and when never change' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_test_out_guard ON app.test_out_request;
CREATE TRIGGER trg_test_out_guard
  BEFORE INSERT OR UPDATE ON app.test_out_request
  FOR EACH ROW EXECUTE FUNCTION app.test_out_guard();
DROP TRIGGER IF EXISTS trg_test_out_updated_at ON app.test_out_request;
CREATE TRIGGER trg_test_out_updated_at BEFORE UPDATE ON app.test_out_request
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE app.test_out_request ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS test_out_select_student ON app.test_out_request;
CREATE POLICY test_out_select_student ON app.test_out_request
  FOR SELECT TO authenticated USING (student_id = auth.uid());
DROP POLICY IF EXISTS test_out_select_assessor ON app.test_out_request;
CREATE POLICY test_out_select_assessor ON app.test_out_request
  FOR SELECT TO authenticated USING (assessor_tutor_id = auth.uid());
DROP POLICY IF EXISTS test_out_admin_read ON app.test_out_request;
CREATE POLICY test_out_admin_read ON app.test_out_request
  FOR SELECT TO authenticated USING (app.is_admin());
REVOKE ALL ON app.test_out_request FROM anon, authenticated;
GRANT SELECT ON app.test_out_request TO authenticated;
GRANT ALL ON app.test_out_request TO service_role;

COMMENT ON TABLE app.test_out_request IS
  'Heritage test-out of Phase 0 (plan 1.6/1.7, §2k). Requested by the learner, a rung-4 '
  'tutor, an owner or an ops admin; assessed live by a linked tutor of rung >= 2 or an ops '
  'admin. No recording is stored (L-004). Passed -> enrolment_state.phase0_completed_via = '
  'test_out. Written by tauka-python only.';
-- Applied to production 2026-10-18 as migration s99_hierarchy_test_out.
