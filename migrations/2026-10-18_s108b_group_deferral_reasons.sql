-- §108b (2026-10-18)  Two deferral reasons for friend groups (plan 3.2)
-- cohort_matching (tauka-python). GROUP_WAITING: a friend in the group isn't in
-- this formation's pool yet, so the others wait for them (playbook: "the faster
-- friend waits for the slower one"). GROUP_LEVEL_GAP: members are at different
-- levels, and a pod has one level — they're told rather than split silently.
ALTER TABLE app.deferred_student DROP CONSTRAINT IF EXISTS deferred_student_reason_check;
ALTER TABLE app.deferred_student ADD CONSTRAINT deferred_student_reason_check CHECK (reason IN (
  'NO_TUTOR_FOR_LEVEL','NO_SLOT_OVERLAP','TZ_ISOLATE','TOO_FEW_PEERS_AT_LEVEL','TUTORS_AT_CAPACITY',
  'DECLINED_ALL_OFFERS','NO_TIMEZONE','TOO_FEW_PREFERENCES','NO_LEVEL','COHORT_DID_NOT_FILL','OFFERS_EXPIRED',
  'GROUP_WAITING','GROUP_LEVEL_GAP'));
-- Applied to production 2026-10-18 as migration s108b_group_deferral_reasons.
