-- ============================================================================
-- §126 (2026-10-18)  Missed payment: 7-day grace, then lapsed (enrolment plan 2.3a, §2c)
-- ============================================================================
--
-- enrolment domain; tauka-python writes. No column on app.student (that would
-- be a SCHEMA_REQUEST to tauka-flutter): a learner is in grace while an open
-- row has no lapsed_at, lapsed once lapsed_at is set, and back to normal when
-- the row is resolved.
--
-- Decided 2026-10-18 (developer):
--   * Grace = keep what they have: sessions, make-ups and unlocked lessons go
--     on; the new month's lessons arrive when the payment succeeds (they are
--     only ever granted on invoice.paid). Emails on days 0, 3 and 6 (WhatsApp
--     and the M-Pesa prompt wait on L-011 and plan 2.4).
--   * Day 7 unpaid -> lapsed: no room tokens for live sessions, not grouped for
--     make-ups, can't book clinics or drop-ins. The pod seat is HELD.
--   * Stripe's own retries end it: paid -> resolved 'paid' (everything back);
--     the subscription cancelled -> resolved 'cancelled', and the learner leaves
--     the pod (any cancellation does, §126: until now a cancelled learner kept
--     an active membership and the seat never reached refills).

CREATE TABLE IF NOT EXISTS app.payment_grace (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  invoice_id    text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  reminded_3_at timestamptz,
  reminded_6_at timestamptz,
  lapsed_at     timestamptz,
  resolved_at   timestamptz,
  resolution    text CHECK (resolution IS NULL OR resolution IN ('paid','cancelled')),
  CONSTRAINT payment_grace_resolved CHECK ((resolved_at IS NULL) = (resolution IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_grace_one_open ON app.payment_grace (student_id) WHERE resolved_at IS NULL;

CREATE OR REPLACE FUNCTION app.payment_grace_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.resolved_at IS NOT NULL THEN
    RAISE EXCEPTION 'payment_grace: a resolved grace is final' USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.student_id, NEW.invoice_id, NEW.started_at) IS DISTINCT FROM (OLD.student_id, OLD.invoice_id, OLD.started_at) THEN
    RAISE EXCEPTION 'payment_grace: the start is not edited' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER trg_payment_grace_guard BEFORE UPDATE ON app.payment_grace
  FOR EACH ROW EXECUTE FUNCTION app.payment_grace_guard();

ALTER TABLE app.payment_grace ENABLE ROW LEVEL SECURITY;
CREATE POLICY payment_grace_read ON app.payment_grace FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.payment_grace FROM anon, authenticated;
GRANT SELECT ON app.payment_grace TO authenticated;
GRANT ALL ON app.payment_grace TO service_role;
COMMENT ON TABLE app.payment_grace IS 'A missed payment (plan 2.3a): 7 days of grace with reminders on days 0/3/6, then lapsed (no live sessions or bookings) until Stripe settles it: paid or cancelled. tauka-python writes; the learner and admins read.';
