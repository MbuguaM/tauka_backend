-- §160 (2026-11-07): exchange guests in the enrolment state machine (fixes §159 XE-23).
-- Run after §159.

-- ── XE-23 fix (2026-11-07): the enrolment guard knew nothing of exchange_guest ──────────────
-- §98's guard only let a new row start at waitlisted or phase0, so a guest sign-up failed with
-- "a new row must start at waitlisted or phase0, not exchange_guest". A guest row may now start
-- there, and a guest who takes up the course moves on to waitlisted or phase0 (or cancels).
CREATE OR REPLACE FUNCTION app.enrolment_state_guard()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'app', 'pg_catalog'
AS $function$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NOT app.enrolment_transition_ok(OLD.state, NEW.state) THEN
      RAISE EXCEPTION 'enrolment_state: % -> % is not a legal transition', OLD.state, NEW.state USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN NEW.state_changed_at := now(); END IF;
    IF OLD.phase0_completed_at IS NOT NULL AND NEW.phase0_completed_at IS DISTINCT FROM OLD.phase0_completed_at THEN
      RAISE EXCEPTION 'enrolment_state: phase0_completed_at cannot be changed once set' USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.state NOT IN ('waitlisted','phase0','exchange_guest') THEN
    RAISE EXCEPTION 'enrolment_state: a new row must start at waitlisted, phase0 or exchange_guest, not %', NEW.state USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END; $function$;

CREATE OR REPLACE FUNCTION app.enrolment_transition_ok(p_from text, p_to text)
 RETURNS boolean
 LANGUAGE sql
 IMMUTABLE
 SET search_path TO 'app', 'public'
AS $function$
  SELECT p_from = p_to OR (p_from, p_to) IN (
    ('waitlisted','phase0'),('waitlisted','cancelled'),
    ('phase0','paid'),('phase0','cancelled'),
    ('paid','seat_confirmed'),('paid','paused'),('paid','cancelled'),
    ('seat_confirmed','matched'),('seat_confirmed','wait_pool'),('seat_confirmed','paused'),('seat_confirmed','cancelled'),
    ('matched','in_pod'),('matched','wait_pool'),('matched','cancelled'),
    ('wait_pool','matched'),('wait_pool','paused'),('wait_pool','cancelled'),
    ('in_pod','wait_pool'),('in_pod','paused'),('in_pod','cancelled'),
    ('paused','paid'),('paused','seat_confirmed'),('paused','cancelled'),
    ('cancelled','phase0'),('cancelled','paid'),('cancelled','seat_confirmed'),
    ('exchange_guest','waitlisted'),('exchange_guest','phase0'),('exchange_guest','cancelled')
  );
$function$;
