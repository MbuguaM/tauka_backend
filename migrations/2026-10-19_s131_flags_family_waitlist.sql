-- ============================================================================
-- §131 (2026-10-19)  Server-side flags; the family waitlist (enrolment plan 1.8)
-- ============================================================================
--
-- platform_config (feature_flag) and enrolment (waitlist_entry); tauka-python.
--
-- 1. Flags. Items built ahead of their blocker (developer, 2026-10-19: "for any
--    blocking items use your recommendation to build them") are seeded
--    'coming'; `app.core.flags.is_ready` refuses them server-side until a
--    developer flips the row to 'ready'.
--
-- 2. The family waitlist (1.8, L-012 still OPEN). L-012's own proposal: a
--    parent leaves THEIR email and the child's age band only (15, 16 or 17),
--    never a name or date of birth. Behind the 'family_waitlist' flag; the web
--    shows the branch and /policy §8's family sentence only when it's ready.

INSERT INTO app.feature_flag (key, status, description) VALUES
  ('family_waitlist',     'coming', 'Plan 1.8: a parent joins a family waitlist for a 15-17-year-old (L-012).'),
  ('gender_matching',     'coming', 'Plan B8/3.3: gender as a matching preference or requirement (L-010).'),
  ('whatsapp_reminders',  'coming', 'Plan 6.1: WhatsApp reminders through a BSP (L-011).'),
  ('peer_sessions',       'coming', 'Track 5: peer sessions between pods.'),
  ('mpesa_payments',      'coming', 'Plan 2.4: M-Pesa STK push.'),
  ('addons_1v1',          'coming', 'Plan 2.6: 1:1 add-on purchases.'),
  ('exit_assessment',     'coming', 'Track 10: exit assessments and certificates.'),
  ('gift_cards',          'coming', 'Track 10: gift cards as a product.'),
  ('institutions',        'coming', 'Track 10: B2B contracts, invoices and employer reports.'),
  ('tutor_public_profiles','coming', 'Plan 8.8: public, indexable tutor profiles.'),
  ('dated_banner',        'coming', 'Track 7: the dated-promise banner.')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE app.waitlist_entry ADD COLUMN IF NOT EXISTS child_age_band text
  CHECK (child_age_band IS NULL OR child_age_band IN ('15','16','17'));
COMMENT ON COLUMN app.waitlist_entry.child_age_band IS
  'Family waitlist (plan 1.8, L-012): the CHILD''s age in years, from their parent. Never a name or date of birth. NULL = an adult''s own entry.';
