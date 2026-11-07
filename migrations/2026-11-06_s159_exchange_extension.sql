-- §159 Exchange extension (2026-11-06, developer; exchange-extension-plan.md XE-1..XE-25).
-- Owner: tauka-python. Domain: exchange (plus small extensions in cohort_matching/enrolment
-- tables tauka-python already owns: concern_report, enrolment_state, consent_record, peer_task).
--
-- Additive. Nothing built on 2026-11-06 changes shape: 1:1 sessions, open hours, chat now,
-- the weekly cap. Every new behaviour is behind a flag seeded 'coming'.
-- Conventions: singular names, TEXT + CHECK, uuid ids, money as integer minor units + currency.

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA extensions;   -- the group-seat overlap rule

-- ── flags ───────────────────────────────────────────────────────────────────
INSERT INTO app.feature_flag (key, status) VALUES
  ('exchange_cases', 'coming'), ('exchange_groups', 'coming'), ('exchange_voice_review', 'coming'),
  ('exchange_recording', 'coming'), ('exchange_guests', 'coming'), ('exchange_phase0', 'coming'),
  ('exchange_plus', 'coming'), ('paid_partners', 'coming'), ('exchange_contact_cases', 'coming')
ON CONFLICT (key) DO NOTHING;

-- ── XE-4 leniency, XE-5 ratings, XE-22 tasters ─────────────────────────────
ALTER TABLE app.exchange_session
  ADD COLUMN IF NOT EXISTS excused_reason text
    CHECK (excused_reason IS NULL OR excused_reason IN ('force_majeure','emergency','first_timezone')),
  ADD COLUMN IF NOT EXISTS excused_at timestamptz;
ALTER TABLE app.exchange_session DROP CONSTRAINT IF EXISTS exchange_session_length_min_check;
ALTER TABLE app.exchange_session ADD CONSTRAINT exchange_session_length_min_check
  CHECK (length_min = ANY (ARRAY[15, 20, 30]));          -- 15 = a Foundation taster (XE-22)

ALTER TABLE app.exchange_rating
  ADD COLUMN IF NOT EXISTS target_lang_kept smallint CHECK (target_lang_kept IS NULL OR target_lang_kept BETWEEN 1 AND 5),
  ADD COLUMN IF NOT EXISTS corrected_me smallint CHECK (corrected_me IS NULL OR corrected_me BETWEEN 1 AND 5);

CREATE TABLE IF NOT EXISTS app.exchange_outage (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  country_code text NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
  starts_at    timestamptz NOT NULL,
  ends_at      timestamptz NOT NULL,
  note         text CHECK (note IS NULL OR length(note) <= 300),
  declared_by  uuid,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at)
);

-- ── XE-7 household links ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_household_link (
  a          uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  b          uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (a, b),
  CHECK (a < b)
);

