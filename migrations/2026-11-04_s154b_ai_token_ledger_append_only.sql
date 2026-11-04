-- §154b The ledger is append-only for the service role too. Supabase's default privileges
-- gave it UPDATE, DELETE and TRUNCATE on the new table, and §154's REVOKE didn't name it.
REVOKE ALL ON app.ai_token_ledger FROM service_role;
GRANT SELECT, INSERT ON app.ai_token_ledger TO service_role;
