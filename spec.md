# Tauka FastAPI Backend — Full API Specification

> **This is a multi-segment specification for the Tauka FastAPI backend.** Each segment (A through F) is a self-contained work unit that can be executed in one Claude Code session. Segments must be built in order — each depends on the one before it. The document opens with the existing project structure and shared infrastructure, then adds new capabilities segment by segment.

---

## 0. Existing Project & Conventions

### 0.1 What Already Exists

The FastAPI project is already running with three route modules:

```
app/
├── main.py                          # FastAPI app, router registration
├── dependencies.py                  # get_current_user (auth dependency)
├── routes/
│   ├── ai.py                        # POST /ai/generate — multi-provider AI calls with rate limiting
│   ├── messaging.py                 # POST /messages/send — store messages with rate limiting
│   └── calling.py                   # POST /calls/token — generate video call tokens (Daily.co)
├── models/
│   └── schemas.py                   # Pydantic models: AIRequest, CallRequest, MessageRequest
└── services/
    ├── ai_services.py               # call_ai() — routes to OpenAI/DeepSeek/Gemini
    ├── calling_service.py           # generate_call_token() — Daily.co integration
    ├── messaging_service.py         # store_message() — writes to Supabase
    ├── rate_limit_service.py        # check_rate_limit() — per-user, per-provider limits
    ├── token_service.py             # count_tokens() — tiktoken-based token counting
    └── usage_service.py             # log_usage() — writes usage events to Supabase
```

### 0.2 Established Patterns

All new code must follow these conventions already set by the existing routes:

**Authentication:** Every authenticated route uses `user_id: str = Depends(get_current_user)`. This dependency extracts and validates the Supabase JWT from the `Authorization: Bearer <token>` header. Anonymous/public routes omit this dependency.

**Rate limiting:** `check_rate_limit(user_id, estimated_cost, provider=..., mode=...)` returns `(allowed: bool, reason: str)`. If not allowed, raise `HTTPException(status_code=429, detail=reason)`.

**Usage logging:** `log_usage(user_id, event_type, quantity)` writes to Supabase. For background tasks: `bg.add_task(log_usage, ...)`. For synchronous fire-and-forget: call directly.

**Error handling:** Raise `HTTPException` with appropriate status codes. Use `400` for bad input, `404` for not found, `422` for validation failures, `429` for rate limits, `502` for upstream service failures.

**Pydantic models:** All request/response bodies are Pydantic `BaseModel` subclasses in `app/models/schemas.py`.

**Service layer:** Route handlers are thin — business logic lives in `app/services/`. One service file per domain.

### 0.3 External Dependencies (Already Configured)

| Dependency | Purpose | Client Location |
|---|---|---|
| Supabase (PostgreSQL) | Database, Auth, Storage, Realtime. Service role key for FastAPI (bypasses RLS), anon key + JWT for mobile/web clients (subject to RLS). See Section 0.6. | `app/services/supabase_client.py` |
| DeepSeek API | AI features, question validation | `app/services/ai_services.py` |
| Daily.co | Video calling | `app/services/calling_service.py` |
| tiktoken | Token counting | `app/services/token_service.py` |

### 0.4 New Dependencies to Add

| Dependency | Purpose | Segment |
|---|---|---|
| `stripe` | Payment processing (subscriptions + gifts) | A |
| `resend` (or `sendgrid`) | Transactional email | A |
| `slowapi` + `redis` (optional) | Application-level rate limiting for anonymous routes | A |
| `apscheduler` or `celery` (lightweight) | Scheduled cron tasks (milestones, gift lifecycle) | D |

### 0.5 Final Project Structure (After All Segments)

```
app/
├── main.py
├── dependencies.py
├── config.py                        # NEW — environment config, Stripe keys, email config
├── routes/
│   ├── ai.py                        # existing
│   ├── messaging.py                 # existing
│   ├── calling.py                   # existing
│   ├── subscriptions.py             # NEW (Segment A) — self-pay Stripe checkout + webhooks
│   ├── test.py                      # NEW (Segment C) — proficiency test flow
│   ├── supporters.py                # NEW (Segment D) — approve, opt-in, milestone prefs
│   ├── gifts.py                     # NEW (Segment E) — gift checkout, refund
│   └── testimonials.py              # NEW (Segment F) — testimonial submission
├── models/
│   └── schemas.py                   # extended with new Pydantic models per segment
├── services/
│   ├── supabase_client.py           # existing — Supabase connection (see Section 0.6)
│   ├── ai_services.py               # existing
│   ├── calling_service.py           # existing
│   ├── messaging_service.py         # existing
│   ├── rate_limit_service.py        # existing (extended in Segment A)
│   ├── token_service.py             # existing
│   ├── usage_service.py             # existing
│   ├── stripe_service.py            # NEW (Segment A) — Stripe client, checkout, webhooks
│   ├── email_service.py             # NEW (Segment A) — transactional email abstraction
│   ├── tier_service.py              # NEW (Segment A) — upgrade/downgrade student roles
│   ├── test_service.py              # NEW (Segment C) — question selection, scoring, adaptive logic
│   ├── question_bank_service.py     # NEW (Segment C) — question generation pipeline
│   ├── supporter_service.py         # NEW (Segment D) — supporter lifecycle
│   ├── milestone_service.py         # NEW (Segment D) — milestone detection + notification
│   ├── gift_service.py              # NEW (Segment E) — gift lifecycle, activation, expiry
│   └── testimonial_service.py       # NEW (Segment F) — testimonial request + collection
├── templates/
│   └── email/                       # NEW (Segment A) — Jinja2 email templates
├── tasks/
│   ├── scheduler.py                 # NEW (Segment D) — cron job orchestrator
│   ├── milestone_check.py           # NEW (Segment D) — daily milestone scan
│   ├── gift_lifecycle.py            # NEW (Segment E) — daily gift expiry/renewal
│   └── testimonial_outreach.py      # NEW (Segment F) — daily testimonial request sender
└── migrations/
    ├── 001_subscriptions.sql        # NEW (Segment A)
    ├── 002_test_questions.sql       # NEW (Segment C)
    ├── 003_supporters.sql           # NEW (Segment D)
    ├── 004_gifts.sql                # NEW (Segment E)
    └── 005_testimonials.sql         # NEW (Segment F)
```

### 0.6 Database Architecture — Supabase as Primary Database

Supabase provides the PostgreSQL database, authentication (auth.users), file storage, and real-time subscriptions. There are **two distinct access patterns** to the same database, and every table must account for both:

**Pattern 1 — FastAPI (server-side, service role key):**
The FastAPI app connects to Supabase using `supabase-py` with the **service role key**. This key bypasses all Row Level Security (RLS) policies, giving the API full read/write access to every table. The FastAPI app is therefore responsible for enforcing access control in its own route handlers via `get_current_user` and explicit permission checks. All write operations (creating sessions, updating tiers, inserting gifts, sending notifications) go through the FastAPI app.

**Pattern 2 — Mobile/Web App (client-side, anon key + user JWT):**
The Flutter mobile app and the Next.js web app connect to Supabase directly using `supabase-js`/`supabase_flutter` with the **anon key** plus the authenticated user's JWT. These connections **are subject to RLS policies**. The mobile app reads certain tables directly for real-time UI updates (student profile, supporter list, active gifts, milestone history) without going through the FastAPI API. Any table the mobile app needs to query must have RLS policies that allow the authenticated student to read their own rows.

