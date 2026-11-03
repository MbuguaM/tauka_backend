-- §151 Monthly plans through Paystack (2026-11-03, API-2, decision D7). Owner: tauka-python.
--
-- Separate tables, not columns on app.student: Paystack's email_token can cancel a
-- subscription, and tutors can read student rows. Neither table has a client policy or
-- client grant, so learners see this only through /account/*, which never returns the token.
-- app.student.tier, subscription_status and current_period_end stay what every gate reads.

CREATE TABLE IF NOT EXISTS app.billing_subscription (
  student_id        uuid PRIMARY KEY REFERENCES app.student(id) ON DELETE CASCADE,
  provider          text NOT NULL CHECK (provider IN ('paystack','demo')),
  channel           text NOT NULL CHECK (channel IN ('card','mpesa')),
  tier              text NOT NULL CHECK (tier IN ('learner_tier','tutor_tier','intensive_tier')),
  status            text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','active','past_due','cancelling','ended')),
  email             text NOT NULL,
  customer_code     text,
  subscription_code text UNIQUE,
  email_token       text,
  plan_code         text,
  current_period_end timestamptz,
  reminded_for      timestamptz,   -- the period end an M-Pesa learner was last reminded about
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS billing_subscription_customer ON app.billing_subscription (customer_code);
CREATE INDEX IF NOT EXISTS billing_subscription_open ON app.billing_subscription (status)
  WHERE status IN ('active','past_due','cancelling');

CREATE TABLE IF NOT EXISTS app.billing_payment (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  tier          text NOT NULL CHECK (tier IN ('learner_tier','tutor_tier','intensive_tier')),
  channel       text NOT NULL CHECK (channel IN ('card','mpesa')),
  purpose       text NOT NULL CHECK (purpose IN ('first','renewal','month')),
  amount_minor  bigint NOT NULL CHECK (amount_minor >= 0),
  currency      text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  reference     text NOT NULL UNIQUE,   -- mo-<id> when we opened it; Paystack's own on a card renewal
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','failed')),
  paid_at       timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS billing_payment_student ON app.billing_payment (student_id, created_at DESC);

ALTER TABLE app.billing_subscription ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.billing_payment ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.billing_subscription, app.billing_payment FROM PUBLIC, anon, authenticated;
GRANT ALL ON app.billing_subscription, app.billing_payment TO service_role;
COMMENT ON TABLE app.billing_subscription IS 'One monthly plan per learner (§151). Holds Paystack''s cancel token, so service role only.';
COMMENT ON TABLE app.billing_payment IS 'Each monthly payment (§151). tauka-python writes; learners read through /account/billing-history.';

ALTER TABLE app.student_payment DROP CONSTRAINT IF EXISTS student_payment_source_check;
ALTER TABLE app.student_payment ADD CONSTRAINT student_payment_source_check
  CHECK (source = ANY (ARRAY['stripe_invoice'::text, 'mpesa'::text, 'addon'::text, 'paystack'::text]));
