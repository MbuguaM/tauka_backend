-- ============================================================================
-- §142 (2026-10-20)  Exchange partners (features addendum, Feature A)
-- ============================================================================
--
-- New `exchange` domain (features-addendum-implementation-plan.md §2a: X-02,
-- X-03, X-04). tauka-python writes every table here except the two a person
-- writes for themselves through tauka-python too (availability, ratings): no
-- client role writes anything; reads are RLS-limited and most go through the API.
--
-- Behind the 'exchange_partners' flag (seeded 'coming'), plus
-- 'exchange_practise_now' (A11) and 'exchange_message_flags' (X-11, and D-12:
-- reading message content needs its own legal item, so it ships OFF).
--
-- X-03  A partner is an `exchange_partner` row. Partner-only accounts get
--       user_type 'partner'; a tutor can also be a partner. A learner of
--       Amharic can't be an Amharic partner (checked at activation).
-- X-06  Nothing is recorded: no recording column exists on purpose (L-004).
-- X-07  Identity check by the safeguarding lead by hand: result + date only.
-- X-08  Rewards: eligibility computed here, payment by hand (status 'due').
-- X-13  Task cards extend app.peer_task (side, English level, corrections…).
-- §3.1  conversations.type gains 'exchange' (the app already sends it).
-- CONFLICT-047: the two dead exchange tables (0 rows) and their trigger
--       function are dropped; delete_user_account loses the one line naming them.

-- ── roles and the CHECKs the addendum needs ─────────────────────────────────

ALTER TABLE app.user_profiles DROP CONSTRAINT IF EXISTS user_only_tutor_or_student;
ALTER TABLE app.user_profiles ADD CONSTRAINT user_only_tutor_or_student
  CHECK (user_type = ANY (ARRAY['tutor'::text, 'student'::text, 'partner'::text]));

ALTER TABLE app.conversations DROP CONSTRAINT IF EXISTS conversations_type_check;
ALTER TABLE app.conversations ADD CONSTRAINT conversations_type_check
  CHECK (type = ANY (ARRAY['student_to_student'::text, 'teacher_to_student'::text, 'class_group'::text,
                           'admin_broadcast'::text, 'exchange'::text]));

ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_about_kind_check;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_about_kind_check
  CHECK (about_kind = ANY (ARRAY['tutor'::text, 'learner'::text, 'partner'::text, 'someone_else'::text, 'myself'::text]));
ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_about;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_about
  CHECK (about_kind = ANY (ARRAY['tutor'::text, 'learner'::text, 'partner'::text]) OR about_user_id IS NULL);
ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_session_type_check;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_session_type_check CHECK (session_type IS NULL OR
  session_type IN ('cohort_session','makeup_session','clinic','one_to_one_session','peer_session','exchange_session'));

ALTER TABLE app.consent_record DROP CONSTRAINT IF EXISTS consent_record_purpose_check;
ALTER TABLE app.consent_record ADD CONSTRAINT consent_record_purpose_check CHECK (purpose = ANY (ARRAY[
  'session_recording'::text, 'pronunciation_profile'::text, 'model_training'::text, 'tutor_voice_review'::text,
  'employer_reporting'::text,
  -- §142: the addendum's separate consents (A12) and the partner's agreements.
  'exchange_conduct'::text, 'message_filtering'::text, 'identity_check'::text, 'english_voice_sample'::text]));

-- Gender for exchanges: the same none / preference / requirement as pods (A8.2, A10).
ALTER TABLE app.gender_preference ADD COLUMN IF NOT EXISTS same_gender_exchange text NOT NULL DEFAULT 'none';
ALTER TABLE app.gender_preference DROP CONSTRAINT IF EXISTS gender_preference_same_gender_exchange_check;
ALTER TABLE app.gender_preference ADD CONSTRAINT gender_preference_same_gender_exchange_check
  CHECK (same_gender_exchange = ANY (ARRAY['none'::text, 'preference'::text, 'requirement'::text]));
ALTER TABLE app.gender_preference DROP CONSTRAINT IF EXISTS gender_preference_requirement;
ALTER TABLE app.gender_preference ADD CONSTRAINT gender_preference_requirement CHECK (
  (same_gender_tutor <> 'requirement' AND same_gender_pod <> 'requirement' AND same_gender_exchange <> 'requirement')
  OR gender = ANY (ARRAY['woman'::text, 'man'::text, 'nonbinary'::text]));

