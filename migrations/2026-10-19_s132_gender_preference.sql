-- ============================================================================
-- §132 (2026-10-19)  Gender as a matching preference or requirement (plan B8, 3.3, 5.2)
-- ============================================================================
--
-- cohort_matching domain; tauka-python writes. Behind the 'gender_matching'
-- flag (§131): until a developer turns it on, nothing is read or written, and
-- L-010 stays OPEN.
--
-- Playbook: "Gender requirement (hard only if marked as a requirement)". One
-- row per person (learners AND tutors state their own): a self-described
-- gender, and for learners none / preference / requirement for a same-gender
-- tutor and a same-gender pod. A requirement is a hard filter in the matcher
-- (deferral NO_SAME_GENDER_MATCH); a preference is a soft score term that
-- collapses at low volume like the others.

CREATE TABLE IF NOT EXISTS app.gender_preference (
  user_id           uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  gender            text CHECK (gender IS NULL OR gender IN ('woman','man','nonbinary','prefer_not')),
  same_gender_tutor text NOT NULL DEFAULT 'none' CHECK (same_gender_tutor IN ('none','preference','requirement')),
  same_gender_pod   text NOT NULL DEFAULT 'none' CHECK (same_gender_pod IN ('none','preference','requirement')),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  -- A requirement needs a stated gender to compare against.
  CONSTRAINT gender_preference_requirement CHECK (
    (same_gender_tutor <> 'requirement' AND same_gender_pod <> 'requirement')
    OR gender IN ('woman','man','nonbinary'))
);

ALTER TABLE app.gender_preference ENABLE ROW LEVEL SECURITY;
CREATE POLICY gender_preference_read ON app.gender_preference FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.gender_preference FROM anon, authenticated;
GRANT SELECT ON app.gender_preference TO authenticated;
GRANT ALL ON app.gender_preference TO service_role;
COMMENT ON TABLE app.gender_preference IS 'Gender and same-gender preferences for matching (plan B8, L-010). Behind the gender_matching flag. tauka-python writes; the person and admins read.';

ALTER TABLE app.deferred_student DROP CONSTRAINT IF EXISTS deferred_student_reason_check;
ALTER TABLE app.deferred_student ADD CONSTRAINT deferred_student_reason_check CHECK (reason IN (
  'NO_TUTOR_FOR_LEVEL','NO_SLOT_OVERLAP','TZ_ISOLATE','TOO_FEW_PEERS_AT_LEVEL','TUTORS_AT_CAPACITY',
  'DECLINED_ALL_OFFERS','NO_TIMEZONE','TOO_FEW_PREFERENCES','NO_LEVEL','COHORT_DID_NOT_FILL','OFFERS_EXPIRED',
  'GROUP_WAITING','GROUP_LEVEL_GAP','NO_SAME_GENDER_MATCH'));