**Supabase client setup (`app/services/supabase_client.py`):**

```python
from supabase import create_client, Client
from app.config import settings

# Service role client — used by all FastAPI services
# Bypasses RLS. NEVER expose this to the client.
supabase_admin: Client = create_client(
    settings.supabase_url,
    settings.supabase_service_key,
)

# Anon client — only used for operations that should respect RLS
# (rarely needed in FastAPI, but available for testing/validation)
supabase_anon: Client = create_client(
    settings.supabase_url,
    settings.supabase_anon_key,
)
```

**Convention for all services:**
```python
from app.services.supabase_client import supabase_admin as db

# All database operations use the admin client
result = db.table("student_profiles").select("*").eq("id", student_id).single().execute()
```

**Which tables does the mobile app query directly (requiring RLS)?**

| Table | Mobile App Access | RLS Needed |
|---|---|---|
| `student_profiles` | Student reads own profile (tier, gift status) | Yes — read own row |
| `test_sessions` | No — results delivered via API response and email | No (FastAPI only) |
| `test_questions` | No — questions served via API only | No (FastAPI only) |
| `test_supporters` | Student reads own supporters list (settings page) | Yes — read where student_id = auth.uid() |
| `gift_subscriptions` | Student reads own active gifts | Yes — read where student_id = auth.uid() |
| `milestone_notifications` | Student reads own milestone history | Yes — read where student_id = auth.uid() |
| `testimonial_requests` | Public reads published testimonials (marketing site) | Yes — read where status = 'published' |
| `test_share_events` | No — analytics only | No (FastAPI only) |

**Critical rule:** Every migration in this spec includes RLS policies for tables the mobile app touches. Tables accessed only through FastAPI still enable RLS (defence in depth) but only grant access to the `service_role` and `admin` — the anon key cannot read them even if the FastAPI app is the only consumer today.

---

**Purpose:** Build the Stripe integration, email service, and tier management that every later segment depends on. This is the base layer.

**Depends on:** Existing project only.

**Outputs:** `routes/subscriptions.py`, `services/stripe_service.py`, `services/email_service.py`, `services/tier_service.py`, `config.py`, `migrations/001_subscriptions.sql`, updated `main.py`, updated `models/schemas.py`.

### A.1 Configuration (`app/config.py`)

Centralised environment configuration using Pydantic `BaseSettings`:

```python
class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_service_key: str          # service role key for admin operations
    supabase_anon_key: str

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_learner_monthly: str  # Stripe Price ID for Learner $14/mo
    stripe_price_tutor_monthly: str    # Stripe Price ID for Tutor $40/mo
    stripe_price_intensive_monthly: str # Stripe Price ID for Intensive $119/mo
    stripe_price_gift_1mo: str         # Stripe Price ID for gift 1-month ($40 one-time)
    stripe_price_gift_3mo: str         # Stripe Price ID for gift 3-month ($120 one-time)

    # Email
    email_provider: str = "resend"     # "resend" or "sendgrid"
    email_api_key: str
    email_from_address: str = "Tauka <hello@tauka.com>"

    # App
    web_base_url: str = "https://www.tauka.com"
    app_base_url: str = "https://app.tauka.com"

    class Config:
        env_file = ".env"
```

### A.2 Database Migration (`migrations/001_subscriptions.sql`)

```sql
-- Student subscription tracking (extends auth.users via a profile table)
-- This table may already exist — add these columns if so, or create if not.

CREATE TABLE IF NOT EXISTS student_profiles (
    id              uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tier            text NOT NULL DEFAULT 'free',
        -- 'free', 'learner', 'tutor', 'intensive'
    tier_source     text NOT NULL DEFAULT 'self',
        -- 'self' (they paid), 'gifted' (supporter paid), 'admin' (manually set)
    stripe_customer_id    text UNIQUE,
    stripe_subscription_id text,
    subscription_status   text DEFAULT 'none',
        -- 'none', 'active', 'past_due', 'cancelled', 'trialing'
    active_gift_id  uuid,               -- FK to gift_subscriptions, set when gift is active
    original_tier   text,               -- stored when gift overrides, restored on gift expiry
    current_period_end timestamptz,      -- when current billing period ends
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

-- Index for Stripe webhook lookups
CREATE INDEX IF NOT EXISTS idx_student_profiles_stripe_customer
    ON student_profiles(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_student_profiles_stripe_subscription
    ON student_profiles(stripe_subscription_id);

-- ═══ RLS POLICIES ═══
-- Mobile app reads this table directly for profile/tier display.
-- All writes go through FastAPI (service role, bypasses RLS).
ALTER TABLE student_profiles ENABLE ROW LEVEL SECURITY;

-- Students can read their own profile
CREATE POLICY "students_read_own_profile"
    ON student_profiles FOR SELECT
    USING (id = auth.uid());

-- Students can update limited fields on their own profile (display prefs, not tier)
-- Tier changes are ONLY done via FastAPI service role.
CREATE POLICY "students_update_own_profile"
    ON student_profiles FOR UPDATE
    USING (id = auth.uid())
    WITH CHECK (id = auth.uid());

-- FastAPI inserts profiles during onboarding (service role bypasses this,
-- but the policy exists for defence in depth)
CREATE POLICY "service_role_full_access"
    ON student_profiles FOR ALL
    USING (auth.role() = 'service_role');
```

### A.3 Tier Service (`app/services/tier_service.py`)

The single source of truth for changing a student's tier. Every pathway — self-pay, gift activation, gift expiry, admin override — calls this service.

```python
async def change_tier(
    student_id: str,
    new_tier: str,           # 'free', 'learner', 'tutor', 'intensive'
    source: str,             # 'self', 'gifted', 'admin'
    gift_id: str | None = None,
    preserve_original: bool = False,  # True when a gift overrides a self-paid tier
) -> dict:
    """
    Changes a student's tier. Handles:
    - Updating student_profiles.tier and tier_source
    - If preserve_original=True, stores current tier in original_tier (for gift restore)
    - If source='gifted', sets active_gift_id
    - Fires a PostHog/Mixpanel event for analytics
    - Returns { "previous_tier": ..., "new_tier": ..., "source": ... }
    """
```

**Business rules enforced here:**
- A gift cannot downgrade a self-paid tier. If student is `intensive` (self-paid) and receives a `tutor` gift, the student keeps `intensive`. The gift is still recorded and tracked, but tier doesn't change.
- When a gift expires, restore `original_tier` if it was set. If `original_tier` is null, fall back to `free`.
- Tier changes are idempotent — calling `change_tier(student, "tutor", "self")` when already on `tutor:self` is a no-op.

### A.4 Email Service (`app/services/email_service.py`)

Abstraction over Resend/SendGrid. Every email sent by Tauka goes through this service.

```python
async def send_email(
    to: str | list[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,   # for email analytics grouping
) -> dict:
    """
    Sends transactional email via configured provider.
    Returns { "id": message_id, "status": "sent" | "failed" }
    """

async def send_template_email(
    to: str,
    template_name: str,
    template_data: dict,
    tags: list[str] | None = None,
) -> dict:
    """
    Renders an HTML email from a template name + data dict, then sends.
    Templates are stored as Jinja2 files in app/templates/email/.
    """
```

