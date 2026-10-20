-- ============================================================================
-- §139 (2026-10-19)  Institutions, community partners, gift cards (enrolment plan Track 10)
-- ============================================================================
--
-- enrolment domain (commercial); tauka-python writes everything here.
--
-- INSTITUTIONS (behind 'institutions'): NGOs, embassies, firms. A contract
--   (seats, tier, term, price) invoiced through Stripe (send_invoice: card or
--   bank transfer); staff join with the contract's code and get the contract's
--   tier with no subscription of their own. Employer reports (progress,
--   attendance) include ONLY members who granted the 'employer_reporting'
--   consent (consent_record) — the playbook's rule. Private pods for the team
--   are formed by ops as for any private-pod group (§128).
-- COMMUNITY PARTNERS: an organisation with a referral code (?pref=CODE); a
--   learner who signs up through it is attributed once; the partner's
--   commission is a flat share of that learner's payments for 12 months (the
--   E-11 shape), reported as a monthly statement ops pays by hand.
-- GIFT CARDS (behind 'gift_cards'): a product separate from the supporter
--   gift. Bought by anyone for N months of a tier; a code the recipient
--   redeems; it never expires unredeemed for 12 months.

CREATE TABLE IF NOT EXISTS app.organization (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL CHECK (length(name) BETWEEN 2 AND 160),
  contact_email text NOT NULL CHECK (position('@' in contact_email) > 1),
  country_code  text CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
  stripe_customer_id text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.org_contract (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES app.organization(id) ON DELETE CASCADE,
  tier          text NOT NULL CHECK (tier IN ('learner_tier','tutor_tier','intensive_tier')),
  seats         int  NOT NULL CHECK (seats BETWEEN 1 AND 500),
  starts_on     date NOT NULL,
  ends_on       date NOT NULL,
  price_minor   bigint NOT NULL CHECK (price_minor >= 0),
  currency      text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  join_code     text NOT NULL UNIQUE CHECK (join_code ~ '^[A-HJ-NP-Z2-9]{8}$'),
  status        text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','ended')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT org_contract_span CHECK (ends_on > starts_on)
);

CREATE TABLE IF NOT EXISTS app.org_member (
  contract_id   uuid NOT NULL REFERENCES app.org_contract(id) ON DELETE CASCADE,
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  joined_at     timestamptz NOT NULL DEFAULT now(),
  left_at       timestamptz,
  PRIMARY KEY (contract_id, student_id)
);

CREATE TABLE IF NOT EXISTS app.org_invoice (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contract_id       uuid NOT NULL REFERENCES app.org_contract(id) ON DELETE CASCADE,
  amount_minor      bigint NOT NULL CHECK (amount_minor >= 0),
  currency          text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  stripe_invoice_id text UNIQUE,
  status            text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','paid','void')),
  issued_at         timestamptz NOT NULL DEFAULT now(),
  paid_at           timestamptz
);

CREATE TABLE IF NOT EXISTS app.partner (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL CHECK (length(name) BETWEEN 2 AND 160),
  code           text NOT NULL UNIQUE CHECK (code ~ '^[A-Za-z0-9_-]{4,40}$'),
  contact_email  text,
  commission_bp  int  NOT NULL DEFAULT 1000 CHECK (commission_bp BETWEEN 0 AND 5000),
  active         boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.partner_signup (
  student_id   uuid PRIMARY KEY REFERENCES app.student(id) ON DELETE CASCADE,
  partner_id   uuid NOT NULL REFERENCES app.partner(id) ON DELETE RESTRICT,
  signed_up_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.gift_card (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code              text UNIQUE CHECK (code IS NULL OR code ~ '^[A-HJ-NP-Z2-9]{12}$'),
  tier              text NOT NULL CHECK (tier IN ('learner_tier','tutor_tier','intensive_tier')),
  months            int  NOT NULL CHECK (months BETWEEN 1 AND 12),
  amount_minor      bigint NOT NULL CHECK (amount_minor >= 0),
  currency          text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
  purchaser_email   text NOT NULL,
  message           text CHECK (message IS NULL OR length(message) <= 300),
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','redeemed','refunded','expired')),
  stripe_session_id text UNIQUE,
  redeemed_by       uuid REFERENCES app.student(id) ON DELETE SET NULL,
  redeemed_at       timestamptz,
  access_until      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT gift_card_paid_has_code CHECK (status = 'pending' OR code IS NOT NULL)
);

ALTER TABLE app.consent_record DROP CONSTRAINT IF EXISTS consent_record_purpose_check;
ALTER TABLE app.consent_record ADD CONSTRAINT consent_record_purpose_check
  CHECK (purpose IN ('session_recording','pronunciation_profile','model_training','tutor_voice_review',
                     'employer_reporting'));

ALTER TABLE app.organization ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.org_contract ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.org_member ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.org_invoice ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.partner ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.partner_signup ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.gift_card ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_member_read ON app.org_member FOR SELECT TO authenticated USING (student_id = auth.uid() OR app.is_admin());
CREATE POLICY gift_card_read ON app.gift_card FOR SELECT TO authenticated USING (redeemed_by = auth.uid() OR app.is_admin());
REVOKE ALL ON app.organization, app.org_contract, app.org_member, app.org_invoice, app.partner, app.partner_signup,
              app.gift_card FROM anon, authenticated;
GRANT SELECT ON app.org_member, app.gift_card TO authenticated;
GRANT ALL ON app.organization, app.org_contract, app.org_member, app.org_invoice, app.partner, app.partner_signup,
              app.gift_card TO service_role;
COMMENT ON TABLE app.org_contract IS 'An institution''s contract (Track 10): seats, tier, term, price; staff join with its code. tauka-python writes.';
COMMENT ON TABLE app.partner IS 'A community partner with a referral code (Track 10). tauka-python writes.';
COMMENT ON TABLE app.gift_card IS 'A gift card product (Track 10), separate from the supporter gift. tauka-python writes.';
