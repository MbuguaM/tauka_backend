-- ============================================================================
-- §108 (2026-10-18)  Friend groups and private pods (enrolment plan 3.2, E-12)
-- ============================================================================
--
-- enrolment domain (tauka-python owns DDL and writes).
--
-- Playbook paths 2 and 3:
--   * FRIENDS (pair or trio): one person creates a group link, the others join
--     with it; matched as ONE UNIT taking 2 or 3 seats. If members' levels
--     differ they're told up front — never placed together silently. If one
--     isn't ready, the faster waits for the slower. If one leaves, the other
--     stays as a normal member.
--   * PRIVATE POD (four): families, couples, small teams. They skip matching and
--     choose their own slot; ops forms them by hand for now (playbook: run it by
--     hand first). The matcher leaves their members out.
--
-- No discounts or premiums: prices are the pricing page's (E-03; plan 2.5).
-- The plan's sketch had `discount` and institutional kinds; neither is built.

CREATE TABLE IF NOT EXISTS app.enrolment_group (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind       text NOT NULL CHECK (kind IN ('friends','private_pod')),
  -- The shareable code: 8 characters, unambiguous alphabet, generated server-side.
  code       text NOT NULL UNIQUE CHECK (code ~ '^[A-HJ-NP-Z2-9]{8}$'),
  course_id  uuid NOT NULL REFERENCES app.course(id) ON DELETE CASCADE,
  created_by uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  max_size   int  NOT NULL CHECK ((kind = 'friends' AND max_size = 3) OR (kind = 'private_pod' AND max_size = 4)),
  status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open','matched','dissolved')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.enrolment_group_member (
  group_id   uuid NOT NULL REFERENCES app.enrolment_group(id) ON DELETE CASCADE,
  student_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  joined_at  timestamptz NOT NULL DEFAULT now(),
  left_at    timestamptz,
  PRIMARY KEY (group_id, student_id)
);
-- One group at a time per learner.
CREATE UNIQUE INDEX IF NOT EXISTS idx_enrolment_group_one_active
  ON app.enrolment_group_member (student_id) WHERE left_at IS NULL;

-- Capacity under a row lock on the group; no joining a matched or dissolved group.
CREATE OR REPLACE FUNCTION app.enrolment_group_member_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  g record;
  n int;
BEGIN
  IF TG_OP = 'INSERT' OR (OLD.left_at IS NOT NULL AND NEW.left_at IS NULL) THEN
    SELECT status, max_size INTO g FROM app.enrolment_group WHERE id = NEW.group_id FOR UPDATE;
    IF g.status <> 'open' THEN
      RAISE EXCEPTION 'enrolment_group: not open' USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO n FROM app.enrolment_group_member
     WHERE group_id = NEW.group_id AND left_at IS NULL AND student_id <> NEW.student_id;
    IF n >= g.max_size THEN
      RAISE EXCEPTION 'enrolment_group: full' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_enrolment_group_member_guard ON app.enrolment_group_member;
CREATE TRIGGER trg_enrolment_group_member_guard BEFORE INSERT OR UPDATE ON app.enrolment_group_member
  FOR EACH ROW EXECUTE FUNCTION app.enrolment_group_member_guard();

DROP TRIGGER IF EXISTS trg_enrolment_group_updated_at ON app.enrolment_group;
CREATE TRIGGER trg_enrolment_group_updated_at BEFORE UPDATE ON app.enrolment_group
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE OR REPLACE FUNCTION app.in_enrolment_group(p_group uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog'
AS $$ SELECT EXISTS (SELECT 1 FROM app.enrolment_group_member
                      WHERE group_id = p_group AND student_id = auth.uid() AND left_at IS NULL); $$;
REVOKE ALL ON FUNCTION app.in_enrolment_group(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.in_enrolment_group(uuid) TO authenticated, service_role;

ALTER TABLE app.enrolment_group        ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.enrolment_group_member ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS enrolment_group_select ON app.enrolment_group;
CREATE POLICY enrolment_group_select ON app.enrolment_group FOR SELECT TO authenticated
  USING (app.in_enrolment_group(id) OR app.is_admin());
DROP POLICY IF EXISTS enrolment_group_member_select ON app.enrolment_group_member;
CREATE POLICY enrolment_group_member_select ON app.enrolment_group_member FOR SELECT TO authenticated
  USING (app.in_enrolment_group(group_id) OR app.is_admin());
REVOKE ALL ON app.enrolment_group, app.enrolment_group_member FROM anon, authenticated;
GRANT SELECT ON app.enrolment_group, app.enrolment_group_member TO authenticated;
GRANT ALL ON app.enrolment_group, app.enrolment_group_member TO service_role;

COMMENT ON TABLE app.enrolment_group IS
  'A friend group (2-3, matched as one unit) or a private pod (4, formed by ops) — plan 3.2. '
  'Joined by code. tauka-python writes; members and admins read.';
COMMENT ON TABLE app.enrolment_group_member IS
  'Membership of an enrolment_group; left_at ends it. One active group per learner. Capacity '
  'and open-only joining are trigger-enforced. tauka-python writes.';
-- Applied to production 2026-10-18 as migration s108_enrolment_groups.