**Email templates directory:**
```
app/templates/email/
├── base.html                   # shared layout: Tauka header, footer, unsubscribe
├── test_result.html            # test completion results
├── supporter_approved.html     # "[name] recommends Tauka for you"
├── milestone_update.html       # milestone notification to supporter
├── milestone_with_gift.html    # milestone + gift nudge variant
├── gift_confirmation.html      # receipt for supporter after payment
├── gift_activated.html         # notification to student
├── gift_expiry_warning.html    # 14-day warning to student
├── gift_impact_summary.html    # post-expiry progress summary to supporter
├── testimonial_request.html    # ask supporter for a quote
└── subscription_receipt.html   # self-pay subscription confirmation
```

### A.5 Stripe Service (`app/services/stripe_service.py`)

```python
async def create_subscription_checkout(
    student_id: str,
    tier: str,               # 'learner', 'tutor', 'intensive'
    success_url: str,
    cancel_url: str,
    customer_email: str,
) -> dict:
    """
    Creates a Stripe Checkout Session for a recurring subscription.
    Returns { "checkout_url": str, "session_id": str }
    """

async def create_gift_checkout(
    supporter_id: str,
    student_id: str,
    duration_months: int,    # 1 or 3
    anonymous: bool,
    supporter_email: str,
    student_name: str,
    success_url: str,
    cancel_url: str,
) -> dict:
    """
    Creates a Stripe Checkout Session for a one-time gift payment.
    Metadata: supporter_id, student_id, duration_months, anonymous
    Returns { "checkout_url": str, "session_id": str }
    """

async def process_webhook_event(payload: bytes, sig_header: str) -> dict:
    """
    Verifies and processes a Stripe webhook event.
    Dispatches to the appropriate handler based on event type.
    Returns { "handled": bool, "event_type": str }
    """

async def issue_refund(payment_intent_id: str) -> dict:
    """
    Issues a full refund for a PaymentIntent.
    Returns { "refund_id": str, "status": str }
    """
```

**Webhook event handlers (internal, called by `process_webhook_event`):**

| Event | Handler | Action |
|---|---|---|
| `checkout.session.completed` | `_handle_checkout_complete` | Detect if subscription or gift via metadata. For subscriptions: create/update student_profiles, call `change_tier(source='self')`. For gifts: activate gift, call `change_tier(source='gifted')`, send notifications. |
| `customer.subscription.updated` | `_handle_subscription_update` | Sync tier if plan changed. Handle downgrades. |
| `customer.subscription.deleted` | `_handle_subscription_cancel` | Call `change_tier(new_tier='free', source='self')`. |
| `invoice.payment_failed` | `_handle_payment_failed` | Update `subscription_status` to `past_due`. Send warning email to student. |

### A.6 Routes (`app/routes/subscriptions.py`)

```python
router = APIRouter()

@router.post("/checkout")
async def create_checkout(req: SubscriptionCheckoutRequest, user_id: str = Depends(get_current_user)):
    """
    Authenticated student creates a Stripe Checkout for self-pay subscription.
    Returns { "checkout_url": str }
    """

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Stripe webhook endpoint. NO authentication (Stripe signs the payload).
    Verifies signature, dispatches to stripe_service.process_webhook_event().
    """

@router.get("/status")
async def subscription_status(user_id: str = Depends(get_current_user)):
    """
    Returns current subscription state for the authenticated student.
    { "tier": str, "source": str, "status": str, "current_period_end": str | null,
      "active_gift": { "gifted_by": str | "angel", "expires_at": str } | null }
    """

@router.post("/cancel")
async def cancel_subscription(user_id: str = Depends(get_current_user)):
    """
    Cancels self-pay subscription at period end (not immediate).
    """
```

**Register in `main.py`:**
```python
from app.routes import subscriptions
app.include_router(subscriptions.router, prefix="/subscriptions")
```

### A.7 Pydantic Models (add to `app/models/schemas.py`)

```python
class SubscriptionCheckoutRequest(BaseModel):
    tier: Literal["learner", "tutor", "intensive"]
    success_url: str | None = None
    cancel_url: str | None = None

class SubscriptionStatusResponse(BaseModel):
    tier: str
    source: str
    status: str
    current_period_end: str | None
    active_gift: dict | None
```

### A.8 Rate Limit Extension

Extend the existing `check_rate_limit` service (or add a middleware) to handle anonymous routes. The test endpoints (Segment C) need IP-based rate limiting for unauthenticated Phase 1 requests:

```python
def check_anonymous_rate_limit(
    ip_hash: str,
    endpoint: str,
    max_requests: int = 5,
    window_seconds: int = 3600,
) -> tuple[bool, str]:
    """
    Rate limits anonymous requests by hashed IP.
    Uses the same backing store as the authenticated rate limiter.
    """
```

---

## Segment B — Cloudflare Edge Configuration

**Purpose:** Configure Cloudflare (already in the Tauka stack) to protect the FastAPI backend.

**Depends on:** Segment A (needs to know which routes exist).

**This segment is infrastructure configuration, not application code.** It can be executed in parallel with later segments.

### B.1 Rate Limiting Rules (Cloudflare Dashboard or API)

| Route Pattern | Rule | Threshold | Action |
|---|---|---|---|
| `POST /test/*/questions` | Anonymous test starts | 5 req/IP/hour | Block with 429 |
| `POST /test/submit-*` | Test submissions | 10 req/IP/hour | Block with 429 |
| `POST /gifts/checkout` | Gift checkout creation | 3 req/IP/hour | Block with 429 |
| `POST /subscriptions/webhook` | Stripe webhooks | No limit | Pass through (Stripe IPs allowlisted) |
| `POST /ai/generate` | AI generation | 60 req/IP/minute | Challenge with JS |
| `GET /*` | Static/marketing pages | 200 req/IP/minute | Block with 429 |
| All other `POST` | Authenticated endpoints | 30 req/IP/minute | Block with 429 |

### B.2 Caching Rules

| Path | Cache | TTL | Notes |
|---|---|---|---|
| `/test/*/questions` response | No | — | Questions are session-specific |
| `/test/*/result/*/og-image` | Yes | 7 days | OG images are immutable once generated |
| Static assets (`/audio/*`, `/images/*`) | Yes | 30 days | CDN-cached audio and images |
| All API responses | No | — | Dynamic content |

### B.3 WAF Rules

- OWASP Core Rule Set enabled on all routes.
- Bot management: challenge suspicious automated traffic on `/test/*` routes.
- Stripe webhook IPs allowlisted (see Stripe docs for current IP ranges).

---

## Segment C — Test & Assessment System

**Purpose:** The public-facing language proficiency test at `/test/[language]`.

**Depends on:** Segment A (email service, rate limit extension).

**Outputs:** `routes/test.py`, `services/test_service.py`, `services/question_bank_service.py`, `migrations/002_test_questions.sql`, updated `main.py`, updated `models/schemas.py`.

### C.1 Database Migration (`migrations/002_test_questions.sql`)

