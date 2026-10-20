-- ============================================================================
-- §105 (2026-10-18)  Phase 0 clinics (enrolment plan 1.5)
-- ============================================================================
--
-- DOMAIN: enrolment (tauka-python). The plan's sketch put these in a new
-- `phase0` domain; they live in the approved enrolment domain instead, because
-- a clinic is a Phase 0 enrolment mechanic and a new domain needs its own
-- approval round for no gain.
--
-- Playbook §Phase 0 clinics: choral shadowing, the tutor models and learners
-- repeat muted; up to 12-15 learners, about 30 minutes; mixed tiers are fine;
-- at least one weekend and one weekday clinic per band each week. A clinic with
-- a SINGLE attendee becomes a 15-minute 1:1 pronunciation check-in (the floor,
-- not the default) — recorded here as delivered_minutes = 15.
--
-- Allowance per tier (Learner 1, Tutor 2, Intensive 2, Free 0) is enforced by
-- tauka-python when booking, counting bookings. It is NOT a session_entitlement
-- grant as the plan sketched: that ledger is tauka-flutter's table, its `kind`
-- CHECK would need a cross-owner change, and a one-off Phase 0 allowance is not
-- a monthly money balance.
--
-- Run by a rung >= 2 tutor (§2k: clinics are session-tutor work) — checked by
-- tauka-python when an admin creates one.

CREATE TABLE IF NOT EXISTS app.clinic (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  band              text NOT NULL CHECK (band IN ('americas','europe','ea_gulf','asia')),
  tutor_id          uuid NOT NULL REFERENCES app.tutor(id) ON DELETE RESTRICT,
  starts_at         timestamptz NOT NULL,
  duration_minutes  int  NOT NULL DEFAULT 30 CHECK (duration_minutes = 30),
  capacity          int  NOT NULL DEFAULT 15 CHECK (capacity BETWEEN 1 AND 15),
  status            text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','completed','cancelled')),
  cancelled_reason  text CHECK (cancelled_reason IS NULL OR cancelled_reason IN ('tutor_issue','admin','no_bookings')),
  -- 30 as a clinic; 15 when exactly one learner attended (the 1:1 check-in
  -- fallback). Set by trigger when the clinic completes.
  delivered_minutes int CHECK (delivered_minutes IS NULL OR delivered_minutes IN (15, 30)),
  video_session_id  uuid REFERENCES app.video_session(id) ON DELETE SET NULL,
  created_by        uuid,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT clinic_cancel_has_reason CHECK ((status = 'cancelled') = (cancelled_reason IS NOT NULL)),
  CONSTRAINT clinic_delivered_when_completed CHECK (delivered_minutes IS NULL OR status = 'completed')
);
CREATE INDEX IF NOT EXISTS idx_clinic_upcoming ON app.clinic (band, starts_at) WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS idx_clinic_tutor ON app.clinic (tutor_id, starts_at);

