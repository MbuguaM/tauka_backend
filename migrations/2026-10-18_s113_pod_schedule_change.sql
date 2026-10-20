-- ============================================================================
-- §113 (2026-10-18)  Moving a pod's time, with everyone's consent
--                    (enrolment plan 4.5, E-14; CONFLICT-056; FC-042)
-- ============================================================================
--
-- cohort_matching domain (tauka-python), plus one fix to tauka-flutter's
-- app.generate_cohort_sessions (CONFLICT-056, approved by the developer
-- 2026-10-18).
--
-- E-14: "No ad-hoc moves. Allow (a) a checkpoint slot change with all members'
-- consent and (b) a dated temporary-slot window. Both are admin-applied and
-- both regenerate cohort_session forward." Decided 2026-10-18: ops alone
-- proposes; BOTH kinds need every active member's consent.
--
-- 1. FC-042. app.generate_cohort_sessions allowed only an admin (a signed-in
--    one: is_admin() reads auth.uid()) or the pod's tutor. tauka-python calls it
--    with the service role, where auth.uid() is NULL, so every call raised and
--    offer_service._activate_if_viable logged "session generation failed": a
--    pod the matcher activated got no calendar. The service role is now
--    allowed. Production had 0 cohorts, so nothing was missed.
--
-- 2. pod_schedule_change: one proposal. permanent = a new weekly slot from a
--    checkpoint (Ethiopic day 1); temporary = a different weekly slot for the
--    sessions inside [starts_on, ends_on), at most 60 days (Ramadan ~30,
--    Hudade ~55). proposed -> agreed (every active member said yes) ->
--    applied; or declined (anyone said no) / withdrawn (ops) / expired (not
--    agreed by starts_on).
--
-- 3. pod_schedule_consent: each member's answer, once.

-- 1. FC-042 ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.generate_cohort_sessions(p_cohort uuid, p_months integer DEFAULT 3)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'app', 'public'
AS $function$
DECLARE
  v_dow int; v_time time; v_tz text; v_per_month int; v_status text; v_home uuid;
  v_ratio text; v_home_n int; v_guest_n int; v_guests uuid[]; v_block int; v_pos int;
  v_lead uuid; v_lead_role text; v_month date; v_first date; v_occurrence date;
  v_at timestamptz; v_next_num int; v_created int := 0; i int; k int;
BEGIN
  -- §113 (FC-042): the service role too — tauka-python activates pods.
  IF NOT (COALESCE(auth.role(), '') = 'service_role' OR app.is_admin() OR EXISTS (
            SELECT 1 FROM app.cohorts c WHERE c.id = p_cohort AND c.tutor_id = auth.uid())) THEN
    RAISE EXCEPTION 'not permitted to generate sessions for this cohort';
  END IF;
  SELECT slot_day_of_week, slot_time, slot_timezone, COALESCE(sessions_per_month, 3), status, tutor_id
    INTO v_dow, v_time, v_tz, v_per_month, v_status, v_home
    FROM app.cohorts WHERE id = p_cohort;
  IF v_dow IS NULL OR v_time IS NULL OR v_tz IS NULL THEN
    RAISE EXCEPTION 'cohort % has no complete weekly slot', p_cohort;
  END IF;
  IF v_status IN ('completed','dissolved') THEN RETURN 0; END IF;
  SELECT value INTO v_ratio FROM app.config WHERE key = 'match.home_guest_ratio';
  v_home_n  := COALESCE(NULLIF(split_part(COALESCE(v_ratio,'3:1'), ':', 1), '')::int, 3);
  v_guest_n := COALESCE(NULLIF(split_part(COALESCE(v_ratio,'3:1'), ':', 2), '')::int, 1);
  IF v_home_n  < 1 THEN v_home_n  := 3; END IF;
  IF v_guest_n < 0 THEN v_guest_n := 1; END IF;
  SELECT COALESCE(array_agg(DISTINCT l.tutor_id), '{}') INTO v_guests
    FROM app.student_tutor_link l JOIN app.cohort_memberships cm ON cm.user_id = l.student_id
   WHERE cm.cohort_id = p_cohort AND cm.status = 'active' AND l.role = 'guest' AND l.state = 'active'
     AND l.tutor_id IS DISTINCT FROM v_home;
  SELECT COALESCE(MAX(session_number), 0) + 1 INTO v_next_num FROM app.cohort_session WHERE cohort_id = p_cohort;
  v_month := date_trunc('month', (now() AT TIME ZONE v_tz))::date;
  FOR i IN 0 .. p_months - 1 LOOP
    v_first := (v_month + (i || ' months')::interval)::date;
    -- §93: slot_day_of_week is 0 = Monday .. 6 = Sunday; isodow is 1 = Monday .. 7 = Sunday.
    v_occurrence := v_first + ((v_dow + 1) - EXTRACT(isodow FROM v_first)::int + 7) % 7;
    FOR k IN 0 .. v_per_month - 1 LOOP
      IF date_trunc('month', v_occurrence + (k * 7))::date <> v_first THEN CONTINUE; END IF;
      v_at := ((v_occurrence + (k * 7)) + v_time) AT TIME ZONE v_tz;
      IF v_at <= now() THEN CONTINUE; END IF;
      v_pos := (v_next_num - 1) % (v_home_n + v_guest_n);
      IF v_pos < v_home_n OR cardinality(v_guests) = 0 THEN
        v_lead := v_home; v_lead_role := 'home';
      ELSE
        v_block := (v_next_num - 1) / (v_home_n + v_guest_n);
        v_lead := v_guests[ ((v_block + (v_pos - v_home_n)) % cardinality(v_guests)) + 1 ];
        v_lead_role := 'guest';
      END IF;
      INSERT INTO app.cohort_session (cohort_id, session_number, scheduled_at, duration_minutes, status, lead_tutor_id, lead_role)
      VALUES (p_cohort, v_next_num, v_at, 50, 'scheduled', v_lead, CASE WHEN v_lead IS NULL THEN NULL ELSE v_lead_role END)
      ON CONFLICT (cohort_id, scheduled_at) DO NOTHING;
      IF FOUND THEN v_next_num := v_next_num + 1; v_created := v_created + 1; END IF;
    END LOOP;
  END LOOP;
  RETURN v_created;