-- ── task cards (X-13) ───────────────────────────────────────────────────────

ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS side text NOT NULL DEFAULT 'learner';
ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS english_level text;
ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS corrections text;
ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'human';
ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS reviewed_by uuid;
ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;
ALTER TABLE app.peer_task ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1;
ALTER TABLE app.peer_task ALTER COLUMN lesson_number DROP NOT NULL;
ALTER TABLE app.peer_task ADD CONSTRAINT peer_task_side_check CHECK (side IN ('learner','partner'));
ALTER TABLE app.peer_task ADD CONSTRAINT peer_task_source_check CHECK (source IN ('human','ai_draft'));
ALTER TABLE app.peer_task ADD CONSTRAINT peer_task_english_level_check
  CHECK (english_level IS NULL OR english_level IN ('A2','B1','B2','C1'));
ALTER TABLE app.peer_task ADD CONSTRAINT peer_task_corrections_check
  CHECK (corrections IS NULL OR length(corrections) <= 1000);
-- A learner card belongs to a lesson; a partner card to an English level.
ALTER TABLE app.peer_task ADD CONSTRAINT peer_task_side_key CHECK (
  (side = 'learner' AND lesson_number IS NOT NULL) OR (side = 'partner' AND english_level IS NOT NULL));
-- An AI draft is never live until a person reviewed it.
ALTER TABLE app.peer_task ADD CONSTRAINT peer_task_draft_reviewed
  CHECK (source <> 'ai_draft' OR NOT active OR reviewed_by IS NOT NULL);

