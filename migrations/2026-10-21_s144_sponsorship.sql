-- §144 Sponsorship (2026-10-21) — the pricing page's "Three other ways in":
-- sponsor a learner from the sponsorship waitlist, sponsor someone you name,
-- or ask for a sponsored seat. Owner: tauka-python (enrolment domain).
--
-- THE SPONSORSHIP WAITLIST (sponsorship_request): a signed-in learner who
--   can't afford a seat applies with a first name, a country, a reason and a
--   short story. Ops review every application before it is listed; only
--   listed ones are shown, and only those fields (never a surname, an email
--   or an account id). Applying records consent 'sponsorship_listing' (L-024).
-- A SPONSORED SEAT (sponsorship): N months of a tier, paid once by anyone.
--   mode 'waitlist' — for a listed request; checkout holds it for 30 minutes
--     so two sponsors can't pay for the same person; paid -> granted at once.
--   mode 'named'    — for someone the sponsor names by email; paid -> we email
--     them a claim link (the token is stored hashed); claiming grants it.
--   A sponsor who was once sponsored is paying it forward (forward_of).
-- Access ends after the months unless the learner is paying by then; unclaimed
-- named seats lapse after 12 months (as gift cards do).
-- Flag 'sponsorship', seeded 'coming'.

CREATE TABLE IF NOT EXISTS app.sponsorship_request (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  first_name    text NOT NULL CHECK (length(btrim(first_name)) BETWEEN 1 AND 40),
  country_code  text CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
  reason        text NOT NULL CHECK (reason IN ('heritage','partner','relocating','faith','work','other')),
  story         text NOT NULL CHECK (length(btrim(story)) BETWEEN 40 AND 600),
  tier_wanted   text NOT NULL DEFAULT 'tutor_tier' CHECK (tier_wanted IN ('learner_tier','tutor_tier','intensive_tier')),
  months_wanted int  NOT NULL DEFAULT 12 CHECK (months_wanted IN (3,6,12)),
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','listed','sponsored','declined','withdrawn')),
  decline_reason text CHECK (decline_reason IS NULL OR length(decline_reason) <= 300),
  reviewed_by   uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  reviewed_at   timestamptz,
  listed_at     timestamptz,
  held_until    timestamptz,
  sponsored_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
-- One open application per learner.
CREATE UNIQUE INDEX IF NOT EXISTS sponsorship_request_one_open
  ON app.sponsorship_request (student_id) WHERE status IN ('pending','listed');
CREATE INDEX IF NOT EXISTS sponsorship_request_listed ON app.sponsorship_request (listed_at) WHERE status = 'listed';

CREATE TABLE IF NOT EXISTS app.sponsorship (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode              text NOT NULL CHECK (mode IN ('waitlist','named')),
  request_id        uuid REFERENCES app.sponsorship_request(id) ON DELETE SET NULL,
  recipient_email   text CHECK (recipient_email IS NULL OR position('@' in recipient_email) > 1),
  recipient_name    text CHECK (recipient_name IS NULL OR length(recipient_name) <= 60),
  recipient_id      uuid REFERENCES app.student(id) ON DELETE SET NULL,
  sponsor_email     text NOT NULL CHECK (position('@' in sponsor_email) > 1),
  sponsor_name      text CHECK (sponsor_name IS NULL OR length(sponsor_name) <= 60),
  sponsor_user_id   uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  anonymous         boolean NOT NULL DEFAULT false,
  message           text CHECK (message IS NULL OR length(message) <= 300),
  tier              text NOT NULL CHECK (tier IN ('learner_tier','tutor_tier','intensive_tier')),
  months            int  NOT NULL CHECK (months BETWEEN 1 AND 12),
  amount_minor      bigint NOT NULL CHECK (amount_minor >= 0),
  currency          text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
  status            text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','paid','claimed','refunded','expired','cancelled')),
  stripe_session_id text UNIQUE,
  claim_token_hash  text UNIQUE,
  forward_of        uuid REFERENCES app.sponsorship(id) ON DELETE SET NULL,
  claimed_at        timestamptz,
  access_until      timestamptz,
  forward_invited_at timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT sponsorship_mode_target CHECK (
    (mode = 'waitlist' AND request_id IS NOT NULL) OR (mode = 'named' AND recipient_email IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS sponsorship_recipient ON app.sponsorship (recipient_id);
CREATE INDEX IF NOT EXISTS sponsorship_open ON app.sponsorship (status) WHERE status IN ('paid','claimed');

ALTER TABLE app.consent_record DROP CONSTRAINT IF EXISTS consent_record_purpose_check;
ALTER TABLE app.consent_record ADD CONSTRAINT consent_record_purpose_check CHECK (purpose = ANY (ARRAY[
  'session_recording'::text, 'pronunciation_profile'::text, 'model_training'::text, 'tutor_voice_review'::text,
  'employer_reporting'::text, 'exchange_conduct'::text, 'message_filtering'::text, 'identity_check'::text,
  'english_voice_sample'::text,
  -- §144: the applicant agrees their first name, country, reason and story are shown to sponsors.
  'sponsorship_listing'::text]));

ALTER TABLE app.sponsorship_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.sponsorship ENABLE ROW LEVEL SECURITY;
-- The applicant reads their own application; the public list is served by the API, never by RLS.
CREATE POLICY sponsorship_request_read ON app.sponsorship_request FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
CREATE POLICY sponsorship_read ON app.sponsorship FOR SELECT TO authenticated
  USING (recipient_id = auth.uid() OR sponsor_user_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.sponsorship_request, app.sponsorship FROM anon, authenticated;
GRANT SELECT ON app.sponsorship_request, app.sponsorship TO authenticated;
GRANT ALL ON app.sponsorship_request, app.sponsorship TO service_role;
COMMENT ON TABLE app.sponsorship_request IS 'The sponsorship waitlist (§144): a learner asks for a sponsored seat; ops review before listing. tauka-python writes.';
COMMENT ON TABLE app.sponsorship IS 'A sponsored seat (§144): N months of a tier paid once, for a waitlisted learner or a named person. tauka-python writes.';

INSERT INTO app.feature_flag (key, status, description) VALUES
  ('sponsorship', 'coming', 'Sponsor a learner from the waitlist or by name; apply for a sponsored seat (§144, L-024)')
ON CONFLICT (key) DO NOTHING;