END;
$function$;

-- 2. pod_schedule_change ---------------------------------------------------
CREATE TABLE IF NOT EXISTS app.pod_schedule_change (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id       uuid NOT NULL REFERENCES app.cohorts(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('permanent','temporary')),
  new_day_of_week int  NOT NULL CHECK (new_day_of_week BETWEEN 0 AND 6),
  new_time        time NOT NULL,
  -- The pod's own zone (cohorts.slot_timezone) when it was proposed.
  timezone        text NOT NULL,
  starts_on       date NOT NULL,
  ends_on         date,
  reason          text NOT NULL CHECK (reason IN ('ramadan','hudade','tutor','members','other')),
  status          text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','agreed','applied','declined','withdrawn','expired')),
  proposed_by     uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  decided_at      timestamptz,
  applied_at      timestamptz,
  CONSTRAINT pod_schedule_change_window CHECK (
    (kind = 'permanent' AND ends_on IS NULL)
    OR (kind = 'temporary' AND ends_on > starts_on AND ends_on <= starts_on + 60))
);
-- One live proposal per pod at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pod_schedule_change_one_live
  ON app.pod_schedule_change (cohort_id) WHERE status IN ('proposed','agreed');

CREATE OR REPLACE FUNCTION app.pod_schedule_change_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status, NEW.status) IN (
       ('proposed','agreed'), ('proposed','declined'), ('proposed','withdrawn'), ('proposed','expired'),
       ('agreed','applied'), ('agreed','withdrawn'), ('agreed','declined')) THEN
    RAISE EXCEPTION 'pod_schedule_change: % -> % is not allowed', OLD.status, NEW.status
      USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.cohort_id, NEW.kind, NEW.new_day_of_week, NEW.new_time, NEW.timezone, NEW.starts_on, NEW.ends_on)
     IS DISTINCT FROM
     (OLD.cohort_id, OLD.kind, OLD.new_day_of_week, OLD.new_time, OLD.timezone, OLD.starts_on, OLD.ends_on) THEN
    RAISE EXCEPTION 'pod_schedule_change: a proposal is not edited' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_pod_schedule_change_guard ON app.pod_schedule_change;
CREATE TRIGGER trg_pod_schedule_change_guard BEFORE UPDATE ON app.pod_schedule_change
  FOR EACH ROW EXECUTE FUNCTION app.pod_schedule_change_guard();

-- 3. pod_schedule_consent --------------------------------------------------
CREATE TABLE IF NOT EXISTS app.pod_schedule_consent (
  change_id   uuid NOT NULL REFERENCES app.pod_schedule_change(id) ON DELETE CASCADE,
  student_id  uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  answer      text NOT NULL CHECK (answer IN ('agree','decline')),
  answered_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (change_id, student_id)
);

-- RLS: a member reads proposals for their own pods and their own answers; ops
-- reads all. tauka-python writes.
ALTER TABLE app.pod_schedule_change ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.pod_schedule_consent ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS pod_schedule_change_read ON app.pod_schedule_change;
CREATE POLICY pod_schedule_change_read ON app.pod_schedule_change FOR SELECT TO authenticated
  USING (app.has_admin_capability('ops') OR EXISTS (
    SELECT 1 FROM app.cohort_memberships m
     WHERE m.cohort_id = pod_schedule_change.cohort_id AND m.user_id = auth.uid() AND m.status = 'active'));
DROP POLICY IF EXISTS pod_schedule_consent_read ON app.pod_schedule_consent;
CREATE POLICY pod_schedule_consent_read ON app.pod_schedule_consent FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.has_admin_capability('ops'));
REVOKE ALL ON app.pod_schedule_change, app.pod_schedule_consent FROM anon, authenticated;
GRANT SELECT ON app.pod_schedule_change, app.pod_schedule_consent TO authenticated;
GRANT ALL ON app.pod_schedule_change, app.pod_schedule_consent TO service_role;

COMMENT ON TABLE app.pod_schedule_change IS
  'A proposed move of a pod time (plan 4.5, E-14): permanent from a checkpoint, or a dated temporary '
  'window (max 60 days). Ops proposes; applied only once every active member agrees. tauka-python writes.';
COMMENT ON TABLE app.pod_schedule_consent IS
  'Each member answer to a pod_schedule_change, once. tauka-python writes; the member and ops read.';
-- Applied to production 2026-10-18 in four parts (the MCP tool refuses some long
-- payloads): s113_generate_sessions_service_role, s113_pod_schedule_change,
-- s113_pod_schedule_change_guard, s113_pod_schedule_rls. Both dry runs passed first
-- (the fix generated 3 sessions as service_role and still refused a signed-in
-- non-admin).