-- ── XE-10 presence from the call provider ──────────────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_provider_event (
  provider     text NOT NULL CHECK (provider IN ('livekit','daily')),
  event_id     text NOT NULL,
  event_type   text NOT NULL,
  room         text,
  user_id      uuid,
  occurred_at  timestamptz NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  processed_at timestamptz,
  PRIMARY KEY (provider, event_id)          -- a repeated webhook is ignored
);
CREATE TABLE IF NOT EXISTS app.exchange_presence (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_kind text NOT NULL CHECK (session_kind IN ('pair','group')),
  session_id   uuid NOT NULL,
  user_id      uuid NOT NULL,
  provider     text NOT NULL CHECK (provider IN ('livekit','daily')),
  joined_at    timestamptz NOT NULL,
  left_at      timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_presence ON app.exchange_presence (session_id, user_id);

-- ── XE-11 cases on concern_report, sanctions, appeals; XE-12 disputes ─────
-- Partners report too: the reporter is any profile, not only a learner.
ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_reporter_id_fkey;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_reporter_id_fkey
  FOREIGN KEY (reporter_id) REFERENCES app.user_profiles(id) ON DELETE CASCADE;
ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_category_check;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_category_check CHECK (category = ANY (ARRAY[
  'harassment','contact_details','inappropriate_content','wellbeing','safety','other',
  'sexual_conduct','hate_speech','threat','fraud','underage','solicitation','account_sharing']));
ALTER TABLE app.concern_report
  ADD COLUMN IF NOT EXISTS severity text CHECK (severity IS NULL OR severity IN ('S1','S2','S3','S4')),
  ADD COLUMN IF NOT EXISTS first_action_due timestamptz,
  ADD COLUMN IF NOT EXISTS decision_due timestamptz,
  ADD COLUMN IF NOT EXISTS interim_suspended_at timestamptz,
  ADD COLUMN IF NOT EXISTS legal_hold boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS decision_note text CHECK (decision_note IS NULL OR length(decision_note) <= 2000),
  ADD COLUMN IF NOT EXISTS automatic boolean NOT NULL DEFAULT false;   -- opened by a rule, not a person
-- A case a rule opened (three contact-detail attempts) has no reporter.
ALTER TABLE app.concern_report ALTER COLUMN reporter_id DROP NOT NULL;
ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_reporter_or_rule;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_reporter_or_rule CHECK (reporter_id IS NOT NULL OR automatic);

CREATE TABLE IF NOT EXISTS app.exchange_sanction (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  kind              text NOT NULL CHECK (kind IN ('nudge','warning','strike','suspend_7','suspend_30','suspend_90','ban','role_removal','interim')),
  concern_id        uuid REFERENCES app.concern_report(id) ON DELETE SET NULL,
  reason            text CHECK (reason IS NULL OR length(reason) <= 1000),
  starts_at         timestamptz NOT NULL DEFAULT now(),
  ends_at           timestamptz,
  strike_expires_at timestamptz,
  lifted_at         timestamptz,
  decided_by        uuid,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_sanction_user ON app.exchange_sanction (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS app.exchange_appeal (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sanction_id uuid NOT NULL REFERENCES app.exchange_sanction(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  statement   text NOT NULL CHECK (length(statement) BETWEEN 1 AND 2000),
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','upheld','reduced','overturned')),
  decided_by  uuid,
  decided_at  timestamptz,
  due_at      timestamptz NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sanction_id)                      -- one appeal per decision
);

CREATE TABLE IF NOT EXISTS app.exchange_dispute (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('attendance','credits','rating','quality','placement','pay')),
  session_id  uuid,
  level       smallint NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 3),
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','escalated')),
  statement   text NOT NULL CHECK (length(statement) BETWEEN 1 AND 2000),
  evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
  decision    text CHECK (decision IS NULL OR length(decision) <= 2000),
  decided_by  uuid,
  decided_at  timestamptz,
  due_at      timestamptz NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- XE-20: contact-detail attempts are counted (3 in 30 days opens a case, behind exchange_contact_cases).
ALTER TABLE app.exchange_message_flag DROP CONSTRAINT IF EXISTS exchange_message_flag_kind_check;
ALTER TABLE app.exchange_message_flag ADD CONSTRAINT exchange_message_flag_kind_check
  CHECK (kind = ANY (ARRAY['money','romance','contact_attempt','spelled_number']));

-- ── XE-13 language bands ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_language_level (
  user_id     uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  language    text NOT NULL CHECK (language IN ('am','en')),
  band        text NOT NULL CHECK (band IN ('foundation','conversation','advanced')),
  cefr        text CHECK (cefr IS NULL OR cefr IN ('A1','A2','B1','B2','C1','C2')),
  source      text NOT NULL CHECK (source IN ('placement','lesson','speaking_sample','tutor','self')),
  assessed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, language)
);

-- ── XE-15 minute budget (measuring only) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_minute_pool (
  period     date NOT NULL,                 -- first day of the month (UTC)
  pool       text NOT NULL CHECK (pool IN ('required','barter','taster')),
  cap_pm     bigint NOT NULL CHECK (cap_pm > 0),
  used_pm    bigint NOT NULL DEFAULT 0,
  alerted_80 timestamptz,
  alerted_95 timestamptz,
  PRIMARY KEY (period, pool)
);

-- ── XE-16 groups, XE-17 private details, matching preferences ─────────────
CREATE TABLE IF NOT EXISTS app.exchange_series (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL CHECK (kind IN ('foundation_support','peer_guide','fluent_english')),
  host_id     uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  anchor_tz   text NOT NULL CHECK (length(anchor_tz) <= 64),
  day_of_week smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
  local_time  time NOT NULL,
  length_min  smallint NOT NULL CHECK (length_min IN (30, 45)),
  band        text NOT NULL CHECK (band IN ('foundation','conversation','advanced')),
  weeks       smallint NOT NULL CHECK (weeks BETWEEN 4 AND 8),
  starts_on   date NOT NULL,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','ended')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.exchange_group (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL CHECK (kind IN ('foundation_support','peer_guide','fluent_english')),
  status      text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','live','completed','cancelled')),
  starts_at   timestamptz NOT NULL,
  length_min  smallint NOT NULL CHECK (length_min IN (30, 45)),
  band        text NOT NULL CHECK (band IN ('foundation','conversation','advanced')),
  host_id     uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  capacity    smallint NOT NULL DEFAULT 3 CHECK (capacity BETWEEN 2 AND 3),   -- members besides the host
  series_id   uuid REFERENCES app.exchange_series(id) ON DELETE SET NULL,
  room_url    text,
  cancel_reason text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_group_start ON app.exchange_group (starts_at) WHERE status IN ('scheduled','live');
CREATE TABLE IF NOT EXISTS app.exchange_group_member (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id   uuid NOT NULL REFERENCES app.exchange_group(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('host','member')),
  status     text NOT NULL DEFAULT 'booked' CHECK (status IN ('booked','cancelled','attended','partial','no_show','excused')),
  slot       tstzrange NOT NULL,            -- the group's time, for the overlap rule
  late_cancel boolean NOT NULL DEFAULT false,
  joined_at  timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (group_id, user_id),
  EXCLUDE USING gist (user_id extensions.gist_uuid_ops WITH =, slot WITH &&) WHERE (status = 'booked')   -- never two seats at once
);

-- Peer guides and alumni hosts are learners, so their hosting permission can't live on
-- exchange_partner (that would put them in the 1:1 partner pool). Ops grant it (doc 01 §12).
CREATE TABLE IF NOT EXISTS app.exchange_host_grant (
  user_id    uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('peer_guide','alumni_host')),
  granted_by uuid,
  granted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, role)
);

