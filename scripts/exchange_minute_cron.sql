-- §159 XE-9 (exchange-extension-plan.md, decision D-4): the exchange's every-minute clock.
--
-- NOT a migration, and never committed with the secret in it. Run once per project in the
-- Supabase SQL editor after replacing the two placeholders. pg_cron and pg_net are already
-- enabled on the project. The hourly GitHub Action runs the same work as a fallback, so
-- nothing breaks if this is never scheduled; rooms are just made later.
--
-- Store the secret in Vault rather than in the job text:
--   select vault.create_secret('<CRON_SECRET>', 'cron_secret');

select cron.schedule(
  'exchange-minute',
  '* * * * *',
  $$
  select net.http_get(
    url     := 'https://<API_HOST>/tasks/minute',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'cron_secret')
    ),
    timeout_milliseconds := 20000
  );
  $$
);

-- To stop it:  select cron.unschedule('exchange-minute');
-- To see runs: select * from cron.job_run_details where jobid = (select jobid from cron.job where jobname = 'exchange-minute') order by start_time desc limit 20;
