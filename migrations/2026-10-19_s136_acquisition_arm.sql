-- ============================================================================
-- §136 (2026-10-19)  The banner's A/B arm, carried to signup (enrolment plan 7.10)
-- ============================================================================
--
-- enrolment domain; tauka-python writes.
--
-- Track 7.10: the dated banner is tested against the static claim. A visitor's
-- arm ('dated' | 'static') is assigned in their browser, stored on their
-- waitlist entry (waitlist_entry.banner_arm, §95) and carried to signup, where
-- it lands here — so refunds and complaints can be compared per arm later.
-- One row per account, written once at provision; never shown to anyone.

CREATE TABLE IF NOT EXISTS app.acquisition_arm (
  user_id    uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  arm        text NOT NULL CHECK (arm IN ('dated','static')),
  experiment text NOT NULL DEFAULT 'dated_banner_v1' CHECK (experiment ~ '^[a-z0-9_]{3,40}$'),
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.acquisition_arm ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.acquisition_arm FROM anon, authenticated;
GRANT ALL ON app.acquisition_arm TO service_role;
COMMENT ON TABLE app.acquisition_arm IS 'The banner A/B arm a learner signed up under (plan 7.10). Admins read via tauka-python. tauka-python writes.';
