-- ============================================================================
-- §134 (2026-10-19)  The tutor side (enrolment plan 8.3, 8.4, 8.5, 8.7, 8.8)
-- ============================================================================
--
-- tutor_management domain; tauka-python writes everything here.
--
-- 8.3  tutor_commitment_block: weekly hours a tutor commits for a unit
--      (~5 weeks, at least 6 h/week, within their availability).
--      tutor_holiday: time off, declared 8 weeks ahead (shorter notice needs
--      ops approval). A tutor on an approved holiday is not offered pods,
--      cover or standby for those days.
-- 8.4  tutor_work_item: paid work that isn't a session (a gathering, a QA
--      observation, other), recorded by ops or by the system. The run prices
--      every type against the rate card (config payout.type_rates_bp) and adds
--      the connectivity stipend (payout.connectivity_stipend_minor).
--      payout_line.breakdown: how each line was made, for the tutor and ops.
-- 8.5  student_payment: what a learner actually paid (Stripe invoices, M-Pesa,
--      add-ons). E-11: a referring tutor earns a FLAT 10% of their referred
--      learner's payments for 12 months from signup, from the marketing budget
--      (config payout.referral_rule = 'commission', the default now).
-- 8.7  qa_review: once a month each active tutor has one upcoming pod session
--      sampled for a live observation by a rung >= 3 reviewer (recording stays
--      blocked, L-004), scored on a fixed rubric (1-4 per item). It informs rung
--      promotion; it never moves pay or matching.
-- 8.8  tutor_public_profile: what a tutor publishes about themselves on an
--      indexable page (first name, headline, bio, languages). Off until they
--      publish it, and behind the tutor_public_profiles flag.

CREATE TABLE IF NOT EXISTS app.student_payment (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  source        text NOT NULL CHECK (source IN ('stripe_invoice','mpesa','addon')),
  external_ref  text NOT NULL UNIQUE,
  amount_minor  bigint NOT NULL CHECK (amount_minor >= 0),
  currency      text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  paid_at       timestamptz NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_student_payment_student ON app.student_payment (student_id, paid_at);

CREATE TABLE IF NOT EXISTS app.tutor_work_item (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id    uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('gathering','qa_observation','other')),
  quantity    int  NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 50),
  occurred_on date NOT NULL,
  ref_code    text CHECK (ref_code IS NULL OR ref_code ~ '^[A-Za-z0-9_./:-]{1,60}$'),
  added_by    uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tutor_id, kind, ref_code)
);
CREATE INDEX IF NOT EXISTS idx_tutor_work_item_tutor ON app.tutor_work_item (tutor_id, occurred_on);

ALTER TABLE app.payout_line ADD COLUMN IF NOT EXISTS breakdown jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS app.tutor_commitment_block (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id       uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  starts_on      date NOT NULL,
  ends_on        date NOT NULL,
  hours_per_week int  NOT NULL CHECK (hours_per_week BETWEEN 1 AND 60),
  status         text NOT NULL DEFAULT 'committed' CHECK (status IN ('committed','released')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tutor_commitment_block_span CHECK (ends_on > starts_on AND ends_on - starts_on <= 70)
);
CREATE INDEX IF NOT EXISTS idx_tutor_commitment_block_tutor ON app.tutor_commitment_block (tutor_id, starts_on);

CREATE TABLE IF NOT EXISTS app.tutor_holiday (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id    uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  starts_on   date NOT NULL,
  ends_on     date NOT NULL,
  status      text NOT NULL DEFAULT 'approved' CHECK (status IN ('approved','needs_approval','declined','cancelled')),
  decided_by  uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tutor_holiday_span CHECK (ends_on >= starts_on AND ends_on - starts_on <= 92)
);
CREATE INDEX IF NOT EXISTS idx_tutor_holiday_tutor ON app.tutor_holiday (tutor_id, starts_on);

CREATE TABLE IF NOT EXISTS app.qa_review (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id     uuid NOT NULL REFERENCES app.tutor(id) ON DELETE CASCADE,
  session_type text NOT NULL CHECK (session_type IN ('cohort_session')),
  session_id   uuid NOT NULL,
  session_at   timestamptz NOT NULL,
  reviewer_id  uuid NOT NULL,
  month        date NOT NULL,
  status       text NOT NULL DEFAULT 'assigned' CHECK (status IN ('assigned','done','skipped')),
  -- Fixed rubric, 1-4 each: structure, speaking_time, correction, warmth, target_language.
  scores       jsonb,
  done_at      timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tutor_id, month),
  CONSTRAINT qa_review_not_self CHECK (reviewer_id <> tutor_id),
  CONSTRAINT qa_review_done CHECK ((status = 'done') = (scores IS NOT NULL AND done_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS app.tutor_public_profile (
  tutor_id    uuid PRIMARY KEY REFERENCES app.tutor(id) ON DELETE CASCADE,
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,60}$'),
  headline    text CHECK (headline IS NULL OR length(headline) <= 120),
  bio         text CHECK (bio IS NULL OR length(bio) <= 1200),
  published   boolean NOT NULL DEFAULT false,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE app.student_payment ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tutor_work_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tutor_commitment_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tutor_holiday ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.qa_review ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tutor_public_profile ENABLE ROW LEVEL SECURITY;
CREATE POLICY student_payment_read ON app.student_payment FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
CREATE POLICY tutor_work_item_read ON app.tutor_work_item FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
CREATE POLICY tutor_commitment_block_read ON app.tutor_commitment_block FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
CREATE POLICY tutor_holiday_read ON app.tutor_holiday FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
-- The observed tutor sees the scores, the reviewer sees their own assignment.
CREATE POLICY qa_review_read ON app.qa_review FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR reviewer_id = auth.uid() OR app.is_admin());
CREATE POLICY tutor_public_profile_read ON app.tutor_public_profile FOR SELECT TO anon, authenticated
  USING (published OR tutor_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.student_payment, app.tutor_work_item, app.tutor_commitment_block, app.tutor_holiday,
              app.qa_review, app.tutor_public_profile FROM anon, authenticated;
GRANT SELECT ON app.student_payment, app.tutor_work_item, app.tutor_commitment_block, app.tutor_holiday,
               app.qa_review, app.tutor_public_profile TO authenticated;
GRANT SELECT ON app.tutor_public_profile TO anon;
GRANT ALL ON app.student_payment, app.tutor_work_item, app.tutor_commitment_block, app.tutor_holiday,
              app.qa_review, app.tutor_public_profile TO service_role;
COMMENT ON TABLE app.student_payment IS 'What a learner paid (plan 8.5, E-11): Stripe invoices, M-Pesa, add-ons. The base of the referral commission. tauka-python writes.';
COMMENT ON TABLE app.tutor_work_item IS 'Paid tutor work that is not a session (plan 8.4): gatherings, QA observations. tauka-python writes.';
COMMENT ON TABLE app.tutor_commitment_block IS 'Weekly hours a tutor commits for a unit (plan 8.3). tauka-python writes.';
COMMENT ON TABLE app.tutor_holiday IS 'Tutor time off, 8 weeks ahead (plan 8.3). tauka-python writes.';
COMMENT ON TABLE app.qa_review IS 'Monthly live-observation QA sample per tutor (plan 8.7), fixed rubric. Never a pay or matching gate. tauka-python writes.';
COMMENT ON TABLE app.tutor_public_profile IS 'A tutor''s published public profile (plan 8.8). Readable by anyone once published. tauka-python writes.';