```sql
CREATE TABLE test_questions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    language        text NOT NULL,
    cefr_level      text NOT NULL,              -- 'A1','A2','B1','B2','C1'
    skill_area      text NOT NULL,              -- 'reading','listening','vocabulary','grammar'
    question_type   text NOT NULL,
        -- 'multiple_choice','audio_mc','image_mc','reorder',
        -- 'fill_conjugation','register_id','idiom','reading_comp'
    content         jsonb NOT NULL,             -- type-specific payload
    correct_answer  text NOT NULL,
    audio_url       text,
    image_url       text,
    fsi_lesson_ref  text,                       -- e.g. 'amharic_lesson_14'
    ai_generated    boolean DEFAULT true,
    human_reviewed  boolean DEFAULT false,
    active          boolean DEFAULT true,
    flag_count      int DEFAULT 0,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

CREATE INDEX idx_test_questions_language_active
    ON test_questions(language, active, cefr_level, skill_area);

CREATE TABLE test_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    language            text NOT NULL,
    status              text DEFAULT 'phase_1',
        -- 'phase_1','phase_1_complete','phase_2','completed','abandoned'
    email               text,
    name                text,
    referrer_student_id uuid REFERENCES auth.users(id),
    referral_code       text,
    question_ids        uuid[] NOT NULL,
    answers             jsonb DEFAULT '{}',
    phase_1_score       jsonb,
    final_score         jsonb,
    cefr_result         text,
    adaptive_state      jsonb DEFAULT '{}',
    started_at          timestamptz DEFAULT now(),
    phase_2_started_at  timestamptz,
    completed_at        timestamptz,
    ip_hash             text,
    user_agent          text,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX idx_test_sessions_email ON test_sessions(email);
CREATE INDEX idx_test_sessions_referrer ON test_sessions(referrer_student_id);

CREATE TABLE test_share_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    test_session_id uuid NOT NULL REFERENCES test_sessions(id),
    channel         text NOT NULL,              -- 'whatsapp','email','copy_link'
    recipient_count int DEFAULT 1,
    created_at      timestamptz DEFAULT now()
);

-- ═══ RLS POLICIES ═══
-- These tables are accessed ONLY through FastAPI (service role).
-- The mobile app never queries them directly — test results are delivered
-- via API response and email. RLS is enabled for defence in depth,
-- but no anon/authenticated policies are needed beyond service_role.

ALTER TABLE test_questions ENABLE ROW LEVEL SECURITY;
-- No client-side access. FastAPI reads with service role.
-- If a future admin UI needs direct access, add admin policy then.

ALTER TABLE test_sessions ENABLE ROW LEVEL SECURITY;
-- No client-side access. All interactions go through /test/* API routes.
-- Contains PII (email, ip_hash) — must not be queryable from client.

ALTER TABLE test_share_events ENABLE ROW LEVEL SECURITY;
-- Analytics only. No client-side access.
```

**`content` JSONB structure by question_type:**

```jsonc
// multiple_choice, audio_mc, image_mc
{ "prompt": "What does ሰላም mean?",
  "options": ["Peace/Hello", "Goodbye", "Water", "Thank you"],
  "explanation": "ሰላም is the standard Amharic greeting." }

// reorder
{ "prompt": "Arrange into a correct sentence:",
  "words": ["ወደ", "ቤት", "እሄዳለሁ"],
  "correct_order": [0, 1, 2] }

// fill_conjugation
{ "prompt": "እሷ ቡና ___.",
  "options": ["ትጠጣለች", "ይጠጣል", "እጠጣለሁ", "ትጠጣላችሁ"],
  "context": "3rd person feminine singular, present tense" }

// register_id
{ "prompt": "Which phrasing is formal?",
  "option_a": "እባክዎ ይቀመጡ", "option_b": "ተቀመጥ",
  "correct": "a" }

// reading_comp
{ "passage": "Short Amharic paragraph...",
  "question": "What is the speaker's main concern?",
  "options": ["...", "...", "...", "..."] }

// idiom
{ "prompt": "\"ልብ ያለው ያስተውላል\" — what does this mean?",
  "options": ["The wise will understand", "The heart knows", "Love conquers", "Be patient"] }
```

### C.2 Test Service (`app/services/test_service.py`)

```python
async def create_test_session(
    language: str,
    ip_hash: str,
    user_agent: str,
    referrer_student_id: str | None = None,
    referral_code: str | None = None,
) -> dict:
    """
    Creates a new test session.
    1. Validate language has enough active questions.
    2. Select 5 Phase 1 questions: spread across skill areas, A1-A2 difficulty.
    3. Pre-select 15 Phase 2 questions: spread across skills and levels.
    4. Randomise option order per question.
    5. Store question_ids in session.
    6. Return session_id + Phase 1 questions (correct_answer stripped).
    """

async def submit_phase_1(session_id: str, answers: dict) -> dict:
    """
    Scores Phase 1 answers.
    1. Validate session exists and status is 'phase_1'.
    2. Score 5 answers against test_questions.correct_answer.
    3. Compute phase_1_score and adaptive_state (inferred floor).
    4. Update status to 'phase_1_complete'.
    5. Return { "correct": int, "total": 5, "continue": true }.
    """

async def capture_email(session_id: str, name: str, email: str) -> dict:
    """
    Captures name + email at the gate. Sets status to 'phase_2'.
    Returns the Phase 2 questions (stripped of answers).
    Adjusts starting difficulty based on adaptive_state.
    """

async def submit_final(session_id: str, answers: dict) -> dict:
    """
    Scores all 20 questions. Computes full breakdown.
    1. Score per skill area.
    2. Determine overall CEFR (grammar+reading weighted higher).
    3. Generate description text (template-based).
    4. Store final_score, cefr_result. Status → 'completed'.
    5. Send result email (background task).
    6. If referrer_student_id set, notify student (background task).
    7. Return full result payload.
    """

def compute_cefr_level(breakdown: dict) -> str:
    """
    Weighted CEFR calculation.
    Grammar: 30%, Reading: 30%, Vocabulary: 20%, Listening: 20%.
    Maps weighted score to CEFR band.
    """

def select_adaptive_questions(
    language: str,
    phase_1_floor: str,
    count: int = 15,
) -> list[dict]:
    """
    Selects Phase 2 questions with adaptive starting difficulty.
    If phase_1_floor is 'A2', skip A1 questions in Phase 2.
    Ensures spread: ~4 per skill area, ascending difficulty.
    """
```

### C.3 Question Bank Service (`app/services/question_bank_service.py`)

```python
async def generate_questions_from_fsi(
    language: str,
    lesson_number: int,
    cefr_level: str,
    count: int = 5,
) -> list[dict]:
    """
    Calls DeepSeek to generate test questions from FSI lesson content.
    1. Fetch lesson content from database.
    2. Send to DeepSeek with structured prompt (see spec below).
    3. Parse JSON response.
    4. Return list of question dicts ready for insertion.
    """

async def validate_questions(questions: list[dict]) -> list[dict]:
    """
    Sends generated questions back through DeepSeek for validation.
    Returns questions with 'valid' flag and 'issues' list.
    Only valid questions should be inserted.
    """

async def seed_question_bank(language: str) -> dict:
    """
    Full pipeline: iterate FSI lessons → generate → validate → insert.
    Returns { "generated": int, "valid": int, "inserted": int }.
    Admin-only operation.
    """
```

