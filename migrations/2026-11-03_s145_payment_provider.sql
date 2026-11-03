-- §145 Payment provider on one-off purchases (2026-11-03, decision D7: Paystack).
-- Owner: tauka-python (enrolment domain).
--
-- Gift cards and sponsored seats no longer pay through Stripe. These two columns
-- say which provider took the payment and under what reference, so neither table
-- pretends a Paystack reference is a Stripe session id. stripe_session_id stays
-- until nothing reads it; both tables hold no paid rows today.
--
-- Needs §144 first: app.sponsorship isn't in production yet.

ALTER TABLE app.gift_card
  ADD COLUMN IF NOT EXISTS payment_provider text CHECK (payment_provider IN ('paystack', 'demo')),
  ADD COLUMN IF NOT EXISTS payment_reference text UNIQUE;

ALTER TABLE app.sponsorship
  ADD COLUMN IF NOT EXISTS payment_provider text CHECK (payment_provider IN ('paystack', 'demo')),
  ADD COLUMN IF NOT EXISTS payment_reference text UNIQUE;

COMMENT ON COLUMN app.gift_card.payment_reference IS 'The provider reference: gc-<id>. Set when checkout opens.';
COMMENT ON COLUMN app.sponsorship.payment_reference IS 'The provider reference: sp-<id>. Set when checkout opens.';
