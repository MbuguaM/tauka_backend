-- ============================================================================
-- §121 (2026-10-18)  Account lookup for tutor approval; close an email probe
--                    (enrolment plan §2j J3; FC-044)
-- ============================================================================
--
-- 1. app.user_id_by_email(text): approving a tutor application must know
--    whether the applicant already has an account (J1, decided: promote the
--    same account). supabase-py has no lookup by email, so this reads
--    auth.users. SERVICE ROLE ONLY: it would otherwise be an account-existence
--    probe.
--
-- 2. FC-044. public.check_user_exists(email) — undocumented, called by nothing in
--    any of the three projects — was executable by PUBLIC, anon and
--    authenticated: anyone could ask whether an email has a Tauka account. Its
--    EXECUTE grant is now service_role only. The function is left in place
--    (dropping it is a decision for whoever created it).

CREATE OR REPLACE FUNCTION app.user_id_by_email(p_email text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'auth', 'pg_catalog'
AS $$ SELECT id FROM auth.users WHERE lower(email) = lower(p_email) LIMIT 1; $$;
REVOKE ALL ON FUNCTION app.user_id_by_email(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION app.user_id_by_email(text) TO service_role;

REVOKE EXECUTE ON FUNCTION public.check_user_exists(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_user_exists(text) TO service_role;
-- Applied to production 2026-10-18 as migration s121_account_lookup.