**DeepSeek generation prompt** (used inside `generate_questions_from_fsi`):
```
You are creating test questions for an Amharic language proficiency assessment.

Source material (FSI Lesson {n}, CEFR level {level}):
{lesson_content}

Generate {count} test questions. For each, output JSON:
{
  "question_type": "multiple_choice"|"audio_mc"|"reorder"|"fill_conjugation"|"register_id"|"reading_comp"|"idiom",
  "skill_area": "reading"|"listening"|"vocabulary"|"grammar",
  "cefr_level": "{level}",
  "content": { ... },
  "correct_answer": "...",
  "explanation": "..."
}

Requirements:
- Wrong options must be plausible (real Amharic words at similar level)
- Only one unambiguously correct answer
- Difficulty must match stated CEFR level
- No knowledge beyond the source lesson's scope

Generate ONLY the JSON array.
```

### C.4 Routes (`app/routes/test.py`)

```python
router = APIRouter()

@router.post("/{language}/start")
async def start_test(language: str, request: Request):
    """
    PUBLIC (no auth). Creates a test session.
    Rate limited: 5 starts/IP/hour via check_anonymous_rate_limit.
    Input: { "referrer_student_id"?: str, "referral_code"?: str }
    Returns: { "session_id": str, "questions": [...Phase 1 questions...] }
    """

@router.post("/submit-phase-1")
async def submit_phase_1(req: Phase1SubmitRequest):
    """
    PUBLIC. Scores Phase 1.
    Input: { "session_id": str, "answers": { question_id: answer, ... } }
    Returns: { "correct": int, "total": 5, "continue": true }
    """

@router.post("/capture-email")
async def capture_email(req: EmailCaptureRequest):
    """
    PUBLIC. Captures email, returns Phase 2 questions.
    Input: { "session_id": str, "name": str, "email": str }
    Returns: { "questions": [...Phase 2 questions...] }
    """

@router.post("/submit-final")
async def submit_final(req: FinalSubmitRequest, bg: BackgroundTasks):
    """
    PUBLIC. Scores full test, sends result email.
    Input: { "session_id": str, "answers": { question_id: answer, ... } }
    Returns: full result payload (score, breakdown, description, cefr_result)
    """

@router.post("/share")
async def log_share(req: ShareEventRequest):
    """
    PUBLIC. Fire-and-forget analytics.
    Input: { "session_id": str, "channel": str, "recipient_count"?: int }
    """

@router.get("/{language}/result/{session_id}/og-image")
async def og_image(language: str, session_id: str):
    """
    PUBLIC. Returns a dynamically generated OG image (PNG) for social sharing.
    Shows: Tauka logo, language, CEFR level, score card design.
    Cached by Cloudflare for 7 days per unique URL.
    """

# ADMIN ONLY — protected by admin role check
@router.post("/admin/seed-questions")
async def seed_questions(req: SeedQuestionsRequest, user_id: str = Depends(get_current_user)):
    """
    Admin-only. Triggers question bank generation for a language.
    """
```

**Register in `main.py`:**
```python
from app.routes import test
app.include_router(test.router, prefix="/test")
```

### C.5 Pydantic Models

```python
class Phase1SubmitRequest(BaseModel):
    session_id: str
    answers: dict[str, str]      # { question_uuid: selected_answer }

class EmailCaptureRequest(BaseModel):
    session_id: str
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr

class FinalSubmitRequest(BaseModel):
    session_id: str
    answers: dict[str, str]

class ShareEventRequest(BaseModel):
    session_id: str
    channel: Literal["whatsapp", "email", "copy_link"]
    recipient_count: int = 1

class SeedQuestionsRequest(BaseModel):
    language: str
    lesson_range: tuple[int, int] | None = None  # e.g. (1, 60)

class TestResultResponse(BaseModel):
    session_id: str
    cefr_result: str
    total_correct: int
    total_questions: int
    breakdown: dict
    description: str
    share_url: str
    referrer_student_id: str | None
```

---

## Segment D — Supporter Lifecycle & Milestone Notifications

**Purpose:** The supporter relationship from approval through ongoing milestone updates.

**Depends on:** Segment A (email service), Segment C (test_sessions table).

**Outputs:** `routes/supporters.py`, `services/supporter_service.py`, `services/milestone_service.py`, `tasks/scheduler.py`, `tasks/milestone_check.py`, `migrations/003_supporters.sql`, updated `main.py`, updated `models/schemas.py`.

### D.1 Database Migration (`migrations/003_supporters.sql`)

```sql
CREATE TABLE test_supporters (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_email     text NOT NULL,
    supporter_name      text NOT NULL,
    student_id          uuid NOT NULL REFERENCES auth.users(id),
    test_session_id     uuid NOT NULL REFERENCES test_sessions(id),
    status              text DEFAULT 'approved',
        -- 'approved','opted_in','opted_out','unsubscribed'
    opted_in_at         timestamptz,
    student_visible     boolean DEFAULT true,
    last_notified_at    timestamptz,
    milestone_email_count int DEFAULT 0,
    gift_nudge_shown    boolean DEFAULT false,
    gift_nudge_shown_at timestamptz,
    engagement_score    int DEFAULT 0,
    created_at          timestamptz DEFAULT now(),

    UNIQUE(supporter_email, student_id)
);

CREATE INDEX idx_supporters_student ON test_supporters(student_id);
CREATE INDEX idx_supporters_status ON test_supporters(status);

CREATE TABLE milestone_notifications (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id      uuid NOT NULL REFERENCES auth.users(id),
    supporter_id    uuid NOT NULL REFERENCES test_supporters(id),
    milestone_type  text NOT NULL,
        -- 'unit_complete','streak_14','first_ai_convo','first_cohort_session','cefr_level_up'
    milestone_data  jsonb NOT NULL,
    status          text DEFAULT 'pending',
        -- 'pending','sent','failed','skipped'
    sent_at         timestamptz,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX idx_milestones_pending ON milestone_notifications(status) WHERE status = 'pending';

-- ═══ RLS POLICIES ═══
-- test_supporters: Mobile app reads for the student's "My Supporters" settings page.
-- Writes go through FastAPI only (approve, opt-in, engagement tracking).
ALTER TABLE test_supporters ENABLE ROW LEVEL SECURITY;

-- Students can read supporters linked to them (for settings page)
CREATE POLICY "students_read_own_supporters"
    ON test_supporters FOR SELECT
    USING (student_id = auth.uid());

-- Students can update student_visible flag on their own supporters
CREATE POLICY "students_toggle_own_supporter_visibility"
    ON test_supporters FOR UPDATE
    USING (student_id = auth.uid())
    WITH CHECK (student_id = auth.uid());

-- Supporters have no Supabase auth — all their interactions go through
-- FastAPI public endpoints (service role). No anon SELECT policy needed.

-- milestone_notifications: Mobile app reads for student's milestone history view.
ALTER TABLE milestone_notifications ENABLE ROW LEVEL SECURITY;

-- Students can read their own milestone history
CREATE POLICY "students_read_own_milestones"
    ON milestone_notifications FOR SELECT
    USING (student_id = auth.uid());

-- All writes (inserting, updating status to 'sent') go through FastAPI cron (service role).
```

