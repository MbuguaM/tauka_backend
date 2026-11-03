-- §152 (2026-11-03): retire store and checkout rows from app.config.
-- The app stopped reading them (ALL-2, ALL-4): store links are build-time now, in StoreLinks.
-- All three held placeholders. min_version stays, and min_version_<platform> may be added.
DELETE FROM app.config WHERE key IN ('stripe_checkout_url', 'ios_store_url', 'android_store_url');
