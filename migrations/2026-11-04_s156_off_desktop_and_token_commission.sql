-- §156 Phone sessions for tutors, and commission on AI tokens (2026-11-04, plan D16, D17). Owner: tauka-python.
--
-- off_desktop_session  one row per tutor per session taught from a phone. The row is what stops
--                      a second email when a tutor rejoins. It's a record for the people who run
--                      tutors. Pay, rungs, matching and access must never read it.
-- ai_tokens           a new student_payment source, so a token pack counts toward commission.

CREATE TABLE IF NOT EXISTS app.off_desktop_session (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_session_id uuid NOT NULL REFERENCES app.video_session(id) ON DELETE CASCADE,
  tutor_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  platform         text NOT NULL CHECK (platform IN ('android','ios')),
  notified_at      timestamptz,   -- null until the email went out
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (video_session_id, tutor_id)
);
CREATE INDEX IF NOT EXISTS off_desktop_session_tutor ON app.off_desktop_session (tutor_id, created_at DESC);

ALTER TABLE app.off_desktop_session ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.off_desktop_session FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON app.off_desktop_session TO service_role;
COMMENT ON TABLE app.off_desktop_session IS
  'Live sessions a tutor taught from a phone (§156, D16). tauka-python writes. Never an input to pay, rungs, matching or access.';

ALTER TABLE app.student_payment DROP CONSTRAINT IF EXISTS student_payment_source_check;
ALTER TABLE app.student_payment ADD CONSTRAINT student_payment_source_check
  CHECK (source = ANY (ARRAY['stripe_invoice'::text, 'mpesa'::text, 'addon'::text, 'paystack'::text, 'ai_tokens'::text]));
