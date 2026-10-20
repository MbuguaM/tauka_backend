-- ============================================================================
-- §100 (2026-10-18)  Tir 2019 is the founding intake; Hidar is a rehearsal
-- ============================================================================
--
-- Developer, 2026-10-18: "Tir is the founding pod." §99 already made Hidar
-- 2019 a rehearsal (a simulated pod, never targeted). This moves the founding
-- label to Tir (formation Sat 9 Jan 2027, lesson deadline 28 Dec – 5 Jan) and
-- strips it from Hidar's intake and season.
--
-- Data only (enrolment domain, tauka-python). Tir stays in Bega 2019, which is
-- the season it falls in; the founding pod is an intake-level fact, so no
-- separate founding season is needed any more. The season `kind` CHECK keeps
-- 'founding' allowed; nothing uses it now.

UPDATE app.intake SET tier = 'founding'
 WHERE ethiopic_month = 'tir' AND ec_year = 2019;

UPDATE app.intake SET tier = 'light'
 WHERE ethiopic_month = 'hidar' AND ec_year = 2019;

-- The former "Hidar 2019 (founding)" season holds only the rehearsal intake.
UPDATE app.season
   SET kind = 'regular',
       display_name = 'Tsedey 2019 (rehearsal)',
       gathers_with_season_id = NULL
 WHERE season_name = 'tsedey' AND ec_year = 2019 AND kind = 'founding';

-- Exactly one founding intake, enforced from now on.
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_one_founding
  ON app.intake ((true)) WHERE tier = 'founding';
-- Applied to production 2026-10-18 as migration s100_tir_founding_intake.
