-- =============================================================================
-- MISSING DEFINITIONS
-- =============================================================================
--
-- APPROACH
-- ─────────────────────────────────────────────────────────────────────────────
-- Rather than creating new public-schema tables (student_profiles, tutor_profiles,
-- tutor_students), we extend the existing app.student and app.tutor tables with
-- the required columns. This keeps all tables in the app schema, consistent with
-- CLAUDE.md and the rest of the database.
--
-- Remaining tables (test_questions, gift_subscriptions, etc.) have no existing
-- equivalent and are created fresh in the app schema.
--
-- Python services must use supabase.schema("app").table(...) for all tables.
--
-- TABLE INVENTORY
-- ─────────────────────────────────────────────────────────────────────────────
-- Extended (ALTER TABLE):
--   app.student   — adds Stripe/tier/milestone fields (was: student_profiles)
--                   adds language/tutor_started_at/tutor_ended_at (was: tutor_students)
--   app.tutor     — adds rate/bio/language fields (was: tutor_profiles)
--
-- Net-new (CREATE TABLE):
--   app.test_questions
--   app.test_sessions
--   app.test_share_events
--   app.test_referrals
--   app.test_supporters
--   app.milestone_notifications
--   app.gift_subscriptions
--   app.testimonial_requests
--   app.tutor_availability
--   app.tutor_payout_settings
--   app.tutor_sessions
-- =============================================================================


-- =============================================================================
-- 001  Extend app.student
-- Adds Stripe subscription state, tier, milestone progress (formerly student_profiles)
-- and tutor-relationship fields (formerly tutor_students).
-- app.student.tutor_id already exists; we add language, start/end tracking.
-- =============================================================================

ALTER TABLE app.student
    ADD COLUMN IF NOT EXISTS tier                     text NOT NULL DEFAULT 'free',
    ADD COLUMN IF NOT EXISTS tier_source              text NOT NULL DEFAULT 'self',
    ADD COLUMN IF NOT EXISTS stripe_customer_id       text UNIQUE,
    ADD COLUMN IF NOT EXISTS stripe_subscription_id   text,
    ADD COLUMN IF NOT EXISTS subscription_status      text DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS active_gift_id           uuid,   -- FK added after app.gift_subscriptions is created
    ADD COLUMN IF NOT EXISTS original_tier            text,
    ADD COLUMN IF NOT EXISTS current_period_end       timestamptz,
    ADD COLUMN IF NOT EXISTS lessons_completed        int     DEFAULT 0,
    ADD COLUMN IF NOT EXISTS current_streak           int     DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ai_conversations         int     DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cohort_sessions_attended int     DEFAULT 0,
    ADD COLUMN IF NOT EXISTS assessed_level           text,
    ADD COLUMN IF NOT EXISTS last_unit_milestone      int     DEFAULT 0,
    ADD COLUMN IF NOT EXISTS streak_14_notified       boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS first_ai_convo_notified  boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS first_cohort_notified    boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS last_notified_level      text,
    ADD COLUMN IF NOT EXISTS language                 text,   -- language studied with this tutor
    ADD COLUMN IF NOT EXISTS tutor_started_at         timestamptz, -- when tutor assignment began
    ADD COLUMN IF NOT EXISTS tutor_ended_at           timestamptz, -- NULL = relationship ongoing
    ADD COLUMN IF NOT EXISTS updated_at               timestamptz DEFAULT now();

-- tier check: 'free' | 'learner' | 'tutor' | 'intensive'
-- tier_source check: 'self' | 'gifted' | 'admin'
-- subscription_status check: 'none' | 'active' | 'past_due' | 'cancelled' | 'trialing'

