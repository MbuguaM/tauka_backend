-- ============================================================================
-- §129 (2026-10-19)  1:1 add-on purchases (enrolment plan 2.6, E-15; CONFLICT-060)
-- ============================================================================
--
-- student_tutor domain (the 1:1 lane, whose writes tauka-python owns, §73/§74).
--
-- Products are the pricing page's (authoritative, E-03): a single 1:1 of 50 or
-- 25 minutes ($26 / $13) and bundles of four ($99 / $49). One-time Stripe
-- checkouts, web only. Payment grants `session_entitlement` (reason
-- addon_purchase, never expiring). The purchase row's pending -> paid update is
-- the lock: a webhook retried twice grants once.
--
-- E-15: a learner with no tutor (Free, Phase 0) names a rung >= 2 active tutor
-- at checkout; payment creates a GUEST link to them (source 'addon'), so D-02
-- (book only your own roster) still holds. CONFLICT-060: one new value in the
-- CHECK on app.student_tutor_link.source (tauka-flutter's DDL).
--
-- Minutes are kept so a 25-minute purchase can't be booked as 50
-- (booking_service checks before booking).

CREATE TABLE IF NOT EXISTS app.addon_purchase (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id        uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  product           text NOT NULL CHECK (product IN ('one_to_one_50','one_to_one_25','bundle4_50','bundle4_25')),
  sessions          int  NOT NULL CHECK (sessions > 0),
  minutes           int  NOT NULL CHECK (minutes IN (25, 50)),
  tutor_id          uuid,
  amount_minor      int  NOT NULL CHECK (amount_minor >= 0),
  currency          text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','refunded','expired')),
  stripe_session_id text UNIQUE,
  created_at        timestamptz NOT NULL DEFAULT now(),
  paid_at           timestamptz,
  CONSTRAINT addon_purchase_paid CHECK ((status IN ('paid','refunded')) = (paid_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_addon_purchase_student ON app.addon_purchase (student_id, created_at);

ALTER TABLE app.addon_purchase ENABLE ROW LEVEL SECURITY;
CREATE POLICY addon_purchase_read ON app.addon_purchase FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.addon_purchase FROM anon, authenticated;
GRANT SELECT ON app.addon_purchase TO authenticated;
GRANT ALL ON app.addon_purchase TO service_role;
COMMENT ON TABLE app.addon_purchase IS '1:1 add-on purchases (plan 2.6): pricing-page products, one-time Stripe checkout; paid grants session_entitlement. tauka-python writes.';

-- CONFLICT-060: guest links created by an add-on purchase (E-15).
ALTER TABLE app.student_tutor_link DROP CONSTRAINT IF EXISTS student_tutor_link_source_check;
ALTER TABLE app.student_tutor_link ADD CONSTRAINT student_tutor_link_source_check
  CHECK (source IN ('referral','matched','admin','handoff','addon'));
