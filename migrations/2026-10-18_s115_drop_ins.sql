-- ============================================================================
-- §115 (2026-10-18)  Wait-pool drop-ins (enrolment plan 3.7)
-- ============================================================================
--
-- enrolment domain (tauka-python), extending §105's clinic tables.
--
-- Plan 3.7: "Wait pool: weekly drop-ins (8–10, any level in the band, built on
-- the §1b.4 open-group type)". Decided 2026-10-18 (developer): 50 minutes;
-- bookable by every PAYING learner who isn't in a pod (wait pool, confirmed
-- seat, holding an offer; not paused, not Free); one a week; drop-ins never use
-- session credits and don't count towards the month's included sessions.
--
-- A drop-in is a clinic of another kind rather than a new table: the same
-- shape (band, tutor, time, capacity, bookings, attendance, room, reminder,
-- pay by delivered minutes) and the same database guards. §1b.4's rule holds:
-- it is never a cohort and the matcher never sees it.
--
--   kind            phase0 (the §105 clinic) | drop_in
--   duration        30 for phase0, 50 for drop_in
--   capacity        <= 15 for phase0, <= 10 for drop_in
--   delivered       phase0: 30, or 15 for a single attendee (1:1 check-in);
--                   drop_in: 50 however many came (plan 4.8: a session runs
--                   with any learner present)

ALTER TABLE app.clinic ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'phase0';
ALTER TABLE app.clinic DROP CONSTRAINT IF EXISTS clinic_kind_check;
ALTER TABLE app.clinic ADD CONSTRAINT clinic_kind_check CHECK (kind IN ('phase0','drop_in'));

ALTER TABLE app.clinic DROP CONSTRAINT IF EXISTS clinic_duration_minutes_check;
ALTER TABLE app.clinic ADD CONSTRAINT clinic_duration_minutes_check CHECK (
  (kind = 'phase0' AND duration_minutes = 30) OR (kind = 'drop_in' AND duration_minutes = 50));
ALTER TABLE app.clinic DROP CONSTRAINT IF EXISTS clinic_drop_in_capacity;
ALTER TABLE app.clinic ADD CONSTRAINT clinic_drop_in_capacity CHECK (kind <> 'drop_in' OR capacity <= 10);
ALTER TABLE app.clinic DROP CONSTRAINT IF EXISTS clinic_delivered_minutes_check;
ALTER TABLE app.clinic ADD CONSTRAINT clinic_delivered_minutes_check CHECK (
  delivered_minutes IS NULL
  OR (kind = 'phase0' AND delivered_minutes IN (15, 30))
  OR (kind = 'drop_in' AND delivered_minutes = 50));
CREATE INDEX IF NOT EXISTS idx_clinic_kind_upcoming ON app.clinic (kind, band, starts_at) WHERE status = 'scheduled';

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
         -- §115: a drop-in is its full 50 minutes; a Phase 0 clinic with one
         -- attendee becomes the 15-minute 1:1 check-in.
         delivered_minutes = CASE WHEN kind = 'drop_in' THEN 50
                                  WHEN v_attended = 1 THEN 15 ELSE 30 END,
         updated_at = now()
   WHERE id = NEW.clinic_id AND status = 'scheduled' AND starts_at <= now();
  RETURN NULL;
END;
$$;

COMMENT ON COLUMN app.clinic.kind IS
  'phase0 = Phase 0 clinic (§105, 30 min, <= 15); drop_in = wait-pool drop-in (§115, plan 3.7, 50 min, <= 10).';
-- Applied to production 2026-10-18 as migration s115_drop_ins.