CREATE INDEX IF NOT EXISTS idx_student_stripe_customer
    ON app.student(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_student_stripe_subscription
    ON app.student(stripe_subscription_id);


-- =============================================================================
-- 002  Extend app.tutor
-- Adds financial and bio fields needed by the Python tutor portal
-- (formerly a separate tutor_profiles table).
-- =============================================================================

ALTER TABLE app.tutor
    ADD COLUMN IF NOT EXISTS per_session_rate_cents int DEFAULT 0,
    ADD COLUMN IF NOT EXISTS language               text,
    ADD COLUMN IF NOT EXISTS bio                    text,
    ADD COLUMN IF NOT EXISTS updated_at             timestamptz DEFAULT now();


-- =============================================================================
-- 003  app.test_questions
-- Purpose: Question bank for the placement test. Admin-managed; served to
--          unauthenticated visitors via the Python backend (service role).
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.test_questions (
    id             uuid  PRIMARY KEY DEFAULT gen_random_uuid(),
    language       text  NOT NULL,
    cefr_level     text  NOT NULL,
    skill_area     text  NOT NULL,
    question_type  text  NOT NULL,
    content        jsonb NOT NULL,
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
    ON app.test_questions(language, active, cefr_level, skill_area);

ALTER TABLE app.test_questions ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- 004  app.test_sessions
-- Purpose: Per-user (or anonymous visitor) placement test run.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.test_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    language            text NOT NULL,
    status              text DEFAULT 'phase_1',
    email               text,
    name                text,
    referrer_student_id uuid REFERENCES auth.users(id),
    referral_code       text,
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
    ON app.test_sessions(email);
CREATE INDEX IF NOT EXISTS idx_test_sessions_referrer
    ON app.test_sessions(referrer_student_id);

ALTER TABLE app.test_sessions ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- 005  app.test_share_events
-- Purpose: Track each time a completed test result is shared.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.test_share_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    test_session_id uuid NOT NULL REFERENCES app.test_sessions(id),
    channel         text NOT NULL,
    recipient_count int  DEFAULT 1,
    created_at      timestamptz DEFAULT now()
);

ALTER TABLE app.test_share_events ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- 006  app.test_referrals
-- Purpose: Referral codes generated when a student shares the test.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.test_referrals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    referral_code   text UNIQUE NOT NULL,

    sender_type       text NOT NULL,
    sender_name       text,
    sender_email      text,
    sender_student_id uuid REFERENCES auth.users(id),

    intent          text,
    channel         text,
    recipient_email text,

    language        text NOT NULL,
    link_opened     boolean DEFAULT false,
    link_opened_at  timestamptz,
    test_started    boolean DEFAULT false,
    test_session_id uuid REFERENCES app.test_sessions(id),
    test_completed  boolean DEFAULT false,
    approved        boolean DEFAULT false,

    sender_notified_on_completion boolean DEFAULT false,
    sender_notified_on_approval   boolean DEFAULT false,

    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_referrals_code
    ON app.test_referrals(referral_code);
CREATE INDEX IF NOT EXISTS idx_referrals_sender_email
    ON app.test_referrals(sender_email);
CREATE INDEX IF NOT EXISTS idx_referrals_sender_student
    ON app.test_referrals(sender_student_id);

ALTER TABLE app.test_referrals ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_referrals"
    ON app.test_referrals FOR SELECT TO authenticated
    USING (sender_student_id = auth.uid());


-- =============================================================================
-- 007  app.test_supporters
-- Purpose: People who follow a student's learning progress.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.test_supporters (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_email       text NOT NULL,
    supporter_name        text NOT NULL,
    student_id            uuid NOT NULL REFERENCES auth.users(id),
    test_session_id       uuid NOT NULL REFERENCES app.test_sessions(id),
    status                text DEFAULT 'approved',
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
    ON app.test_supporters(student_id);
CREATE INDEX IF NOT EXISTS idx_supporters_status
    ON app.test_supporters(status);

ALTER TABLE app.test_supporters ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_supporters"
    ON app.test_supporters FOR SELECT TO authenticated
    USING (student_id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_toggle_own_supporter_visibility"
    ON app.test_supporters FOR UPDATE TO authenticated
    USING  (student_id = auth.uid())
    WITH CHECK (student_id = auth.uid());


-- =============================================================================
-- 008  app.milestone_notifications
-- Purpose: Queue of milestone emails to send to supporters.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.milestone_notifications (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id     uuid NOT NULL REFERENCES auth.users(id),
    supporter_id   uuid NOT NULL REFERENCES app.test_supporters(id),
    milestone_type text NOT NULL,
    milestone_data jsonb NOT NULL,
    status         text DEFAULT 'pending',
    sent_at        timestamptz,
    created_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_milestones_pending
    ON app.milestone_notifications(status) WHERE status = 'pending';

ALTER TABLE app.milestone_notifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_milestones"
    ON app.milestone_notifications FOR SELECT TO authenticated
    USING (student_id = auth.uid());


-- =============================================================================
-- 009  app.gift_subscriptions
-- Purpose: Gift payment records — supporter pays for student's subscription.
-- Note:    After creating this table, we add the FK from app.student.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.gift_subscriptions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_id       uuid NOT NULL REFERENCES app.test_supporters(id),
    student_id         uuid NOT NULL REFERENCES auth.users(id),
    stripe_payment_id  text NOT NULL,
    stripe_receipt_url text,
    tier               text DEFAULT 'tutor',
    duration_months    int  NOT NULL,
    amount_cents       int  NOT NULL,
    currency           text DEFAULT 'usd',
    anonymous          boolean DEFAULT false,
    status             text DEFAULT 'pending',
    activated_at       timestamptz,
    expires_at         timestamptz,
    expiry_warned      boolean DEFAULT false,
    renewal_nudge_sent boolean DEFAULT false,
    refunded_at        timestamptz,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gifts_student
    ON app.gift_subscriptions(student_id);
CREATE INDEX IF NOT EXISTS idx_gifts_status
    ON app.gift_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_gifts_expires
    ON app.gift_subscriptions(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_gifts_stripe
    ON app.gift_subscriptions(stripe_payment_id);

-- Deferred FK — add now that app.gift_subscriptions exists
ALTER TABLE app.student
    ADD CONSTRAINT IF NOT EXISTS fk_student_active_gift
    FOREIGN KEY (active_gift_id) REFERENCES app.gift_subscriptions(id);

ALTER TABLE app.gift_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "students_read_own_gifts"
    ON app.gift_subscriptions FOR SELECT TO authenticated
    USING (student_id = auth.uid());


-- =============================================================================
-- 010  app.testimonial_requests
-- Purpose: Post-gift testimonial flow.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.testimonial_requests (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_id       uuid NOT NULL REFERENCES app.test_supporters(id),
    gift_id            uuid NOT NULL REFERENCES app.gift_subscriptions(id),
    status             text DEFAULT 'pending',
    sent_at            timestamptz,
    quote_text         text,
    display_name       text,
    display_preference text,
    approved_by_admin  boolean DEFAULT false,
    published_at       timestamptz,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_testimonials_status
    ON app.testimonial_requests(status);
CREATE INDEX IF NOT EXISTS idx_testimonials_published
    ON app.testimonial_requests(status) WHERE status = 'published';

ALTER TABLE app.testimonial_requests ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "public_read_published_testimonials"
    ON app.testimonial_requests FOR SELECT
    USING (status = 'published');


-- =============================================================================
-- 011  app.tutor_availability
-- Purpose: Weekly availability slots for session scheduling.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.tutor_availability (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id     uuid NOT NULL REFERENCES auth.users(id),
    day_of_week  int  NOT NULL,
    start_time   time NOT NULL,
    end_time     time NOT NULL,
    timezone     text NOT NULL DEFAULT 'UTC',
    active       boolean DEFAULT true,
    created_at   timestamptz DEFAULT now(),
    updated_at   timestamptz DEFAULT now(),

    UNIQUE(tutor_id, day_of_week, start_time)
);

ALTER TABLE app.tutor_availability ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_manage_own_availability"
    ON app.tutor_availability FOR ALL TO authenticated
    USING (tutor_id = auth.uid());

CREATE POLICY IF NOT EXISTS "students_read_tutor_availability"
    ON app.tutor_availability FOR SELECT TO authenticated
    USING (active = true);


-- =============================================================================
-- 012  app.tutor_payout_settings
-- Purpose: Fernet-encrypted bank / M-Pesa payout details.
--          FastAPI is the ONLY consumer that decrypts this.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.tutor_payout_settings (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id          uuid UNIQUE NOT NULL REFERENCES auth.users(id),
    method            text NOT NULL,
    encrypted_details text NOT NULL,
    details_last4     text,
    verified          boolean DEFAULT false,
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now()
);

ALTER TABLE app.tutor_payout_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_read_own_payout_display"
    ON app.tutor_payout_settings FOR SELECT TO authenticated
    USING (tutor_id = auth.uid());


-- =============================================================================
-- 013  app.tutor_sessions
-- Purpose: Completed teaching sessions — source of truth for earnings.
--          Distinct from app.video_session (scheduled/upcoming). This tracks
--          completed sessions.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app.tutor_sessions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id         uuid NOT NULL REFERENCES auth.users(id),
    student_ids      uuid[],
    student_count    int  DEFAULT 1,
    session_type     text DEFAULT 'cohort',
    duration_minutes int,
    started_at       timestamptz DEFAULT now(),
    ended_at         timestamptz,
    topic            text,
    notes            text,
    created_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tutor_sessions_tutor
    ON app.tutor_sessions(tutor_id, started_at DESC);

ALTER TABLE app.tutor_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "tutors_read_own_sessions"
    ON app.tutor_sessions FOR SELECT TO authenticated
    USING (tutor_id = auth.uid());


-- =============================================================================
-- POST-CREATION: app.get_tutor_monthly_earnings RPC
-- tutor_service.py calls db.rpc("get_tutor_monthly_earnings", {...})
-- =============================================================================

CREATE OR REPLACE FUNCTION app.get_tutor_monthly_earnings(
    p_tutor_id uuid,
    p_limit    int DEFAULT 12,
    p_offset   int DEFAULT 0
)
RETURNS TABLE (
    month           text,
    sessions_count  bigint,
    gross_earnings  numeric,
    net_earnings    numeric
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = app
AS $$
    SELECT
        to_char(date_trunc('month', started_at), 'Month YYYY') AS month,
        COUNT(*)                                                AS sessions_count,
        COUNT(*) * (
            SELECT per_session_rate_cents::numeric / 100
            FROM app.tutor
            WHERE id = p_tutor_id
        )                                                       AS gross_earnings,
        COUNT(*) * (
            SELECT per_session_rate_cents::numeric / 100
            FROM app.tutor
            WHERE id = p_tutor_id
        ) * 0.80                                               AS net_earnings
    FROM app.tutor_sessions
    WHERE tutor_id = p_tutor_id
    GROUP BY date_trunc('month', started_at)
    ORDER BY date_trunc('month', started_at) DESC
    LIMIT p_limit OFFSET p_offset;
$$;
