-- ============================================================================
-- §130 (2026-10-19)  M-Pesa as a second payment method (enrolment plan 2.4, §2d)
-- ============================================================================
--
-- enrolment domain; tauka-python writes.
--
-- Recommendation adopted (developer: "use your recommendation" for blocked
-- items, 2026-10-19): Stripe stays the one billing ledger. A learner paying by
-- M-Pesa gets a Stripe subscription with collection_method = send_invoice;
-- each invoice is paid by a Daraja STK push to their phone, CONFIRMED by an STK
-- query (the callback alone is never trusted), then marked paid out of band on
-- Stripe so renewals, lesson grants and the grace period work unchanged.
-- Ratiba standing orders need Safaricom approval; until then every new invoice
-- triggers an STK prompt automatically. Telebirr: not built (investigation).
--
-- The phone number is personal data: stored ENCRYPTED (Fernet, ENCRYPTION_KEY)
-- with the last four digits for display, only for learners who chose M-Pesa.

CREATE TABLE IF NOT EXISTS app.mpesa_payer (
  student_id      uuid PRIMARY KEY REFERENCES app.student(id) ON DELETE CASCADE,
  phone_encrypted text NOT NULL,
  phone_last4     text NOT NULL CHECK (phone_last4 ~ '^[0-9]{4}$'),
  auto_prompt     boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.mpesa_payment (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id          uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  stripe_invoice_id   text,
  purpose             text NOT NULL CHECK (purpose IN ('first_month','invoice')),
  tier                text CHECK (tier IS NULL OR tier IN ('learner_tier','tutor_tier','intensive_tier')),
  amount_kes          int  NOT NULL CHECK (amount_kes > 0),
  checkout_request_id text UNIQUE,
  merchant_request_id text,
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','failed','cancelled')),
  result_code         int,
  mpesa_receipt       text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  CONSTRAINT mpesa_payment_done CHECK ((status = 'pending') = (completed_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_mpesa_payment_student ON app.mpesa_payment (student_id, created_at);
-- One open prompt per invoice at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mpesa_payment_one_pending
  ON app.mpesa_payment (stripe_invoice_id) WHERE status = 'pending' AND stripe_invoice_id IS NOT NULL;

ALTER TABLE app.mpesa_payer ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mpesa_payment ENABLE ROW LEVEL SECURITY;
-- The payer row holds the encrypted number: service role only, no client read.
REVOKE ALL ON app.mpesa_payer FROM anon, authenticated;
GRANT ALL ON app.mpesa_payer TO service_role;
CREATE POLICY mpesa_payment_read ON app.mpesa_payment FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.mpesa_payment FROM anon, authenticated;
GRANT SELECT ON app.mpesa_payment TO authenticated;
GRANT ALL ON app.mpesa_payment TO service_role;
COMMENT ON TABLE app.mpesa_payer IS 'A learner who pays by M-Pesa (plan 2.4): the phone number, encrypted. Service role only.';
COMMENT ON TABLE app.mpesa_payment IS 'Each M-Pesa STK push (plan 2.4): pending until confirmed by an STK query, then the Stripe invoice is paid out of band. tauka-python writes.';
