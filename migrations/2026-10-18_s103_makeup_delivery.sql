-- ============================================================================
-- §103 (2026-10-18)  Make-ups delivered: notices, completion, no early marking
--                    (enrolment plan §2f items 2-5)
-- ============================================================================
--
-- cohort_matching domain (tauka-python). Three things the screens need:
--
-- 1. booked_notice_sent_at / reminder_sent_at — email is the notification
--    channel (push is suspended client-side, FC-008). Each is stamped once so a
--    retried job never mails twice.
-- 2. A make-up becomes `completed` when every live seat is marked. It was left
--    `scheduled` for ever before, which would let it block the learner's next
--    seat (§2f item 9) and hide it from "delivered" counts.
-- 3. Attendance can't be marked before the session starts. The §92 guard limited
--    a client to the attendance column; it did not stop "attended" being
--    recorded the day before — and an attended credit seat spends the credit.

ALTER TABLE app.makeup_session
  ADD COLUMN IF NOT EXISTS booked_notice_sent_at timestamptz,
  ADD COLUMN IF NOT EXISTS reminder_sent_at timestamptz;
COMMENT ON COLUMN app.makeup_session.booked_notice_sent_at IS
  'When the "your make-up is booked" email went to its learners and tutor (§103). Set once.';
COMMENT ON COLUMN app.makeup_session.reminder_sent_at IS
  'When the day-before reminder email went (§103, makeup_cycle). Set once.';

-- 3. Marking only once the session has started (any writer except the service
--    role, which the sweeps use to withdraw seats).
CREATE OR REPLACE FUNCTION app.makeup_participant_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'app', 'pg_catalog'
AS $$
DECLARE
  v_at timestamptz;
  v_status text;
BEGIN
  IF current_user = 'authenticated' THEN
    IF (NEW.makeup_session_id, NEW.student_id, NEW.funded_by, NEW.period_start, NEW.created_at)
       IS DISTINCT FROM
       (OLD.makeup_session_id, OLD.student_id, OLD.funded_by, OLD.period_start, OLD.created_at)
       OR NEW.status NOT IN ('attended','absent','excused') THEN
      RAISE EXCEPTION 'makeup_session_participant: a client may only mark attendance';
    END IF;
  END IF;
  IF NEW.status IN ('attended','absent','excused') AND NEW.status IS DISTINCT FROM OLD.status THEN
    SELECT scheduled_at, status INTO v_at, v_status FROM app.makeup_session WHERE id = NEW.makeup_session_id;
    IF v_status NOT IN ('scheduled','in_progress','completed') OR v_at IS NULL OR v_at > now() THEN
      RAISE EXCEPTION 'makeup_session_participant: attendance can be marked only once the session has started'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

-- 2. Completed when no live seat is still pending. SECURITY DEFINER: the tutor
--    marking the last seat can't update makeup_session (admin-write policy).
CREATE OR REPLACE FUNCTION app.makeup_complete_when_marked()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'app', 'pg_catalog'
AS $$
BEGIN
  UPDATE app.makeup_session s
     SET status = 'completed', updated_at = now()
   WHERE s.id = NEW.makeup_session_id
     AND s.status IN ('scheduled','in_progress')
     AND EXISTS (SELECT 1 FROM app.makeup_session_participant p
                  WHERE p.makeup_session_id = s.id AND p.status <> 'withdrawn')
     AND NOT EXISTS (SELECT 1 FROM app.makeup_session_participant p
                      WHERE p.makeup_session_id = s.id AND p.status = 'pending');
  RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS trg_makeup_complete_when_marked ON app.makeup_session_participant;
CREATE TRIGGER trg_makeup_complete_when_marked
  AFTER UPDATE OF status ON app.makeup_session_participant
  FOR EACH ROW WHEN (NEW.status IN ('attended','absent','excused'))
  EXECUTE FUNCTION app.makeup_complete_when_marked();
-- Applied to production 2026-10-18 as migration s103_makeup_delivery.