### D.2 Supporter Service (`app/services/supporter_service.py`)

```python
async def approve_for_student(
    session_id: str,
    note: str | None = None,
) -> dict:
    """
    Called when referred supporter taps 'approve' on result screen.
    1. Look up session's referrer_student_id.
    2. Create test_supporters record (status: 'approved').
    3. Send student notification email (background).
    4. Return { "supporter_id": str }.
    """

async def opt_in_supporter(supporter_id: str) -> dict:
    """
    Supporter agrees to follow student's progress.
    Updates status to 'opted_in', sets opted_in_at.
    """

async def opt_out_supporter(supporter_id: str) -> dict:
    """
    Supporter clicks 'stop updates' in email.
    Updates status to 'opted_out'.
    """

async def unsubscribe_supporter(supporter_id: str) -> dict:
    """
    Hard unsubscribe via email footer link.
    Updates status to 'unsubscribed'.
    """

async def track_engagement(supporter_id: str, event: str) -> None:
    """
    Increments engagement_score. Called when supporter opens an email,
    clicks a WhatsApp link, etc. Uses tracking pixels / redirect URLs.
    Events: 'email_open', 'whatsapp_click', 'email_click'.
    """

async def get_student_supporters(student_id: str) -> list[dict]:
    """
    Returns all supporters for a student (for settings page).
    Respects student_visible flag — returns all but marks hidden ones.
    """

async def toggle_supporter_visibility(
    student_id: str, supporter_id: str, visible: bool
) -> dict:
    """
    Student toggles whether to share milestones with a specific supporter.
    """
```

### D.3 Milestone Service (`app/services/milestone_service.py`)

```python
async def check_all_milestones() -> dict:
    """
    Called by the daily cron. Scans for new milestones across all students
    who have opted-in supporters.
    Returns { "milestones_found": int, "notifications_queued": int }
    """

async def detect_milestones_for_student(student_id: str) -> list[dict]:
    """
    Checks all milestone types for a specific student.
    Returns list of { "type": str, "data": dict } for new milestones.

    Detection queries:
    - unit_complete: lessons_completed crossed a unit boundary (every 10-12 lessons)
    - streak_14: current_streak >= 14 and not previously notified
    - first_ai_convo: ai_conversations count went from 0 to 1
    - first_cohort_session: cohort_sessions_attended went from 0 to 1
    - cefr_level_up: assessed_level changed since last check
    """

async def queue_notification(
    student_id: str,
    supporter_id: str,
    milestone_type: str,
    milestone_data: dict,
) -> str:
    """
    Inserts a milestone_notifications row (status: 'pending').
    Checks:
    - supporter is 'opted_in'
    - student has student_visible=true for this supporter
    - at least 14 days since last_notified_at
    Returns notification_id or None if skipped.
    """

async def process_pending_notifications() -> dict:
    """
    Sends all pending milestone notifications.
    For each:
    1. Render email (standard or gift-nudge variant based on conditions).
    2. Send via email_service.
    3. Update notification status to 'sent'.
    4. Update supporter's last_notified_at and milestone_email_count.
    Returns { "sent": int, "failed": int, "skipped": int }.
    """

def should_include_gift_nudge(supporter: dict, student: dict, milestone_type: str) -> bool:
    """
    Evaluates whether this milestone email should include the sponsorship option.
    All conditions must be true:
    - supporter.engagement_score >= 3
    - supporter.gift_nudge_shown is False OR gift_nudge_shown_at > 90 days ago
    - student.tier == 'free'
    - milestone_type in ('unit_complete', 'cefr_level_up')
    """
```

### D.4 Task Scheduler (`app/tasks/scheduler.py`)

```python
"""
Lightweight task scheduler using APScheduler.
Started alongside the FastAPI app via a lifespan event.

Alternative: if deploying on a platform with native cron (Railway, Fly.io),
use platform cron to hit internal endpoints instead of APScheduler.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

def start_scheduler():
    # Milestone check: daily at 06:00 UTC
    scheduler.add_job(
        "app.tasks.milestone_check:run_milestone_check",
        "cron", hour=6, minute=0,
        id="milestone_check",
        replace_existing=True,
    )
    # Gift lifecycle: daily at 07:00 UTC
    scheduler.add_job(
        "app.tasks.gift_lifecycle:run_gift_lifecycle",
        "cron", hour=7, minute=0,
        id="gift_lifecycle",
        replace_existing=True,
    )
    # Testimonial outreach: daily at 08:00 UTC
    scheduler.add_job(
        "app.tasks.testimonial_outreach:run_testimonial_outreach",
        "cron", hour=8, minute=0,
        id="testimonial_outreach",
        replace_existing=True,
    )
    scheduler.start()
```

**Register in `main.py` via lifespan:**
```python
from contextlib import asynccontextmanager
from app.tasks.scheduler import start_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield

app = FastAPI(lifespan=lifespan)
```

### D.5 Milestone Check Task (`app/tasks/milestone_check.py`)

```python
async def run_milestone_check():
    """
    Daily cron job.
    1. Call milestone_service.check_all_milestones() — finds new milestones.
    2. Call milestone_service.process_pending_notifications() — sends emails.
    3. Log results for monitoring.
    """
```

### D.6 Routes (`app/routes/supporters.py`)

```python
router = APIRouter()

@router.post("/approve")
async def approve(req: ApproveRequest, bg: BackgroundTasks):
    """
    PUBLIC. Supporter approves platform for student.
    Input: { "session_id": str, "note"?: str }
    Returns: { "supporter_id": str }
    """

@router.post("/opt-in")
async def opt_in(req: OptInRequest):
    """
    PUBLIC. Supporter agrees to milestone updates.
    Input: { "supporter_id": str }
    """

@router.get("/opt-out/{supporter_id}")
async def opt_out(supporter_id: str):
    """
    PUBLIC (clicked from email link). Stops milestone emails.
    Returns a simple HTML page confirming opt-out.
    """

@router.get("/unsubscribe/{supporter_id}")
async def unsubscribe(supporter_id: str):
    """
    PUBLIC (email footer). Hard unsubscribe.
    Returns a simple HTML page confirming unsubscribe.
    """

@router.get("/track/{supporter_id}/{event}")
async def track(supporter_id: str, event: str):
    """
    PUBLIC. Tracking pixel/redirect for engagement scoring.
    Increments engagement_score, then redirects (for link clicks)
    or returns 1x1 transparent GIF (for email open pixels).
    """

# AUTHENTICATED — student managing their supporters
@router.get("/my-supporters")
async def my_supporters(user_id: str = Depends(get_current_user)):
    """
    Returns list of supporters for the authenticated student.
    """

@router.patch("/visibility")
async def toggle_visibility(req: VisibilityToggleRequest, user_id: str = Depends(get_current_user)):
    """
    Student toggles milestone sharing for a specific supporter.
    Input: { "supporter_id": str, "visible": bool }
    """
```

**Register in `main.py`:**
```python
from app.routes import supporters
app.include_router(supporters.router, prefix="/supporters")
```

---

