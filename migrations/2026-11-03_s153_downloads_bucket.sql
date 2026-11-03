-- §153 (2026-11-03): public bucket for desktop installers and the update feed (WIN-1, WIN-4, MAC-4).
-- Readable by URL. No client policy: only the service role and the dashboard can write or list.
INSERT INTO storage.buckets (id, name, public, file_size_limit)
VALUES ('downloads', 'downloads', true, 524288000)
ON CONFLICT (id) DO NOTHING;
