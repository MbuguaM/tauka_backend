-- ============================================================================
-- §137 (2026-10-19)  Benchmark snapshots for the ops dashboard (enrolment plan Track 9)
-- ============================================================================
--
-- platform_config domain; tauka-python writes. One row per formation day: the
-- benchmark values after that formation closed, so the dashboard can apply the
-- playbook's "two consecutive formations" rule. Display only (E-16).

CREATE TABLE IF NOT EXISTS app.benchmark_snapshot (
  taken_on   date PRIMARY KEY,
  "values"   jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.benchmark_snapshot ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.benchmark_snapshot FROM anon, authenticated;
GRANT ALL ON app.benchmark_snapshot TO service_role;
COMMENT ON TABLE app.benchmark_snapshot IS 'Benchmark values after each formation (plan Track 9), for the two-consecutive-formations rule. Read by ops through tauka-python.';