## Segment E — Sponsorship & Gift Subscriptions

**Purpose:** Supporters can gift students 1 or 3 months of Tutor tier access.

**Depends on:** Segment A (Stripe service, tier service, email service), Segment D (supporter records, engagement tracking).

**Outputs:** `routes/gifts.py`, `services/gift_service.py`, `tasks/gift_lifecycle.py`, `migrations/004_gifts.sql`, updated `main.py`, updated `models/schemas.py`.

### E.1 Database Migration (`migrations/004_gifts.sql`)

```sql
CREATE TABLE gift_subscriptions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_id        uuid NOT NULL REFERENCES test_supporters(id),
    student_id          uuid NOT NULL REFERENCES auth.users(id),
    stripe_payment_id   text NOT NULL,
    stripe_receipt_url  text,
    tier                text DEFAULT 'tutor',
    duration_months     int NOT NULL,           -- 1 or 3
    amount_cents        int NOT NULL,           -- 4000 or 12000
    currency            text DEFAULT 'usd',
    anonymous           boolean DEFAULT false,
    status              text DEFAULT 'pending',
        -- 'pending','active','expired','refunded'
    activated_at        timestamptz,
    expires_at          timestamptz,
    expiry_warned       boolean DEFAULT false,
    renewal_nudge_sent  boolean DEFAULT false,
    refunded_at         timestamptz,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX idx_gifts_student ON gift_subscriptions(student_id);
CREATE INDEX idx_gifts_status ON gift_subscriptions(status);
CREATE INDEX idx_gifts_expires ON gift_subscriptions(expires_at) WHERE status = 'active';
CREATE INDEX idx_gifts_stripe ON gift_subscriptions(stripe_payment_id);

-- ═══ RLS POLICIES ═══
-- Mobile app reads this table for the student's "gifted access" badge and expiry date.
-- All writes (creation, activation, expiry) go through FastAPI (service role).
ALTER TABLE gift_subscriptions ENABLE ROW LEVEL SECURITY;

-- Students can read gifts linked to their account
CREATE POLICY "students_read_own_gifts"
    ON gift_subscriptions FOR SELECT
    USING (student_id = auth.uid());

-- IMPORTANT: When anonymous=true, the mobile app query will return the gift row
-- but the Flutter/Next.js client must check the anonymous flag and display
-- "Angel supporter" instead of looking up the supporter's name.
-- The supporter_id FK is still present in the row — the anonymity is enforced
-- at the UI layer, not the database layer. This is acceptable because the student
-- would need to manually query test_supporters to resolve the name, and that table's
-- RLS only returns rows where student_id = auth.uid() (which it does — but the
-- supporter_name field is visible). If stricter anonymity is needed later, create
-- a database view that masks supporter_name when anonymous=true.
```

### E.2 Gift Service (`app/services/gift_service.py`)

```python
async def create_gift_checkout(
    supporter_id: str,
    duration_months: int,    # 1 or 3
    anonymous: bool,
) -> dict:
    """
    1. Validate supporter exists and is 'opted_in'.
    2. Look up associated student.
    3. Check no overlapping active gift with >30 days remaining.
    4. Create Stripe Checkout Session (one-time payment).
    5. Insert gift_subscriptions row (status: 'pending').
    6. Return { "checkout_url": str }.
    """

async def activate_gift(stripe_session_id: str) -> dict:
    """
    Called by Stripe webhook handler when payment completes.
    1. Find pending gift matching stripe_payment_id.
    2. Handle stacking: if student has active gift, extend rather than overlap.
    3. Set activated_at, expires_at. Status → 'active'.
    4. Call tier_service.change_tier(source='gifted', preserve_original=True).
    5. Notify student (named or anonymous based on gift.anonymous flag).
    6. Send supporter receipt email.
    7. Return activation details.
    """

async def process_gift_expiries() -> dict:
    """
    Called by daily cron.
    Step 1 — Expiry warnings (14 days before):
      Send student email. Set expiry_warned=true.
    Step 2 — Actual expiration:
      Set status='expired'. Downgrade student via tier_service.
    Step 3 — Impact summary (3 days after expiry):
      Compile student progress during gifted period.
      Send supporter email with progress card + renewal option.
      Set renewal_nudge_sent=true. Sent exactly once.
    Returns { "warnings_sent": int, "expired": int, "summaries_sent": int }
    """

async def request_refund(gift_id: str, supporter_email: str) -> dict:
    """
    1. Validate gift exists, email matches, status is 'active', within 7 days.
    2. Issue Stripe refund.
    3. Status → 'refunded'. Downgrade student.
    4. Notify student neutrally ("Your gifted access has ended").
    5. Return { "refund_id": str }.
    """

async def compile_gift_impact(gift_id: str) -> dict:
    """
    Compiles student progress during the gifted period.
    Returns {
        "lessons_completed": int,
        "sessions_attended": int,
        "best_streak": int,
        "level_progress": "A1 → A2" | "Solidifying A2",
        "topics_covered": ["greetings", "daily routines", ...],
        "duration_description": "3 months"
    }
    """
```

### E.3 Gift Lifecycle Task (`app/tasks/gift_lifecycle.py`)

```python
async def run_gift_lifecycle():
    """
    Daily cron job.
    1. Call gift_service.process_gift_expiries().
    2. Log results for monitoring.
    """
```

### E.4 Routes (`app/routes/gifts.py`)

```python
router = APIRouter()

@router.post("/checkout")
async def create_checkout(req: GiftCheckoutRequest):
    """
    PUBLIC (supporter has no account).
    Rate limited: 3 checkouts/supporter/hour.
    Input: { "supporter_id": str, "duration_months": 1|3, "anonymous": bool }
    Returns: { "checkout_url": str }
    """

@router.get("/success")
async def gift_success(session_id: str):
    """
    PUBLIC. Stripe redirects here after payment.
    Returns gift confirmation data for the success page.
    """

@router.post("/refund")
async def request_refund(req: GiftRefundRequest):
    """
    PUBLIC. Supporter requests refund within 7 days.
    Input: { "gift_id": str, "supporter_email": str }
    """

# Note: gift activation happens via the Stripe webhook in
# routes/subscriptions.py → stripe_service.process_webhook_event().
# The webhook handler checks metadata to distinguish self-pay from gift.
```

**Register in `main.py`:**
```python
from app.routes import gifts
app.include_router(gifts.router, prefix="/gifts")
```

### E.5 Stripe Webhook Update

In `app/services/stripe_service.py`, the `_handle_checkout_complete` function (from Segment A) must be updated to handle both subscription checkouts and gift checkouts:

```python
async def _handle_checkout_complete(session: dict):
    metadata = session.get("metadata", {})

    if "supporter_id" in metadata:
        # This is a gift payment
        await gift_service.activate_gift(session["id"])
    elif "tier" in metadata:
        # This is a self-pay subscription
        await _activate_subscription(session)
    else:
        logger.warning(f"Unknown checkout session: {session['id']}")
```

---

## Segment F — Testimonial Collection

**Purpose:** Collect endorsement quotes from supporters who gifted.

**Depends on:** Segment D (supporter records), Segment E (gift_subscriptions for trigger conditions).

