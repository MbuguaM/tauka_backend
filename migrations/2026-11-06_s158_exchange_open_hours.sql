-- §158 Exchange: partners' open hours, "free to chat now", free replacement after a no-show
-- (2026-11-06, developer). Owner: tauka-python. Domain: exchange.
--
-- Partners used to offer only slot_catalogue times (16 a week, the enrolment playbook's
-- example catalogue for pods). An exchange is one learner and one partner, so nothing needs
-- packing into shared times: partners now mark any hours of the week they're free, in their
-- own time zone, and learners book any half hour a partner is free. The catalogue stays as
-- "popular times" hints. exchange_availability is superseded; kept read-only for one release.
--
--   exchange_partner_hour      one row per (partner, weekday, hour) they're free, wall clock
--                              in exchange_partner.timezone (so clock changes never shift it)
--   exchange_partner.timezone  IANA zone those hours are in
--   exchange_partner.weekly_cap  at most this many booked sessions a week (1-14, default 3);
--                              chat-now sessions don't count, the partner switched those on
--   exchange_queue.free_until  a partner's "free to chat now" toggle (30 min to 2 h)
--   exchange_session.replaces_id  a chat that replaces a session the other person didn't
--                              come to; it doesn't use the learner's allowance
-- Nothing here gates on activity: "free now" is a switch the person flips, never presence.

ALTER TABLE app.exchange_partner
  ADD COLUMN IF NOT EXISTS timezone text CHECK (timezone IS NULL OR length(timezone) <= 64),
  ADD COLUMN IF NOT EXISTS weekly_cap smallint NOT NULL DEFAULT 3 CHECK (weekly_cap BETWEEN 1 AND 14);

CREATE TABLE IF NOT EXISTS app.exchange_partner_hour (
  user_id     uuid NOT NULL REFERENCES app.exchange_partner(user_id) ON DELETE CASCADE,
  day_of_week smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),   -- 0 = Monday, in exchange_partner.timezone
  hour        smallint NOT NULL CHECK (hour BETWEEN 0 AND 23),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, day_of_week, hour)
);
ALTER TABLE app.exchange_partner_hour ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS exchange_partner_hour_own ON app.exchange_partner_hour;
CREATE POLICY exchange_partner_hour_own ON app.exchange_partner_hour FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.exchange_partner_hour FROM anon, authenticated;
GRANT SELECT ON app.exchange_partner_hour TO authenticated;
GRANT ALL ON app.exchange_partner_hour TO service_role;
COMMENT ON TABLE app.exchange_partner_hour IS
  'Hours of the week an exchange partner is free (§158), in exchange_partner.timezone. tauka-python writes.';

ALTER TABLE app.exchange_queue ADD COLUMN IF NOT EXISTS free_until timestamptz;
COMMENT ON COLUMN app.exchange_queue.free_until IS
  'A partner''s "free to chat now" switch (§158). Null = the old 3-minute practise-now wait.';

ALTER TABLE app.exchange_session
  ADD COLUMN IF NOT EXISTS replaces_id uuid REFERENCES app.exchange_session(id) ON DELETE SET NULL;
COMMENT ON COLUMN app.exchange_session.replaces_id IS
  'The session this chat replaces after the other person didn''t come (§158). Not counted in the allowance.';

-- Existing partners keep what they picked: each catalogue slot becomes the hour it falls in,
-- in the partner's zone (Addis Ababa where none is known yet; the Times page saves the real
-- one). The reference week is fixed, so the conversion is the same on every run.
UPDATE app.exchange_partner SET timezone = 'Africa/Addis_Ababa'
 WHERE timezone IS NULL AND user_id IN (SELECT user_id FROM app.exchange_availability);
INSERT INTO app.exchange_partner_hour (user_id, day_of_week, hour)
SELECT a.user_id, (extract(isodow FROM x.l) - 1)::smallint, extract(hour FROM x.l)::smallint
  FROM app.exchange_availability a
  JOIN app.slot_catalogue c ON c.id = a.slot_id
  JOIN app.exchange_partner p ON p.user_id = a.user_id
 CROSS JOIN LATERAL (SELECT ((date '2026-11-09' + c.day_of_week + c.start_time) AT TIME ZONE c.anchor_timezone)
                            AT TIME ZONE coalesce(p.timezone, 'Africa/Addis_Ababa') AS l) x
ON CONFLICT DO NOTHING;

