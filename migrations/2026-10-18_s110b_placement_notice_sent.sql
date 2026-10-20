-- §110b (2026-10-18)  Placement notices: claimed vs sent (plan 3.5)
-- cohort_matching (tauka-python). A row inserted before sending is a CLAIM;
-- `sent_at` is stamped only after the provider accepts the email. The send
-- runs inside the accept request with a time limit (app/core/followup.py), and
-- an abandoned send would otherwise leave a claim that looks like a delivery.
-- The daily job resends claims left unsent for over an hour.
ALTER TABLE app.placement_notice
  ADD COLUMN IF NOT EXISTS claimed_at timestamptz NOT NULL DEFAULT now(),
  ALTER COLUMN sent_at DROP NOT NULL,
  ALTER COLUMN sent_at DROP DEFAULT;
COMMENT ON COLUMN app.placement_notice.sent_at IS
  'When the provider accepted the placement email; NULL while only claimed (§110b).';
-- Applied to production 2026-10-18 as migration s110b_placement_notice_sent.
