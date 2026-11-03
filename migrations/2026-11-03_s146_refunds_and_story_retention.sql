-- §146 Refunds on one-off purchases, and how long sponsorship stories are kept
-- (2026-11-03). Owner: tauka-python (enrolment domain). Needs §144 and §145.
--
-- REFUNDS: status 'refunded' is set when a refund is asked for, which voids an
-- unused code or claim link at once. refund_state then follows the provider,
-- because a refund can take days and can fail after the status has moved.
--
-- STORIES (L-024, provisional until legal review): story becomes nullable so it
-- can be erased while the row survives. A sponsorship row needs its request
-- (sponsorship_mode_target), so a sponsored request can't simply be deleted.

ALTER TABLE app.gift_card
  ADD COLUMN IF NOT EXISTS refund_state text CHECK (refund_state IN ('requested','processed','failed','needs_attention')),
  ADD COLUMN IF NOT EXISTS refund_reason text CHECK (refund_reason IS NULL OR length(refund_reason) <= 300),
  ADD COLUMN IF NOT EXISTS refunded_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS refund_requested_at timestamptz,
  ADD COLUMN IF NOT EXISTS refund_processed_at timestamptz;

ALTER TABLE app.sponsorship
  ADD COLUMN IF NOT EXISTS refund_state text CHECK (refund_state IN ('requested','processed','failed','needs_attention')),
  ADD COLUMN IF NOT EXISTS refund_reason text CHECK (refund_reason IS NULL OR length(refund_reason) <= 300),
  ADD COLUMN IF NOT EXISTS refunded_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS refund_requested_at timestamptz,
  ADD COLUMN IF NOT EXISTS refund_processed_at timestamptz;

ALTER TABLE app.sponsorship_request ALTER COLUMN story DROP NOT NULL;
ALTER TABLE app.sponsorship_request DROP CONSTRAINT IF EXISTS sponsorship_request_story_check;
ALTER TABLE app.sponsorship_request
  ADD CONSTRAINT sponsorship_request_story_check CHECK (story IS NULL OR length(btrim(story)) BETWEEN 40 AND 600),
  ADD COLUMN IF NOT EXISTS story_erased_at timestamptz,
  ADD CONSTRAINT sponsorship_request_open_has_story CHECK (status NOT IN ('pending','listed') OR story IS NOT NULL);

COMMENT ON COLUMN app.sponsorship_request.story_erased_at IS 'When the story was erased (§146, L-024): at once on withdrawal, 30 days after a decline or a sponsorship.';