-- delete_user_account: also the open hours, and §157's country and this migration's zone.
-- Everything else is the function as it was (pg_get_functiondef of the §142 version).
CREATE OR REPLACE FUNCTION app.delete_user_account(p_user uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'app', 'public'
AS $function$
DECLARE
  v_storage    jsonb := '[]'::jsonb;
  v_retained   jsonb := '[]'::jsonb;
  v_n          int;
BEGIN
  IF p_user IS NULL THEN
    RAISE EXCEPTION 'delete_user_account: p_user is required';
  END IF;

  -- Caller check. The Edge Function runs as the service role, where auth.uid()
  -- is NULL; a signed-in caller may only erase themselves, and an admin anyone.
  IF auth.uid() IS NOT NULL AND auth.uid() <> p_user AND NOT app.is_admin() THEN
    RAISE EXCEPTION 'delete_user_account: not permitted';
  END IF;

  -- ── 1. Collect Storage paths BEFORE deleting the rows that name them ──────
  --
  -- This is the step CONFLICT-029 is about. `activity_file` rows cascade on
  -- account deletion; the JSONL objects in the `activity` bucket do not, and
  -- sat there afterwards. legal.md:223 records that "the claim that account
  -- deletion actually deletes was false for anything already uploaded". Reading
  -- the paths out first is what makes the claim true.

  SELECT coalesce(jsonb_agg(jsonb_build_object('bucket','activity','path',path)), '[]'::jsonb)
    INTO v_storage FROM app.activity_file WHERE user_id = p_user AND path IS NOT NULL;

  v_storage := v_storage || coalesce(
    (SELECT jsonb_agg(jsonb_build_object('bucket','voice-recordings','path',storage_path))
       FROM app.voice_recordings WHERE user_id = p_user AND storage_path IS NOT NULL), '[]'::jsonb);

  v_storage := v_storage || coalesce(
    (SELECT jsonb_agg(jsonb_build_object('bucket','voice-recordings','path',storage_path))
       FROM app.speech_submission WHERE user_id = p_user AND storage_path IS NOT NULL), '[]'::jsonb);

  -- §142: a partner's 2-minute English answer (X-12).
  v_storage := v_storage || coalesce(
    (SELECT jsonb_agg(jsonb_build_object('bucket','voice-recordings','path',english_sample_path))
       FROM app.exchange_partner WHERE user_id = p_user AND english_sample_path IS NOT NULL), '[]'::jsonb);

  -- DELIBERATELY NOT COLLECTED: app.session_recordings and
  -- app.cohort_session.recording_path. A one-to-one or cohort recording is also
  -- the tutor's and the other students' record of that session; erasing one
  -- participant must not delete the other participants' copy. legal.md §281-296
  -- has open consent questions on recordings that this function must not
  -- pre-empt by quietly deleting. Recorded so the omission reads as a decision.

  -- ── 2. The user's OWN rows, on tables whose FK does not cascade ───────────

  DELETE FROM app.exercise_result          WHERE user_id = p_user;
  DELETE FROM app.handwriting_session      WHERE user_id = p_user;
  DELETE FROM app.anki_deck_assignment     WHERE assigned_by = p_user OR assigned_to_user_id = p_user;
  DELETE FROM app.milestone_notifications  WHERE student_id = p_user;
  DELETE FROM app.test_supporters          WHERE student_id = p_user;
  -- §142 exchange: their own availability, phrases, queue place, blocks they set,
  -- and the free text of ratings they wrote (the scores stay: they are the other
  -- person's record too). Their partner row is anonymised by the profile cascade.
  DELETE FROM app.exchange_availability    WHERE user_id = p_user;
  DELETE FROM app.exchange_partner_hour    WHERE user_id = p_user;   -- §158
  DELETE FROM app.exchange_phrase          WHERE user_id = p_user;
  DELETE FROM app.exchange_queue           WHERE user_id = p_user;
  DELETE FROM app.exchange_favourite       WHERE learner_id = p_user OR partner_id = p_user;
  UPDATE app.exchange_rating SET note = NULL, phrase = NULL WHERE rater_id = p_user;
  UPDATE app.exchange_partner SET english_sample_path = NULL, region = NULL, country_code = NULL, timezone = NULL, variety_note = NULL,
         interests = '{}' WHERE user_id = p_user;
  DELETE FROM app.test_referrals           WHERE sender_student_id = p_user;
  DELETE FROM app.video_session_participant WHERE student_id = p_user;
  DELETE FROM app.course_correction        WHERE tutor_id = p_user;
  DELETE FROM app.yt_playlist              WHERE user_id = p_user;

  -- ── 3. Other people's rows that merely NAME this user ────────────────────
  --
  -- Nulled, never deleted. Deleting a class because the account that created it
  -- was erased, or an admin row because its creator left, would destroy other
  -- people's data to satisfy one person's erasure.

  UPDATE app.admin_users           SET created_by = NULL           WHERE created_by = p_user;
  UPDATE app.classes               SET created_by = NULL           WHERE created_by = p_user;
  UPDATE app.course_correction     SET reviewed_by = NULL          WHERE reviewed_by = p_user;
  UPDATE app.user_profiles         SET suspended_by = NULL         WHERE suspended_by = p_user;
  UPDATE app.test_sessions         SET referrer_student_id = NULL  WHERE referrer_student_id = p_user;
  UPDATE app.yt_playlist           SET submitted_by = NULL         WHERE submitted_by = p_user;
  UPDATE app.yt_playlist           SET reviewed_by = NULL          WHERE reviewed_by = p_user;
  UPDATE app.yt_video              SET timing_submitted_by = NULL  WHERE timing_submitted_by = p_user;
  UPDATE app.yt_video              SET timing_reviewed_by = NULL   WHERE timing_reviewed_by = p_user;

  -- ── 4. What must be RETAINED, and therefore blocks a hard delete ─────────
  --
  -- Financial and contractual records. GDPR Art.17(3)(b) and (e) recognise
  -- retention against an erasure request where it is required for a legal
  -- obligation or the establishment of legal claims — which is what a payout
  -- ledger and a signed commitment are. The RESTRICT constraints already
  -- encoded this intent; this makes it explicit and, more importantly, makes
  -- the account anonymous rather than merely un-deletable.

  SELECT coalesce(jsonb_agg(t), '[]'::jsonb) INTO v_retained FROM (
    SELECT 'payout_line' AS t         WHERE EXISTS (SELECT 1 FROM app.payout_line WHERE tutor_id = p_user)
    UNION ALL SELECT 'referral_signup' WHERE EXISTS (SELECT 1 FROM app.referral_signup WHERE tutor_id = p_user OR student_id = p_user)
    UNION ALL SELECT 'tutor_rate_version' WHERE EXISTS (SELECT 1 FROM app.tutor_rate_version WHERE tutor_id = p_user)
    UNION ALL SELECT 'tutor_standing'  WHERE EXISTS (SELECT 1 FROM app.tutor_standing WHERE tutor_id = p_user)
    UNION ALL SELECT 'tutor_feedback'  WHERE EXISTS (SELECT 1 FROM app.tutor_feedback WHERE tutor_id = p_user)
    UNION ALL SELECT 'gift_subscriptions' WHERE EXISTS (SELECT 1 FROM app.gift_subscriptions WHERE student_id = p_user)
    UNION ALL SELECT 'enrollment_commitment' WHERE EXISTS (SELECT 1 FROM app.enrollment_commitment WHERE student_id = p_user OR locked_home_tutor_id = p_user)
    UNION ALL SELECT 'one_to_one_session' WHERE EXISTS (SELECT 1 FROM app.one_to_one_session WHERE student_id = p_user OR tutor_id = p_user)
    UNION ALL SELECT 'cohort_payments' WHERE EXISTS (SELECT 1 FROM app.cohort_payments WHERE student_id = p_user OR tutor_id = p_user)
    UNION ALL SELECT 'video_session'   WHERE EXISTS (SELECT 1 FROM app.video_session WHERE tutor_id = p_user)
    UNION ALL SELECT 'student_tutor_link' WHERE EXISTS (SELECT 1 FROM app.student_tutor_link WHERE tutor_id = p_user)
    UNION ALL SELECT 'exercise_assignment' WHERE EXISTS (SELECT 1 FROM app.exercise_assignment WHERE tutor_id = p_user)
    -- §142: a reward paid out is a payment record.
    UNION ALL SELECT 'exchange_reward' WHERE EXISTS (SELECT 1 FROM app.exchange_reward WHERE partner_id = p_user AND status = 'paid')
  ) x(t);

  -- ── 5. Anonymise the profile ─────────────────────────────────────────────
  --
  -- Done in BOTH paths, not only the retained one. On the hard-delete path the
  -- row is about to cascade away anyway, so this costs nothing; if the caller
  -- then fails to revoke the auth user, what is left behind carries no
  -- identifying data. The failure mode of a two-step erasure is that step two
  -- does not happen, so step one must be safe on its own.

  UPDATE app.user_profiles SET
      first_name          = 'Deleted',
      last_name           = NULL,
      avatar_url          = NULL,
      location            = NULL,
      phone_number        = NULL,
      date_of_birth       = NULL,
      onboarding_location = NULL,
      known_languages     = '[]'::jsonb,
      deleted_at          = coalesce(deleted_at, now())
    WHERE id = p_user;
  GET DIAGNOSTICS v_n = ROW_COUNT;

  RETURN jsonb_build_object(
    'user_id',         p_user,
    'profile_found',   v_n > 0,
    'storage',         v_storage,
    'retained',        v_retained,
    'can_hard_delete', jsonb_array_length(v_retained) = 0
  );
END;
$function$;
