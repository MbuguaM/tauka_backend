-- ============================================================================
-- §97 (2026-10-18)  Waitlist double opt-in (enrolment plan H2)
-- ============================================================================
-- An entry means nothing until the inbox owner clicks the emailed link: anyone
-- can type anyone's address into a public form. The token is never stored, only
-- its sha256. Unconfirmed entries expire after 7 days (the new default);
-- confirming extends retention to 12 months. Mail an entry only when
-- confirmed_at IS NOT NULL AND email_opt_in. The table was empty when applied.
ALTER TABLE app.waitlist_entry
  ADD COLUMN IF NOT EXISTS confirmed_at timestamptz,
  ADD COLUMN IF NOT EXISTS confirm_token_hash text,
  ADD COLUMN IF NOT EXISTS confirm_sent_at timestamptz;
ALTER TABLE app.waitlist_entry ALTER COLUMN delete_after SET DEFAULT now() + interval '7 days';
CREATE UNIQUE INDEX IF NOT EXISTS idx_waitlist_entry_token
  ON app.waitlist_entry (confirm_token_hash) WHERE confirm_token_hash IS NOT NULL;
ALTER TABLE app.waitlist_entry DROP CONSTRAINT IF EXISTS waitlist_entry_token_hash_shape;
ALTER TABLE app.waitlist_entry ADD CONSTRAINT waitlist_entry_token_hash_shape
  CHECK (confirm_token_hash IS NULL OR confirm_token_hash ~ '^[0-9a-f]{64}$');
COMMENT ON COLUMN app.waitlist_entry.confirmed_at IS
  'Set when the inbox owner clicks the confirmation link (double opt-in, §97). Mail an entry only when confirmed_at IS NOT NULL AND email_opt_in.';
COMMENT ON COLUMN app.waitlist_entry.confirm_token_hash IS
  'sha256 hex of the token in the confirmation email. The token itself is never stored. Kept after confirmation so the email''s remove link keeps working.';
COMMENT ON COLUMN app.waitlist_entry.delete_after IS
  'Unconfirmed: 7 days from the last confirmation email. Confirmed: 12 months from confirmation or the latest confirmed re-submission. Swept daily unless converted.';
-- Applied to production 2026-10-18 as migration s97_waitlist_double_opt_in.