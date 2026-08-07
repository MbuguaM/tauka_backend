-- =============================================================================
-- Tauka — Consolidated Migration
-- Sources: spec.md (Segments A–F) + partial_spec.md (Referrals, Account Portal,
--          Tutor Portal)
-- Run once against your Supabase project (postgres / service role user).
-- Tables are created in FK-dependency order so constraints resolve cleanly.
-- All policies use IF NOT EXISTS to make this safe to re-run.
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- 001  Student profiles & subscription tracking   (Segment A)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS student_profiles (
    id                       uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tier                     text NOT NULL DEFAULT 'free',
        -- 'free' | 'learner' | 'tutor' | 'intensive'
    tier_source              text NOT NULL DEFAULT 'self',
        -- 'self' | 'gifted' | 'admin'
    stripe_customer_id       text UNIQUE,
    stripe_subscription_id   text,
    subscription_status      text DEFAULT 'none',
        -- 'none' | 'active' | 'past_due' | 'cancelled' | 'trialing'
    active_gift_id           uuid,           -- FK added after gift_subscriptions is created
    original_tier            text,           -- stored when a gift overrides a self-paid tier
    current_period_end       timestamptz,

    -- Progress fields (milestone detection + tutor portal)
    lessons_completed        int     DEFAULT 0,
    current_streak           int     DEFAULT 0,
    ai_conversations         int     DEFAULT 0,
    cohort_sessions_attended int     DEFAULT 0,
    assessed_level           text,
    last_unit_milestone      int     DEFAULT 0,
    streak_14_notified       boolean DEFAULT false,
    first_ai_convo_notified  boolean DEFAULT false,
    first_cohort_notified    boolean DEFAULT false,
    last_notified_level      text,

    created_at               timestamptz DEFAULT now(),
    updated_at               timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_student_profiles_stripe_customer
    ON student_profiles(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_student_profiles_stripe_subscription
    ON student_profiles(stripe_subscription_id);

ALTER TABLE student_profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_profile"
    ON student_profiles FOR SELECT
    USING (id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_update_own_profile"
    ON student_profiles FOR UPDATE
    USING  (id = auth.uid())
    WITH CHECK (id = auth.uid());

CREATE POLICY IF NOT EXISTS "service_role_full_access_student_profiles"
    ON student_profiles FOR ALL
    USING (auth.role() = 'service_role');


-- ─────────────────────────────────────────────────────────────────────────────
-- 002  Test questions, sessions & share events   (Segment C)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS test_questions (
    id             uuid  PRIMARY KEY DEFAULT gen_random_uuid(),
    language       text  NOT NULL,
    cefr_level     text  NOT NULL,   -- 'A1' | 'A2' | 'B1' | 'B2' | 'C1'
    skill_area     text  NOT NULL,   -- 'reading' | 'listening' | 'vocabulary' | 'grammar'
    question_type  text  NOT NULL,
        -- 'multiple_choice' | 'audio_mc' | 'image_mc' | 'reorder'
        -- | 'fill_conjugation' | 'register_id' | 'idiom' | 'reading_comp'
    content        jsonb NOT NULL,   -- type-specific payload (see spec §C.1)
    correct_answer text  NOT NULL,
    audio_url      text,
    image_url      text,
    fsi_lesson_ref text,
    ai_generated   boolean DEFAULT true,
    human_reviewed boolean DEFAULT false,
    active         boolean DEFAULT true,
    flag_count     int     DEFAULT 0,
    created_at     timestamptz DEFAULT now(),
    updated_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_test_questions_language_active
    ON test_questions(language, active, cefr_level, skill_area);

CREATE TABLE IF NOT EXISTS test_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    language            text NOT NULL,
    status              text DEFAULT 'phase_1',
        -- 'phase_1' | 'phase_1_complete' | 'phase_2' | 'completed' | 'abandoned'
    email               text,
    name                text,
    referrer_student_id uuid REFERENCES auth.users(id),
    referral_code       text,           -- links to test_referrals.referral_code
    question_ids        uuid[] NOT NULL,
    answers             jsonb  DEFAULT '{}',
    phase_1_score       jsonb,
    final_score         jsonb,
    cefr_result         text,
    adaptive_state      jsonb  DEFAULT '{}',
    started_at          timestamptz DEFAULT now(),
    phase_2_started_at  timestamptz,
    completed_at        timestamptz,
    ip_hash             text,
    user_agent          text,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_test_sessions_email
    ON test_sessions(email);
CREATE INDEX IF NOT EXISTS idx_test_sessions_referrer
    ON test_sessions(referrer_student_id);

CREATE TABLE IF NOT EXISTS test_share_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    test_session_id uuid NOT NULL REFERENCES test_sessions(id),
    channel         text NOT NULL,   -- 'whatsapp' | 'email' | 'copy_link'
    recipient_count int  DEFAULT 1,
    created_at      timestamptz DEFAULT now()
);

-- FastAPI-only tables — no client-side anon/authenticated access needed.
ALTER TABLE test_questions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_sessions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_share_events ENABLE ROW LEVEL SECURITY;


-- ─────────────────────────────────────────────────────────────────────────────
-- 002b  Referral & share tracking   (Segment C.5 — partial_spec.md)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS test_referrals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    referral_code   text UNIQUE NOT NULL,

    -- Sender
    sender_type       text NOT NULL,        -- 'visitor' | 'student'
    sender_name       text,
    sender_email      text,
    sender_student_id uuid REFERENCES auth.users(id),

    -- Intent & channel
    intent          text,                   -- 'validate' | 'peer' | null
    channel         text,                   -- 'whatsapp' | 'email' | 'copy_link'
    recipient_email text,

    -- Tracking
    language        text NOT NULL,
    link_opened     boolean DEFAULT false,
    link_opened_at  timestamptz,
    test_started    boolean DEFAULT false,
    test_session_id uuid REFERENCES test_sessions(id),
    test_completed  boolean DEFAULT false,
    approved        boolean DEFAULT false,

    -- Notification state
    sender_notified_on_completion boolean DEFAULT false,
    sender_notified_on_approval   boolean DEFAULT false,

    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_referrals_code
    ON test_referrals(referral_code);
CREATE INDEX IF NOT EXISTS idx_referrals_sender_email
    ON test_referrals(sender_email);
CREATE INDEX IF NOT EXISTS idx_referrals_sender_student
    ON test_referrals(sender_student_id);

ALTER TABLE test_referrals ENABLE ROW LEVEL SECURITY;

-- Authenticated students read their own sent referrals (in-app Invites view)
CREATE POLICY IF NOT EXISTS "students_read_own_referrals"
    ON test_referrals FOR SELECT
    USING (sender_student_id = auth.uid());


-- ─────────────────────────────────────────────────────────────────────────────
-- 003  Supporters & milestone notifications   (Segment D)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS test_supporters (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_email       text NOT NULL,
    supporter_name        text NOT NULL,
    student_id            uuid NOT NULL REFERENCES auth.users(id),
    test_session_id       uuid NOT NULL REFERENCES test_sessions(id),
    status                text DEFAULT 'approved',
        -- 'approved' | 'opted_in' | 'opted_out' | 'unsubscribed'
    opted_in_at           timestamptz,
    student_visible       boolean DEFAULT true,
    last_notified_at      timestamptz,
    milestone_email_count int     DEFAULT 0,
    gift_nudge_shown      boolean DEFAULT false,
    gift_nudge_shown_at   timestamptz,
    engagement_score      int     DEFAULT 0,
    created_at            timestamptz DEFAULT now(),

    UNIQUE(supporter_email, student_id)
);

CREATE INDEX IF NOT EXISTS idx_supporters_student
    ON test_supporters(student_id);
CREATE INDEX IF NOT EXISTS idx_supporters_status
    ON test_supporters(status);

CREATE TABLE IF NOT EXISTS milestone_notifications (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id     uuid NOT NULL REFERENCES auth.users(id),
    supporter_id   uuid NOT NULL REFERENCES test_supporters(id),
    milestone_type text NOT NULL,
        -- 'unit_complete' | 'streak_14' | 'first_ai_convo'
        -- | 'first_cohort_session' | 'cefr_level_up'
    milestone_data jsonb NOT NULL,
    status         text DEFAULT 'pending',
        -- 'pending' | 'sent' | 'failed' | 'skipped'
    sent_at        timestamptz,
    created_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_milestones_pending
    ON milestone_notifications(status) WHERE status = 'pending';

ALTER TABLE test_supporters        ENABLE ROW LEVEL SECURITY;
ALTER TABLE milestone_notifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_supporters"
    ON test_supporters FOR SELECT
    USING (student_id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_toggle_own_supporter_visibility"
    ON test_supporters FOR UPDATE
    USING  (student_id = auth.uid())
    WITH CHECK (student_id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_read_own_milestones"
    ON milestone_notifications FOR SELECT
    USING (student_id = auth.uid());


-- ─────────────────────────────────────────────────────────────────────────────
-- 004  Gift subscriptions   (Segment E)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gift_subscriptions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_id       uuid NOT NULL REFERENCES test_supporters(id),
    student_id         uuid NOT NULL REFERENCES auth.users(id),
    stripe_payment_id  text NOT NULL,
    stripe_receipt_url text,
    tier               text DEFAULT 'tutor',
    duration_months    int  NOT NULL,   -- 1 or 3
    amount_cents       int  NOT NULL,   -- 4000 or 12000
    currency           text DEFAULT 'usd',
    anonymous          boolean DEFAULT false,
    status             text DEFAULT 'pending',
        -- 'pending' | 'active' | 'expired' | 'refunded'
    activated_at       timestamptz,
    expires_at         timestamptz,
    expiry_warned      boolean DEFAULT false,
    renewal_nudge_sent boolean DEFAULT false,
    refunded_at        timestamptz,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gifts_student
    ON gift_subscriptions(student_id);
CREATE INDEX IF NOT EXISTS idx_gifts_status
    ON gift_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_gifts_expires
    ON gift_subscriptions(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_gifts_stripe
    ON gift_subscriptions(stripe_payment_id);

-- Add FK from student_profiles once gift_subscriptions exists
ALTER TABLE student_profiles
    ADD CONSTRAINT IF NOT EXISTS fk_student_active_gift
    FOREIGN KEY (active_gift_id) REFERENCES gift_subscriptions(id);

ALTER TABLE gift_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_gifts"
    ON gift_subscriptions FOR SELECT
    USING (student_id = auth.uid());


-- ─────────────────────────────────────────────────────────────────────────────
-- 005  Testimonial requests   (Segment F)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS testimonial_requests (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_id       uuid NOT NULL REFERENCES test_supporters(id),
    gift_id            uuid NOT NULL REFERENCES gift_subscriptions(id),
    status             text DEFAULT 'pending',
        -- 'pending' | 'sent' | 'submitted' | 'approved' | 'declined' | 'published'
    sent_at            timestamptz,
    quote_text         text,
    display_name       text,
    display_preference text,   -- 'full_name' | 'first_name_initial' | 'anonymous'
    approved_by_admin  boolean DEFAULT false,
    published_at       timestamptz,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_testimonials_status
    ON testimonial_requests(status);
CREATE INDEX IF NOT EXISTS idx_testimonials_published
    ON testimonial_requests(status) WHERE status = 'published';

ALTER TABLE testimonial_requests ENABLE ROW LEVEL SECURITY;

-- Marketing site (Next.js anon key) reads published testimonials
CREATE POLICY IF NOT EXISTS "public_read_published_testimonials"
    ON testimonial_requests FOR SELECT
    USING (status = 'published');


-- ─────────────────────────────────────────────────────────────────────────────
-- 006  Tutor portal tables   (partial_spec.md)
-- ─────────────────────────────────────────────────────────────────────────────

-- Tutor profiles (rate, language, bio)
CREATE TABLE IF NOT EXISTS tutor_profiles (
    id                    uuid PRIMARY KEY REFERENCES auth.users(id),
    per_session_rate_cents int DEFAULT 0,
    language              text,
    bio                   text,
    created_at            timestamptz DEFAULT now(),
    updated_at            timestamptz DEFAULT now()
);

ALTER TABLE tutor_profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_manage_own_profile"
    ON tutor_profiles FOR ALL
    USING (id = auth.uid());

-- Tutor availability (for session scheduling)
CREATE TABLE IF NOT EXISTS tutor_availability (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id     uuid NOT NULL REFERENCES auth.users(id),
    day_of_week  int  NOT NULL,   -- 0 = Monday, 6 = Sunday
    start_time   time NOT NULL,
    end_time     time NOT NULL,
    timezone     text NOT NULL DEFAULT 'UTC',
    active       boolean DEFAULT true,
    created_at   timestamptz DEFAULT now(),
    updated_at   timestamptz DEFAULT now(),

    UNIQUE(tutor_id, day_of_week, start_time)
);

ALTER TABLE tutor_availability ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_manage_own_availability"
    ON tutor_availability FOR ALL
    USING (tutor_id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_read_tutor_availability"
    ON tutor_availability FOR SELECT
    USING (active = true);

-- Tutor payout settings (sensitive — Fernet-encrypted at rest, never client-readable)
CREATE TABLE IF NOT EXISTS tutor_payout_settings (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id          uuid UNIQUE NOT NULL REFERENCES auth.users(id),
    method            text NOT NULL,        -- 'bank' | 'mpesa'
    encrypted_details text NOT NULL,        -- Fernet-encrypted JSON blob
    details_last4     text,                 -- last 4 digits for display
    verified          boolean DEFAULT false,
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now()
);

ALTER TABLE tutor_payout_settings ENABLE ROW LEVEL SECURITY;

-- Tutors may read the masked row (mobile app settings page)
-- The encrypted_details column is returned — but the app must NOT display it.
-- FastAPI is the only consumer that decrypts it.
CREATE POLICY IF NOT EXISTS "tutors_read_own_payout_display"
    ON tutor_payout_settings FOR SELECT
    USING (tutor_id = auth.uid());

-- Tutor–student assignments
CREATE TABLE IF NOT EXISTS tutor_students (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id    uuid NOT NULL REFERENCES auth.users(id),
    student_id  uuid NOT NULL REFERENCES auth.users(id),
    language    text,
    active      boolean DEFAULT true,
    started_at  timestamptz DEFAULT now(),
    ended_at    timestamptz,

    UNIQUE(tutor_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_tutor_students_tutor
    ON tutor_students(tutor_id);

ALTER TABLE tutor_students ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_read_own_students"
    ON tutor_students FOR SELECT
    USING (tutor_id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_read_own_tutor"
    ON tutor_students FOR SELECT
    USING (student_id = auth.uid());

-- Tutor sessions (source of truth for earnings)
CREATE TABLE IF NOT EXISTS tutor_sessions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id         uuid NOT NULL REFERENCES auth.users(id),
    student_ids      uuid[],
    student_count    int  DEFAULT 1,
    session_type     text DEFAULT 'cohort',  -- 'cohort' | 'one_on_one'
    duration_minutes int,
    started_at       timestamptz DEFAULT now(),
    ended_at         timestamptz,
    topic            text,
    notes            text,
    created_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tutor_sessions_tutor
    ON tutor_sessions(tutor_id, started_at DESC);

ALTER TABLE tutor_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_read_own_sessions"
    ON tutor_sessions FOR SELECT
    USING (tutor_id = auth.uid());


-- =============================================================================
-- End of consolidated migration
-- =============================================================================
