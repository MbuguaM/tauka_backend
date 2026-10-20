-- ============================================================================
-- §96 (2026-10-18)  No client role may TRUNCATE an app table (CONFLICT-053)
-- ============================================================================
-- The schema's default privileges (FOR ROLE postgres IN SCHEMA app) granted
-- anon and authenticated ALL on every new table, TRUNCATE included, and
-- TRUNCATE is checked against neither RLS nor row triggers — so a role that RLS
-- limits to its own rows could empty an append-only ledger. Before: 113 of 115
-- app tables. After: 0. service_role keeps TRUNCATE. Nothing in the database
-- calls TRUNCATE (checked: no function body mentions it), and PostgREST has no
-- verb for it, so no client path loses anything. Approved by the developer.
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA app FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA app
  REVOKE TRUNCATE ON TABLES FROM anon, authenticated;
-- Applied to production 2026-10-18 as migration s96_revoke_client_truncate.