CREATE TABLE IF NOT EXISTS app.clinic_booking (
  clinic_id        uuid NOT NULL REFERENCES app.clinic(id) ON DELETE CASCADE,
  student_id       uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  -- booked -> attended | absent (tutor-marked after the start), or cancelled.
  status           text NOT NULL DEFAULT 'booked' CHECK (status IN ('booked','cancelled','attended','absent')),
  booked_at        timestamptz NOT NULL DEFAULT now(),
  cancelled_at     timestamptz,
  reminder_sent_at timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (clinic_id, student_id),
  CONSTRAINT clinic_booking_cancel_stamp CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_clinic_booking_student ON app.clinic_booking (student_id, status);

-- Capacity, and no booking/marking against the wrong clinic state. Serialised
-- on the clinic row so two last-seat bookings can't both win.
CREATE OR REPLACE FUNCTION app.clinic_booking_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  c record;
  v_taken int;
BEGIN
  SELECT status, starts_at, capacity INTO c FROM app.clinic WHERE id = NEW.clinic_id FOR UPDATE;
  IF TG_OP = 'INSERT' OR (OLD.status = 'cancelled' AND NEW.status = 'booked') THEN
    IF c.status <> 'scheduled' OR c.starts_at <= now() THEN
      RAISE EXCEPTION 'clinic_booking: clinic is not open for booking' USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO v_taken FROM app.clinic_booking
     WHERE clinic_id = NEW.clinic_id AND status <> 'cancelled' AND student_id <> NEW.student_id;
    IF v_taken >= c.capacity THEN
      RAISE EXCEPTION 'clinic_booking: clinic is full' USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.status IN ('attended','absent') AND NEW.status IS DISTINCT FROM OLD.status THEN
    IF c.status = 'cancelled' OR c.starts_at > now() OR OLD.status = 'cancelled' THEN
      RAISE EXCEPTION 'clinic_booking: attendance only for a held seat once the clinic has started'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.status = 'cancelled' AND OLD.status IN ('attended','absent') THEN
    RAISE EXCEPTION 'clinic_booking: a marked seat cannot be cancelled' USING ERRCODE = 'check_violation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_clinic_booking_guard ON app.clinic_booking;
CREATE TRIGGER trg_clinic_booking_guard BEFORE INSERT OR UPDATE ON app.clinic_booking
  FOR EACH ROW EXECUTE FUNCTION app.clinic_booking_guard();

-- Completed once no held seat is unmarked; 15 minutes if exactly one attended.
CREATE OR REPLACE FUNCTION app.clinic_complete_when_marked()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  v_attended int;
BEGIN
  IF EXISTS (SELECT 1 FROM app.clinic_booking WHERE clinic_id = NEW.clinic_id AND status = 'booked') THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO v_attended FROM app.clinic_booking WHERE clinic_id = NEW.clinic_id AND status = 'attended';
  UPDATE app.clinic
     SET status = 'completed',
         delivered_minutes = CASE WHEN v_attended = 1 THEN 15 ELSE 30 END,
         updated_at = now()
   WHERE id = NEW.clinic_id AND status = 'scheduled' AND starts_at <= now();
  RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS trg_clinic_complete_when_marked ON app.clinic_booking;
CREATE TRIGGER trg_clinic_complete_when_marked AFTER UPDATE OF status ON app.clinic_booking
  FOR EACH ROW WHEN (NEW.status IN ('attended','absent')) EXECUTE FUNCTION app.clinic_complete_when_marked();

DROP TRIGGER IF EXISTS trg_clinic_updated_at ON app.clinic;
CREATE TRIGGER trg_clinic_updated_at BEFORE UPDATE ON app.clinic
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE OR REPLACE FUNCTION app.is_clinic_tutor(p_clinic uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog'
AS $$ SELECT EXISTS (SELECT 1 FROM app.clinic WHERE id = p_clinic AND tutor_id = auth.uid()); $$;
CREATE OR REPLACE FUNCTION app.is_clinic_booked(p_clinic uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'app', 'pg_catalog'
AS $$ SELECT EXISTS (SELECT 1 FROM app.clinic_booking WHERE clinic_id = p_clinic AND student_id = auth.uid()
                      AND status <> 'cancelled'); $$;
REVOKE ALL ON FUNCTION app.is_clinic_tutor(uuid), app.is_clinic_booked(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION app.is_clinic_tutor(uuid), app.is_clinic_booked(uuid) TO authenticated, service_role;

ALTER TABLE app.clinic         ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.clinic_booking ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS clinic_select ON app.clinic;
CREATE POLICY clinic_select ON app.clinic FOR SELECT TO authenticated
  USING (app.is_admin() OR tutor_id = auth.uid() OR app.is_clinic_booked(id));
DROP POLICY IF EXISTS clinic_booking_select ON app.clinic_booking;
CREATE POLICY clinic_booking_select ON app.clinic_booking FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin() OR app.is_clinic_tutor(clinic_id));
REVOKE ALL ON app.clinic, app.clinic_booking FROM anon, authenticated;
GRANT SELECT ON app.clinic, app.clinic_booking TO authenticated;
GRANT ALL ON app.clinic, app.clinic_booking TO service_role;

COMMENT ON TABLE app.clinic IS
  'Phase 0 clinic (plan 1.5): 30-min choral shadowing for up to 15 learners in a band, run by a '
  'rung >= 2 tutor. delivered_minutes = 15 when one learner attended (the 1:1 check-in fallback). '
  'tauka-python writes; its tutor, its learners and admins read.';
COMMENT ON TABLE app.clinic_booking IS
  'A learner''s seat in a clinic (plan 1.5). Capacity, booking window and attendance timing are '
  'trigger-enforced; the per-tier allowance (Learner 1, Tutor 2, Intensive 2) by tauka-python.';
-- Applied to production 2026-10-18 as migration s105_phase0_clinics.