**Outputs:** `routes/testimonials.py`, `services/testimonial_service.py`, `tasks/testimonial_outreach.py`, `migrations/005_testimonials.sql`, updated `main.py`, updated `models/schemas.py`.

### F.1 Database Migration (`migrations/005_testimonials.sql`)

```sql
CREATE TABLE testimonial_requests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supporter_id        uuid NOT NULL REFERENCES test_supporters(id),
    gift_id             uuid NOT NULL REFERENCES gift_subscriptions(id),
    status              text DEFAULT 'pending',
        -- 'pending','sent','submitted','approved','declined','published'
    sent_at             timestamptz,
    quote_text          text,
    display_name        text,
    display_preference  text,          -- 'full_name','first_name_initial','anonymous'
    approved_by_admin   boolean DEFAULT false,
    published_at        timestamptz,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX idx_testimonials_status ON testimonial_requests(status);
CREATE INDEX idx_testimonials_published ON testimonial_requests(status)
    WHERE status = 'published';

-- ═══ RLS POLICIES ═══
-- Marketing site (Next.js with anon key) reads published testimonials for display.
-- All other operations go through FastAPI (service role).
ALTER TABLE testimonial_requests ENABLE ROW LEVEL SECURITY;

-- Public can read published testimonials (for marketing site components)
CREATE POLICY "public_read_published_testimonials"
    ON testimonial_requests FOR SELECT
    USING (status = 'published');

-- No other client-side access. Submission, approval, and publishing
-- all go through FastAPI endpoints with service role.
```

### F.2 Testimonial Service (`app/services/testimonial_service.py`)

```python
async def check_testimonial_eligibility() -> list[dict]:
    """
    Called by daily cron.
    Finds expired gifts (7+ days ago) where:
    - Student completed ≥1 unit OR attended ≥2 sessions during gifted period
    - No existing testimonial_request for this gift
    - Supporter is still 'opted_in'
    Creates 'pending' testimonial_request rows.
    Returns list of created requests.
    """

async def send_pending_requests() -> dict:
    """
    Sends testimonial request emails for 'pending' rows.
    Sets status to 'sent', records sent_at.
    Returns { "sent": int }
    """

async def submit_testimonial(
    request_id: str,
    quote_text: str,
    display_name: str,
    display_preference: str,
) -> dict:
    """
    1. Validate request exists and status is 'sent'.
    2. Validate quote_text length (20-500 chars).
    3. Store testimonial. Status → 'submitted'.
    4. Notify admin (email/Slack).
    5. Return confirmation.
    """

async def decline_testimonial(request_id: str) -> dict:
    """
    Supporter clicked "No thanks". Status → 'declined'.
    """

async def auto_expire_unanswered() -> int:
    """
    Called by daily cron. Sets 'sent' requests older than 14 days to 'declined'.
    Returns count of expired requests.
    """

async def get_published_testimonials(language: str | None = None) -> list[dict]:
    """
    PUBLIC. Returns published testimonials for the marketing site.
    Optionally filtered by language.
    Returns list of { "quote": str, "display_name": str, "language": str }.
    """
```

### F.3 Testimonial Outreach Task (`app/tasks/testimonial_outreach.py`)

```python
async def run_testimonial_outreach():
    """
    Daily cron job.
    1. Call testimonial_service.check_testimonial_eligibility() — creates new requests.
    2. Call testimonial_service.send_pending_requests() — sends emails.
    3. Call testimonial_service.auto_expire_unanswered() — cleans up old requests.
    4. Log results.
    """
```

### F.4 Routes (`app/routes/testimonials.py`)

```python
router = APIRouter()

@router.post("/submit")
async def submit(req: TestimonialSubmitRequest):
    """
    PUBLIC (link from email).
    Input: { "request_id": str, "quote_text": str, "display_name": str,
             "display_preference": "full_name"|"first_name_initial"|"anonymous" }
    """

@router.get("/decline/{request_id}")
async def decline(request_id: str):
    """
    PUBLIC (link from email). Sets status to 'declined'.
    Returns simple HTML confirmation page.
    """

@router.get("/published")
async def published(language: str | None = None):
    """
    PUBLIC. Returns published testimonials for the marketing site.
    """

# ADMIN
@router.post("/admin/approve/{request_id}")
async def admin_approve(request_id: str, user_id: str = Depends(get_current_user)):
    """
    Admin approves a submitted testimonial for publishing.
    """

@router.post("/admin/publish/{request_id}")
async def admin_publish(request_id: str, user_id: str = Depends(get_current_user)):
    """
    Admin publishes an approved testimonial.
    """
```

**Register in `main.py`:**
```python
from app.routes import testimonials
app.include_router(testimonials.router, prefix="/testimonials")
```

---

## Final `main.py` (After All Segments)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.routes import ai, messaging, calling, subscriptions, test, supporters, gifts, testimonials
from app.tasks.scheduler import start_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield

app = FastAPI(lifespan=lifespan)

# Existing
app.include_router(ai.router, prefix="/ai")
app.include_router(messaging.router, prefix="/messages")
app.include_router(calling.router, prefix="/calls")

# Segment A
app.include_router(subscriptions.router, prefix="/subscriptions")

# Segment C
app.include_router(test.router, prefix="/test")

# Segment D
app.include_router(supporters.router, prefix="/supporters")

# Segment E
app.include_router(gifts.router, prefix="/gifts")

# Segment F
app.include_router(testimonials.router, prefix="/testimonials")
```

---

## Appendix: Environment Variables

```env
# Supabase
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
SUPABASE_ANON_KEY=eyJ...

# Stripe
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_LEARNER_MONTHLY=price_...
STRIPE_PRICE_TUTOR_MONTHLY=price_...
STRIPE_PRICE_INTENSIVE_MONTHLY=price_...
STRIPE_PRICE_GIFT_1MO=price_...
STRIPE_PRICE_GIFT_3MO=price_...

# Email
EMAIL_PROVIDER=resend
EMAIL_API_KEY=re_...
EMAIL_FROM_ADDRESS=Tauka <hello@tauka.com>

# App
WEB_BASE_URL=https://www.tauka.com
APP_BASE_URL=https://app.tauka.com

# External APIs (existing)
DEEPSEEK_API_KEY=...
OPENAI_API_KEY=...
DAILY_API_KEY=...
```

---

## Appendix: Execution Checklist

| Segment | Estimated Effort | Prerequisite | Deliverables |
|---|---|---|---|
| **A** — Subscriptions & Foundations | 1–2 sessions | Existing project | config, stripe_service, email_service, tier_service, subscriptions route, migration 001 |
| **B** — Cloudflare Edge | 1 session (infra) | Segment A | Cloudflare dashboard rules (not code) |
| **C** — Test System | 2–3 sessions | Segment A | test route, test_service, question_bank_service, migration 002 |
| **D** — Supporters & Milestones | 1–2 sessions | A + C | supporters route, supporter_service, milestone_service, scheduler, migration 003 |
| **E** — Sponsorship & Gifts | 1–2 sessions | A + D | gifts route, gift_service, gift_lifecycle task, migration 004, updated webhook handler |
| **F** — Testimonials | 1 session | D + E | testimonials route, testimonial_service, outreach task, migration 005 |

**Total: 7–11 Claude Code sessions, executed in order.**