-- ============================================================================
-- §101 (2026-10-18)  Tahsas 2019 cancelled: Tir is the first real formation
-- ============================================================================
--
-- Developer, 2026-10-18: "cancel tahsas". Tahsas (10 Dec 2026) was a live
-- intake forming before the founding one (Tir, 9 Jan 2027, §100), so anyone
-- starting Phase 0 now was aimed at it. Cancelled, it is skipped by
-- phase0_service (status must be 'scheduled'); learners are aimed at Tir, and
-- any row already targeting Tahsas is retargeted on its next read.
--
-- Data only (enrolment domain, tauka-python). The row is kept, not deleted:
-- the calendar's history stays explicable.

UPDATE app.intake SET status = 'cancelled'
 WHERE ethiopic_month = 'tahsas' AND ec_year = 2019 AND status = 'scheduled';
-- Applied to production 2026-10-18 as migration s101_cancel_tahsas.
