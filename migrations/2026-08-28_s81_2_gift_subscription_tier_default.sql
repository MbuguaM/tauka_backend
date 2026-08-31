-- =============================================================================
-- §81.2  app.gift_subscriptions.tier — finishing the §64 rename
--        Applied 2026-08-28 as part of migration
--        `s81_tier_default_rename_completion_and_cleanup`.
--        DOMAIN: test_referral. OWNER: tauka-python.
--
-- Filed here rather than in tauka-flutter's `additional_changes.sql` because
-- `gift_subscriptions` is tauka-python's table (shared-db/OWNERS.md:116), even
-- though it was found and fixed in the same pass as three tauka-flutter ones.
-- The full §81 narrative lives in `tauka/shared-db/schema/db.sql`.
-- =============================================================================
--
-- TWO DEFECTS, both left behind by §64's tier rename.
--
-- 1. DEFAULT was the bare `'tutor'`. CLAUDE.md forbids comparing a tier against
--    that string because it collides with the tutor ROLE
--    (`app.user_profiles.user_type`) — the collision CONFLICT-036 was filed
--    over. As a DEFAULT it was worse than a comparison: it would have WRITTEN
--    the ambiguous value.
--
-- 2. There was no CHECK constraint on the column at all, so nothing would ever
--    have rejected it. `app.student.tier`, `app.user_profiles.subscription_tier`
--    and `app.tier_gating_rules.min_tier` all have one; this table was the gap.
--
-- Impact: 0 rows, so nothing to repair. But a gift created without an explicit
-- tier would have carried a value that no `tier_gating_rules.min_tier` can
-- match — those hold `*_tier` values only — so the gift would have granted
-- nothing and the failure would have looked like a gating bug.
--
-- The new default is 'free_tier' and NOT 'tutor_tier'. A gift with no tier
-- named is not a gift of the tutor plan; it read that way only because the
-- value predated the rename.
--
-- Callers are unaffected: the only insert is `app/services/gift_service.py:67`,
-- which sets `"tier": "tutor_tier"` explicitly (gifts are always the tutor
-- plan). That is why this was latent rather than live. Its two sibling
-- columns in §81.1 were not so lucky — `app.user_profiles` and `app.student`
-- had callers that omitted the column, and every new-account insert failed.
-- See CONFLICT-045.

ALTER TABLE app.gift_subscriptions ALTER COLUMN tier SET DEFAULT 'free_tier';

ALTER TABLE app.gift_subscriptions
  ADD CONSTRAINT gift_subscriptions_tier_check
  CHECK (tier = ANY (ARRAY['free_tier','learner_tier','tutor_tier','intensive_tier']));
