-- §155 (2026-11-04, D15): installers are behind sign-in, so the `downloads` bucket is private.
-- Replaces §153, which made it public and was never applied to production. No client policy:
-- only the service role reads it, and download_service hands out short-lived signed links.
INSERT INTO storage.buckets (id, name, public, file_size_limit)
VALUES ('downloads', 'downloads', false, 524288000)
ON CONFLICT (id) DO UPDATE SET public = false;