CREATE TABLE IF NOT EXISTS app.exchange_sensitive (     -- service role only (XE-17, Kenya DPA)
  user_id       uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  date_of_birth date NOT NULL CHECK (date_of_birth > DATE '1900-01-01'),
  dob_method    text NOT NULL DEFAULT 'declared' CHECK (dob_method IN ('declared','id_check')),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.exchange_match_pref (
  user_id        uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  pod_any_mix    boolean NOT NULL DEFAULT false,  -- a woman may be the only woman in a group
  widen_age_band boolean NOT NULL DEFAULT false,
  record_own_side boolean,                         -- XE-21: null = the default for their role
  recap_opt_in   boolean NOT NULL DEFAULT false,
  wifi_only_upload boolean NOT NULL DEFAULT true,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- ── XE-21 recordings (each person's own microphone) ────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_recording (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_kind text NOT NULL CHECK (session_kind IN ('pair','group')),
  session_id   uuid NOT NULL,
  user_id      uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  storage_path text NOT NULL,
  duration_s   int CHECK (duration_s IS NULL OR duration_s BETWEEN 0 AND 14400),
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','uploaded','deleted')),
  expires_at   timestamptz NOT NULL,
  legal_hold   boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_kind, session_id, user_id)
);
CREATE TABLE IF NOT EXISTS app.exchange_recording_access (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id uuid NOT NULL REFERENCES app.exchange_recording(id) ON DELETE CASCADE,
  actor        uuid NOT NULL,
  concern_id   uuid REFERENCES app.concern_report(id) ON DELETE SET NULL,
  at           timestamptz NOT NULL DEFAULT now()
);
INSERT INTO storage.buckets (id, name, public) VALUES ('exchange-recordings', 'exchange-recordings', false)
ON CONFLICT (id) DO NOTHING;

-- ── XE-19 voice-note review ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_voice_prompt (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  band       text NOT NULL CHECK (band IN ('foundation','conversation','advanced')),
  prompt     text NOT NULL CHECK (length(prompt) BETWEEN 3 AND 500),
  active     boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.exchange_voice_note (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  learner_id        uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  prompt_id         uuid NOT NULL REFERENCES app.exchange_voice_prompt(id),
  band              text NOT NULL CHECK (band IN ('foundation','conversation','advanced')),
  audio_path        text NOT NULL,
  status            text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','reviewed','accepted','rejected','expired')),
  reviewer_id       uuid REFERENCES app.user_profiles(id) ON DELETE SET NULL,
  claimed_at        timestamptz,
  due_at            timestamptz,
  review_audio_path text,
  review_text       jsonb,                   -- {corrected, fixes[], alternative}
  rejection_reason  text CHECK (rejection_reason IS NULL OR length(rejection_reason) <= 300),
  reviewed_at       timestamptz,
  decided_at        timestamptz,
  sampled           boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_voice_queue ON app.exchange_voice_note (status, band, created_at);

-- ── XE-24 paid partners ────────────────────────────────────────────────────
ALTER TABLE app.exchange_partner
  ADD COLUMN IF NOT EXISTS roles text[] NOT NULL DEFAULT '{barter}'
    CHECK (roles <@ ARRAY['barter','pap','fep','peer_guide','alumni_host','reviewer']::text[]),
  ADD COLUMN IF NOT EXISTS plus_until timestamptz;           -- XE-25 Exchange+
CREATE TABLE IF NOT EXISTS app.exchange_rate (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  work_type      text NOT NULL CHECK (work_type IN ('live_30','live_45','voice_review','recording_min')),
  band           text NOT NULL CHECK (band IN ('foundation','conversation','advanced')),
  amount_minor   int NOT NULL CHECK (amount_minor >= 0),
  currency       text NOT NULL DEFAULT 'KES' CHECK (currency ~ '^[A-Z]{3}$'),
  effective_from timestamptz NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.exchange_payout_batch (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  status       text NOT NULL DEFAULT 'building' CHECK (status IN ('building','approved','sent','complete')),
  total_minor  bigint NOT NULL DEFAULT 0,
  currency     text NOT NULL DEFAULT 'KES',
  approved_by  uuid[] NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.exchange_payout (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id       uuid NOT NULL REFERENCES app.exchange_payout_batch(id) ON DELETE CASCADE,
  partner_id     uuid NOT NULL REFERENCES app.user_profiles(id),
  amount_minor   bigint NOT NULL CHECK (amount_minor > 0),
  currency       text NOT NULL DEFAULT 'KES',
  method         text NOT NULL CHECK (method IN ('mpesa_b2c','airtime','org')),
  originator_ref text NOT NULL UNIQUE,
  provider_ref   text,
  status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','paid','failed','reversed')),
  attempts       smallint NOT NULL DEFAULT 0,
  last_error     text,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.exchange_earning (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  partner_id   uuid NOT NULL REFERENCES app.user_profiles(id),
  kind         text NOT NULL CHECK (kind IN ('live','live_partial','no_show_protection','infra_failure','voice_review','recording','bonus','reversal')),
  status       text NOT NULL DEFAULT 'held' CHECK (status IN ('held','releasable','batched','paid','reversed')),
  amount_minor int NOT NULL,                 -- negative only for a reversal
  currency     text NOT NULL DEFAULT 'KES' CHECK (currency ~ '^[A-Z]{3}$'),
  session_kind text CHECK (session_kind IS NULL OR session_kind IN ('pair','group','voice_note')),
  ref_id       uuid,
  rate_id      uuid REFERENCES app.exchange_rate(id),
  hold_until   timestamptz NOT NULL,
  concern_id   uuid REFERENCES app.concern_report(id) ON DELETE SET NULL,
  payout_id    uuid REFERENCES app.exchange_payout(id) ON DELETE SET NULL,
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'reversal') = (amount_minor < 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exchange_earning_once ON app.exchange_earning (partner_id, kind, ref_id) WHERE kind <> 'reversal';
CREATE TABLE IF NOT EXISTS app.exchange_payout_destination (
  partner_id      uuid PRIMARY KEY REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  method          text NOT NULL CHECK (method IN ('mpesa_b2c','airtime','org')),
  msisdn          text CHECK (msisdn IS NULL OR msisdn ~ '^254[17][0-9]{8}$'),
  registered_name text CHECK (registered_name IS NULL OR length(registered_name) <= 120),
  verified_at     timestamptz,
  cooldown_until  timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ── XE-25 Exchange+ purchases (payment kind 'xp') ─────────────────────────
CREATE TABLE IF NOT EXISTS app.exchange_plus_purchase (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES app.user_profiles(id) ON DELETE CASCADE,
  days              smallint NOT NULL DEFAULT 30 CHECK (days BETWEEN 1 AND 31),
  amount_minor      int NOT NULL CHECK (amount_minor > 0),
  currency          text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  email             text NOT NULL,
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','failed','refunded')),
  payment_provider  text,
  payment_reference text,
  paid_at           timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- ── XE-23 guests, XE-21 consent purpose ────────────────────────────────────
ALTER TABLE app.enrolment_state DROP CONSTRAINT IF EXISTS enrolment_state_state_check;
ALTER TABLE app.enrolment_state ADD CONSTRAINT enrolment_state_state_check CHECK (state = ANY (ARRAY[
  'waitlisted','phase0','paid','seat_confirmed','matched','in_pod','paused','wait_pool','cancelled','exchange_guest']));
ALTER TABLE app.consent_record DROP CONSTRAINT IF EXISTS consent_record_purpose_check;
ALTER TABLE app.consent_record ADD CONSTRAINT consent_record_purpose_check CHECK (purpose = ANY (ARRAY[
  'session_recording','pronunciation_profile','model_training','tutor_voice_review','employer_reporting',
  'exchange_conduct','message_filtering','identity_check','english_voice_sample','sponsorship_listing',
  'exchange_recording','exchange_recap']));

-- ── XE-22 Foundation cards for Phase 0 and guests (lesson 0) ──────────────
INSERT INTO app.peer_task (side, lesson_number, kind, title, prompt_a, prompt_b, active, source, corrections)
SELECT 'learner', 0, 'role_play', v.title, v.a, v.b, true, 'human', v.c
  FROM (VALUES
    ('Saying hello', 'Greet your partner and ask how they are. Try: selam, indemin neh / nesh?',
     'Answer the greeting, then ask back. Speak slowly and repeat new words.', 'Greeting forms for a man and a woman'),
    ('My name is', 'Say your name and where you live. Ask your partner the same.',
     'Say your name and city slowly. Help them say "sime ... new".', 'The "new" ending'),
    ('Numbers one to ten', 'Count from one to ten with your partner, then say your age or a phone-free number like a bus line.',
     'Count slowly and have them repeat. Correct one number at a time.', 'and 7 (sebat) vs 8 (simint)')
  ) AS v(title, a, b, c)
 WHERE NOT EXISTS (SELECT 1 FROM app.peer_task WHERE side = 'learner' AND lesson_number = 0);
INSERT INTO app.exchange_voice_prompt (band, prompt)
SELECT v.band, v.prompt FROM (VALUES
  ('foundation', 'Introduce yourself: your name, where you live and one thing you like.'),
  ('foundation', 'Count from one to ten, then say today''s day of the week.'),
  ('conversation', 'Describe your morning, from waking up to leaving home.'),
  ('advanced', 'Retell a news story you heard this week in two minutes or less.')
) AS v(band, prompt)
WHERE NOT EXISTS (SELECT 1 FROM app.exchange_voice_prompt);

-- ── row security: own rows to read; tauka-python writes everything ────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['exchange_host_grant','exchange_outage','exchange_household_link','exchange_provider_event','exchange_presence',
    'exchange_sanction','exchange_appeal','exchange_dispute','exchange_language_level','exchange_minute_pool',
    'exchange_series','exchange_group','exchange_group_member','exchange_sensitive','exchange_match_pref',
    'exchange_recording','exchange_recording_access','exchange_voice_prompt','exchange_voice_note','exchange_rate',
    'exchange_payout_batch','exchange_payout','exchange_earning','exchange_payout_destination','exchange_plus_purchase']
  LOOP
    EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON app.%I FROM anon, authenticated', t);
    EXECUTE format('GRANT ALL ON app.%I TO service_role', t);
  END LOOP;
END $$;
-- Own rows, read only. exchange_sensitive, provider events, presence, recordings, access log,
-- payout batches and the minute pool have NO client policy (service role only).
DROP POLICY IF EXISTS exchange_sanction_own ON app.exchange_sanction;
CREATE POLICY exchange_sanction_own ON app.exchange_sanction FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS exchange_appeal_own ON app.exchange_appeal;
CREATE POLICY exchange_appeal_own ON app.exchange_appeal FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS exchange_dispute_own ON app.exchange_dispute;
CREATE POLICY exchange_dispute_own ON app.exchange_dispute FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS exchange_language_level_own ON app.exchange_language_level;
CREATE POLICY exchange_language_level_own ON app.exchange_language_level FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS exchange_match_pref_own ON app.exchange_match_pref;
CREATE POLICY exchange_match_pref_own ON app.exchange_match_pref FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS exchange_group_member_own ON app.exchange_group_member;
CREATE POLICY exchange_group_member_own ON app.exchange_group_member FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS exchange_voice_note_own ON app.exchange_voice_note;
CREATE POLICY exchange_voice_note_own ON app.exchange_voice_note FOR SELECT TO authenticated
  USING (learner_id = auth.uid() OR reviewer_id = auth.uid());
DROP POLICY IF EXISTS exchange_earning_own ON app.exchange_earning;
CREATE POLICY exchange_earning_own ON app.exchange_earning FOR SELECT TO authenticated USING (partner_id = auth.uid());
DROP POLICY IF EXISTS exchange_payout_own ON app.exchange_payout;
CREATE POLICY exchange_payout_own ON app.exchange_payout FOR SELECT TO authenticated USING (partner_id = auth.uid());
DROP POLICY IF EXISTS exchange_payout_destination_own ON app.exchange_payout_destination;
CREATE POLICY exchange_payout_destination_own ON app.exchange_payout_destination FOR SELECT TO authenticated USING (partner_id = auth.uid());
DROP POLICY IF EXISTS exchange_plus_purchase_own ON app.exchange_plus_purchase;
CREATE POLICY exchange_plus_purchase_own ON app.exchange_plus_purchase FOR SELECT TO authenticated USING (user_id = auth.uid());
GRANT SELECT ON app.exchange_sanction, app.exchange_appeal, app.exchange_dispute, app.exchange_language_level,
  app.exchange_match_pref, app.exchange_group_member, app.exchange_voice_note, app.exchange_earning,
  app.exchange_payout, app.exchange_payout_destination, app.exchange_plus_purchase TO authenticated;

-- ── account deletion covers the new personal data (pg_get_functiondef of the §158 version) ──
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

  -- §159: their own recorded side of exchange calls, and their voice notes and reviews.
  v_storage := v_storage || coalesce(
    (SELECT jsonb_agg(jsonb_build_object('bucket','exchange-recordings','path',storage_path))
       FROM app.exchange_recording WHERE user_id = p_user AND status <> 'deleted' AND NOT legal_hold), '[]'::jsonb);
  v_storage := v_storage || coalesce(
    (SELECT jsonb_agg(jsonb_build_object('bucket','voice-recordings','path',audio_path))
       FROM app.exchange_voice_note WHERE learner_id = p_user), '[]'::jsonb);
  v_storage := v_storage || coalesce(
    (SELECT jsonb_agg(jsonb_build_object('bucket','voice-recordings','path',review_audio_path))
       FROM app.exchange_voice_note WHERE reviewer_id = p_user AND review_audio_path IS NOT NULL), '[]'::jsonb);

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
  -- §159: private details, preferences, bands, household links, recordings not under a hold,
  -- voice notes, the payout line (payouts already made stay: payment records).
  DELETE FROM app.exchange_sensitive       WHERE user_id = p_user;
  DELETE FROM app.exchange_match_pref      WHERE user_id = p_user;
  DELETE FROM app.exchange_language_level  WHERE user_id = p_user;
  DELETE FROM app.exchange_household_link  WHERE a = p_user OR b = p_user;
  DELETE FROM app.exchange_recording       WHERE user_id = p_user AND NOT legal_hold;
  DELETE FROM app.exchange_voice_note      WHERE learner_id = p_user;
  UPDATE app.exchange_voice_note SET review_audio_path = NULL, review_text = NULL WHERE reviewer_id = p_user;
  DELETE FROM app.exchange_payout_destination WHERE partner_id = p_user;
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
    -- §159: partner pay made, and Exchange+ months bought, are payment records.
    UNION ALL SELECT 'exchange_payout' WHERE EXISTS (SELECT 1 FROM app.exchange_payout WHERE partner_id = p_user)
    UNION ALL SELECT 'exchange_plus_purchase' WHERE EXISTS (SELECT 1 FROM app.exchange_plus_purchase WHERE user_id = p_user AND status = 'paid')
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