-- ── the partner ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.exchange_partner (
  user_id            uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  pair               text NOT NULL DEFAULT 'am-en' CHECK (pair IN ('am-en')),
  region             text CHECK (region IS NULL OR length(region) <= 80),
  amharic_variety    text NOT NULL DEFAULT 'standard' CHECK (amharic_variety IN ('standard','regional')),
  variety_note       text CHECK (variety_note IS NULL OR length(variety_note) <= 120),
  english_self       text CHECK (english_self IS NULL OR english_self IN ('A2','B1','B2','C1')),
  -- X-12: set by ops after hearing the 2-minute answer; the matcher reads this one.
  english_level      text CHECK (english_level IS NULL OR english_level IN ('A2','B1','B2','C1')),
  english_sample_path text,
  english_reviewed_by uuid,
  english_reviewed_at timestamptz,
  interests          text[] NOT NULL DEFAULT '{}',
  band               text,
  status             text NOT NULL DEFAULT 'onboarding'
                     CHECK (status IN ('onboarding','active','waitlisted','paused','removed')),
  status_reason      text CHECK (status_reason IS NULL OR length(status_reason) <= 300),
  conduct_agreed_at  timestamptz,
  trained_at         timestamptz,
  activated_at       timestamptz,
  waitlisted_at      timestamptz,
  paused_until       timestamptz,
  -- X-07: the result of a manual check, never the document.
  verified_at        timestamptz,
  verified_by        uuid,
  low_data           boolean NOT NULL DEFAULT true,
  tutor_invited_at   timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT exchange_partner_active_ready CHECK (status <> 'active' OR (conduct_agreed_at IS NOT NULL AND trained_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_exchange_partner_status ON app.exchange_partner (status, band);

-- Weekly slots from the catalogue (A4.3).
CREATE TABLE IF NOT EXISTS app.exchange_availability (
  user_id    uuid NOT NULL REFERENCES app.exchange_partner(user_id) ON DELETE CASCADE,
  slot_id    uuid NOT NULL REFERENCES app.slot_catalogue(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, slot_id)
);

-- ── sessions ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.exchange_session (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  learner_id        uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  partner_id        uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  pair              text NOT NULL DEFAULT 'am-en',
  kind              text NOT NULL DEFAULT 'scheduled' CHECK (kind IN ('scheduled','instant')),
  slot_id           uuid REFERENCES app.slot_catalogue(id) ON DELETE SET NULL,
  scheduled_at      timestamptz NOT NULL,
  length_min        int  NOT NULL DEFAULT 30 CHECK (length_min IN (20, 30)),
  status            text NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested','booked','declined','expired','completed','missed','cancelled')),
  respond_by        timestamptz,
  responded_at      timestamptz,
  cancelled_by      uuid,
  cancelled_at      timestamptz,
  cancel_reason     text CHECK (cancel_reason IS NULL OR length(cancel_reason) <= 300),
  late_cancel       boolean NOT NULL DEFAULT false,
  lesson_number     int,
  learner_task_id   uuid REFERENCES app.peer_task(id) ON DELETE SET NULL,
  partner_task_id   uuid REFERENCES app.peer_task(id) ON DELETE SET NULL,
  room_url          text,
  conversation_id   uuid REFERENCES app.conversations(id) ON DELETE SET NULL,
  learner_joined_at timestamptz,
  partner_joined_at timestamptz,
  connect_failures  int NOT NULL DEFAULT 0,
  actual_duration_s int CHECK (actual_duration_s IS NULL OR actual_duration_s >= 0),
  relay_used        boolean,
  video_used        boolean NOT NULL DEFAULT false,
  missed_by         uuid,
  ended_at          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT exchange_session_two_people CHECK (learner_id <> partner_id)
);
CREATE INDEX IF NOT EXISTS idx_exchange_session_learner ON app.exchange_session (learner_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_exchange_session_partner ON app.exchange_session (partner_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_exchange_session_status ON app.exchange_session (status, scheduled_at);
-- One live booking per partner per start time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_exchange_partner_time ON app.exchange_session (partner_id, scheduled_at)
  WHERE status IN ('requested','booked');

CREATE TABLE IF NOT EXISTS app.exchange_rating (
  session_id    uuid NOT NULL REFERENCES app.exchange_session(id) ON DELETE CASCADE,
  rater_id      uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  helpfulness   int  NOT NULL CHECK (helpfulness BETWEEN 1 AND 5),
  again         boolean NOT NULL,
  other_came    boolean NOT NULL DEFAULT true,
  flag_moment_s int CHECK (flag_moment_s IS NULL OR flag_moment_s BETWEEN 0 AND 3600),
  note          text CHECK (note IS NULL OR length(note) <= 500),
  phrase        text CHECK (phrase IS NULL OR length(phrase) <= 200),
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, rater_id)
);

-- The partner's English phrase list (A6, "after the call"). Learners' phrases go to their deck.
CREATE TABLE IF NOT EXISTS app.exchange_phrase (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  session_id uuid REFERENCES app.exchange_session(id) ON DELETE SET NULL,
  phrase     text NOT NULL CHECK (length(phrase) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_phrase_user ON app.exchange_phrase (user_id, created_at);

-- Blocks between any two people (partners have no app.student row, so
-- app.peer_block can't hold them). A block here also stops peer pairing.
CREATE TABLE IF NOT EXISTS app.exchange_block (
  blocker_id uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  blocked_id uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (blocker_id, blocked_id),
  CONSTRAINT exchange_block_not_self CHECK (blocker_id <> blocked_id)
);

-- After four sessions together a learner may favourite a partner (A8.5).
CREATE TABLE IF NOT EXISTS app.exchange_favourite (
  learner_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  partner_id uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (learner_id, partner_id)
);

-- "Practise now" (A11), the peer_queue pattern.
CREATE TABLE IF NOT EXISTS app.exchange_queue (
  user_id    uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  side       text NOT NULL CHECK (side IN ('learner','partner')),
  band       text,
  joined_at  timestamptz NOT NULL DEFAULT now(),
  session_id uuid REFERENCES app.exchange_session(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS app.exchange_reward (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- RESTRICT: a paid reward is a payment record (delete_user_account retains it).
  partner_id       uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE RESTRICT,
  sessions_counted int  NOT NULL CHECK (sessions_counted > 0),
  session_ids      uuid[] NOT NULL,
  amount_minor     bigint CHECK (amount_minor IS NULL OR amount_minor >= 0),
  currency         text NOT NULL DEFAULT 'ETB',
  channel          text CHECK (channel IS NULL OR channel IN ('telebirr','mpesa_et')),
  status           text NOT NULL DEFAULT 'due' CHECK (status IN ('due','held','paid','cancelled')),
  held_reason      text CHECK (held_reason IS NULL OR length(held_reason) <= 200),
  reference        text CHECK (reference IS NULL OR length(reference) <= 120),
  paid_at          timestamptz,
  paid_by          uuid,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT exchange_reward_paid CHECK ((status = 'paid') = (paid_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_exchange_reward_partner ON app.exchange_reward (partner_id, created_at);

-- X-11: a keyword match in a pair's chat, for the safeguarding lead. Never a
-- pause on its own. Written only while 'exchange_message_flags' is ready.
CREATE TABLE IF NOT EXISTS app.exchange_message_flag (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid REFERENCES app.conversations(id) ON DELETE CASCADE,
  message_id      uuid,
  sender_id       uuid,
  kind            text NOT NULL CHECK (kind IN ('money','romance')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  reviewed_at     timestamptz,
  reviewed_by     uuid,
  outcome         text CHECK (outcome IS NULL OR outcome IN ('no_action','warning_issued','paused','removed'))
);

-- ── RLS: own rows only; everything else through tauka-python ────────────────

ALTER TABLE app.exchange_partner ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_availability ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_rating ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_phrase ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_favourite ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_reward ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.exchange_message_flag ENABLE ROW LEVEL SECURITY;

CREATE POLICY exchange_partner_own ON app.exchange_partner FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_admin());
CREATE POLICY exchange_availability_own ON app.exchange_availability FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR app.is_admin());
CREATE POLICY exchange_session_own ON app.exchange_session FOR SELECT TO authenticated
  USING (learner_id = auth.uid() OR partner_id = auth.uid() OR app.is_admin());
-- Ratings you wrote, never the ones about you (A11 RLS).
CREATE POLICY exchange_rating_own ON app.exchange_rating FOR SELECT TO authenticated
  USING (rater_id = auth.uid() OR app.is_admin());
CREATE POLICY exchange_phrase_own ON app.exchange_phrase FOR SELECT TO authenticated
  USING (user_id = auth.uid());
CREATE POLICY exchange_block_own ON app.exchange_block FOR SELECT TO authenticated
  USING (blocker_id = auth.uid() OR app.is_admin());
CREATE POLICY exchange_favourite_own ON app.exchange_favourite FOR SELECT TO authenticated
  USING (learner_id = auth.uid());
CREATE POLICY exchange_reward_own ON app.exchange_reward FOR SELECT TO authenticated
  USING (partner_id = auth.uid() OR app.is_admin());
-- exchange_queue and exchange_message_flag: no client policy at all.

REVOKE ALL ON app.exchange_partner, app.exchange_availability, app.exchange_session, app.exchange_rating,
              app.exchange_phrase, app.exchange_block, app.exchange_favourite, app.exchange_queue,
              app.exchange_reward, app.exchange_message_flag FROM anon, authenticated;
GRANT SELECT ON app.exchange_partner, app.exchange_availability, app.exchange_session, app.exchange_rating,
                app.exchange_phrase, app.exchange_block, app.exchange_favourite, app.exchange_reward TO authenticated;
GRANT ALL ON app.exchange_partner, app.exchange_availability, app.exchange_session, app.exchange_rating,
             app.exchange_phrase, app.exchange_block, app.exchange_favourite, app.exchange_queue,
             app.exchange_reward, app.exchange_message_flag TO service_role;

COMMENT ON TABLE app.exchange_partner IS 'Exchange partners (features addendum A, §142): native Amharic speakers practising English. The row is the role (X-03). tauka-python writes.';
COMMENT ON TABLE app.exchange_session IS 'One learner + one partner, 20 or 30 min, half Amharic half English, audio-only Daily room, never recorded (§142). tauka-python writes.';
COMMENT ON TABLE app.exchange_reward IS 'Data-cost rewards for partners: every 4 completed sessions, max 2 a month, paid by hand (X-08). tauka-python writes.';
COMMENT ON TABLE app.exchange_message_flag IS 'Keyword matches (money, romance) in exchange chats for safeguarding. Never pauses anyone (X-11). Off unless exchange_message_flags is ready.';

-- ── flags and config ────────────────────────────────────────────────────────

INSERT INTO app.feature_flag (key, status, description) VALUES
  ('exchange_partners',      'coming', 'Features addendum A: exchange partners (§142).'),
  ('exchange_practise_now',  'coming', 'Features addendum A11: practise now for exchanges.'),
  ('exchange_message_flags', 'coming', 'X-11: money/romance keyword flags to safeguarding. Needs its legal item and D-12 revisited.')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app.config (key, value) VALUES
  ('exchange.allowance', '{"free_tier":{"month":2},"learner_tier":{"month":4},"tutor_tier":{"week":3},"intensive_tier":{"week":5}}'),
  ('exchange.partner_ratio', '1.5'),
  -- The pilot's 20-30 hand-recruited partners are never waitlisted for want of learners.
  ('exchange.partner_floor', '30'),
  ('exchange.reward_every', '4'),
  ('exchange.reward_cap_month', '2'),
  ('exchange.reward_amount_minor', '0'),
  ('exchange.reliability_misses', '2'),
  ('exchange.instant_band', 'ea_gulf'),
  ('exchange.tutor_invite', '{"sessions":20,"rating":4.5}')
ON CONFLICT (key) DO NOTHING;

-- ── a starter English bank for the partner side (A6: "written once") ────────

-- An information gap's B half is what the LEARNER sees during the English half.
INSERT INTO app.peer_task (side, english_level, kind, title, prompt_a, prompt_b, source, active) VALUES
  ('partner','A2','question_set','Your day','Tell your partner about yesterday: three things you did, in order. Then ask them about theirs.',NULL,'human',true),
  ('partner','A2','role_play','At a café','You are ordering for two people. Ask about the menu, order, and ask for the bill.',NULL,'human',true),
  ('partner','A2','question_set','Family','Describe one person in your family: who they are, what they do, what they like.',NULL,'human',true),
  ('partner','B1','information_gap','Planning a trip','Plan a weekend trip together. You know the train times (Friday 18:00, Saturday 08:00). Ask what your partner knows about hotels, then agree a plan.','You know two hotels: one cheap and far from the station, one dear and close. Ask your partner about the trains, then agree a plan.','human',true),
  ('partner','B1','role_play','Returning something','You bought shoes that are too small. Explain the problem at the shop and ask for a different size or your money back.',NULL,'human',true),
  ('partner','B1','question_set','Work and study','What do you do every week for work or study? What is hard about it, and what do you enjoy?',NULL,'human',true),
  ('partner','B2','question_set','A change in your city','Describe something that has changed in your city in the last five years. Is it better or worse, and why?',NULL,'human',true),
  ('partner','B2','role_play','A job interview','Your partner interviews you for a job you want. Answer, then ask them two questions about the job.',NULL,'human',true),
  ('partner','B2','question_set','Advice','Ask your partner for advice on a real decision you need to make. Explain the options, then discuss them.',NULL,'human',true),
  ('partner','C1','question_set','Two sides','Pick a topic in the news. Give the strongest argument for one side, then for the other, and say which you find stronger.',NULL,'human',true),
  ('partner','C1','role_play','A difficult conversation','Tell a colleague, politely, that their work is late and is holding you up. Agree a solution.',NULL,'human',true),
  ('partner','C1','question_set','Explaining your field','Explain something from your work or study to someone outside it, then answer their questions.',NULL,'human',true);

-- ── CONFLICT-047: the dead exchange tables ──────────────────────────────────

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM app.language_exchange_match) OR EXISTS (SELECT 1 FROM app.langexchange) THEN
    RAISE EXCEPTION 'language_exchange_match or langexchange has rows: stop and look before dropping';
  END IF;
END $$;
DROP TABLE IF EXISTS app.language_exchange_match;
DROP TABLE IF EXISTS app.langexchange;
DROP FUNCTION IF EXISTS public.update_rematch_available_at();

-- delete_user_account: the CONFLICT-047 line removed (its table is gone) and the
-- exchange rows added. Everything else is the function as it was.
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
  DELETE FROM app.exchange_phrase          WHERE user_id = p_user;
  DELETE FROM app.exchange_queue           WHERE user_id = p_user;
  DELETE FROM app.exchange_favourite       WHERE learner_id = p_user OR partner_id = p_user;
  UPDATE app.exchange_rating SET note = NULL, phrase = NULL WHERE rater_id = p_user;
  UPDATE app.exchange_partner SET english_sample_path = NULL, region = NULL, variety_note = NULL,
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
