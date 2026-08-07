# Partial FastAPI Spec — Referral & Share System

> **Context for Claude Code:** This is a partial addition to the existing Tauka FastAPI backend spec (`tauka_fastapi_spec.md`). It adds the referral creation and share infrastructure that feeds into Segment C (test system) and Segment D (supporters). Insert this as **Segment C.5** (between the test routes and the supporter lifecycle), or as a new sub-section within Segment C. It introduces one new table, one new service, two new route endpoints, and modifications to two existing endpoints.

---

## Database Migration (add to `migrations/002_test_questions.sql` or create `migrations/002b_referrals.sql`)

```sql
CREATE TABLE test_referrals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    referral_code   text UNIQUE NOT NULL,       -- short URL-safe code, e.g. 'xK7mQ2'
    
    -- Who sent the referral
    sender_type     text NOT NULL,              -- 'visitor' (unauthenticated) or 'student' (authenticated)
    sender_name     text,                       -- captured from form (visitor) or profile (student)
    sender_email    text,                       -- captured from form (visitor) or profile (student)
    sender_student_id uuid,                     -- FK → auth.users(id), set only if sender_type='student'
    
    -- What the sender intended
    intent          text,                       -- 'validate', 'peer', or null (intentless)
    
    -- How it was sent
    channel         text,                       -- 'whatsapp', 'email', 'copy_link'
    recipient_email text,                       -- only set if sent via email
    
    -- Tracking
    language        text NOT NULL,              -- 'amharic', 'french', etc.
    link_opened     boolean DEFAULT false,      -- recipient clicked the link
    link_opened_at  timestamptz,
    test_started    boolean DEFAULT false,      -- recipient started the test
    test_session_id uuid,                       -- FK → test_sessions(id), set when recipient starts test
    test_completed  boolean DEFAULT false,      -- recipient finished the test
    approved        boolean DEFAULT false,      -- recipient approved (validate intent only)
    
    -- Notification tracking
    sender_notified_on_completion boolean DEFAULT false, -- "your contact took the test"
    sender_notified_on_approval   boolean DEFAULT false, -- "your contact approved Tauka"
    
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX idx_referrals_code ON test_referrals(referral_code);
CREATE INDEX idx_referrals_sender_email ON test_referrals(sender_email);
CREATE INDEX idx_referrals_sender_student ON test_referrals(sender_student_id);

-- ═══ RLS POLICIES ═══
-- Authenticated students can read their own sent referrals (for in-app "Invites" view).
-- Visitors' referrals are only accessed through FastAPI (service role).
ALTER TABLE test_referrals ENABLE ROW LEVEL SECURITY;

-- Students see referrals they sent
CREATE POLICY "students_read_own_referrals"
    ON test_referrals FOR SELECT
    USING (sender_student_id = auth.uid());

-- All writes go through FastAPI (service role)
```

## Referral Service (`app/services/referral_service.py`)

```python
import secrets
import string

def generate_referral_code(length: int = 8) -> str:
    """
    Generates a short, URL-safe referral code.
    Uses alphanumeric characters, avoids ambiguous chars (0/O, 1/l).
    Example output: 'xK7mQ2bR'
    """
    alphabet = string.ascii_letters + string.digits
    alphabet = alphabet.translate(str.maketrans('', '', '0O1lI'))
    return ''.join(secrets.choice(alphabet) for _ in range(length))


async def create_referral(
    language: str,
    intent: str | None,            # 'validate', 'peer', or None
    channel: str,                   # 'whatsapp', 'email', 'copy_link'
    sender_type: str,               # 'visitor' or 'student'
    sender_name: str | None = None,
    sender_email: str | None = None,
    sender_student_id: str | None = None,
    recipient_email: str | None = None,
) -> dict:
    """
    Creates a referral record and returns the share link.
    
    1. Generate unique referral_code.
    2. Insert test_referrals row.
    3. Build the share URL:
       - Base: {WEB_BASE_URL}/test/{language}
       - Params: ?ref={referral_code}
       - If intent is set: &intent={intent}
    4. If channel is 'email', send the referral email (background task):
       - Use the appropriate pre-filled message based on intent.
       - From: "[Sender name] via Tauka" or just "Tauka" if name unavailable.
    5. Return { "referral_code": str, "share_url": str, "referral_id": str }
    """


async def record_link_opened(referral_code: str) -> None:
    """
    Called when the test page loads with a ref param.
    Sets link_opened=true, link_opened_at=now() if not already set.
    Fire-and-forget — never blocks the test page render.
    """


async def connect_test_session(referral_code: str, test_session_id: str) -> None:
    """
    Called when a test session is created with a ref param.
    Links the referral to the test session. Sets test_started=true.
    Also copies the referral's sender_student_id to the test session's
    referrer_student_id field (if sender was an authenticated student).
    """


async def handle_test_completion(referral_code: str) -> None:
    """
    Called when a referred test-taker completes the test.
    Sets test_completed=true.
    
    Sends the sender a notification:
    - If sender_type='visitor': email to sender_email.
      For validate intent: "[Recipient] completed the Amharic assessment.
        See their result and sign up to start learning: [signup link]"
      For peer intent: "[Recipient] took the Amharic test too!
        See how you compare — take the test yourself: [test link]"
    - If sender_type='student': in-app notification + email.
      For validate intent: "[Recipient] took the assessment on your behalf.
        Check your Supporters page to see if they approved."
      For peer intent: "[Recipient] took the Amharic test!
        They scored [level]. [optional: playful comparison prompt]"
    
    Sets sender_notified_on_completion=true.
    """


async def handle_approval(referral_code: str) -> None:
    """
    Called when a referred test-taker (validate intent) approves the platform.
    Sets approved=true.
    
    Sends the sender a notification:
    - If sender_type='visitor': email — "[Supporter name] recommends Tauka
        for your Amharic learning. Ready to start? [signup link with ref]"
        This email is Tauka's first warm contact with the visitor — it
        doubles as a high-intent conversion prompt.
    - If sender_type='student': in-app notification + email —
        "[Supporter name] approved Tauka for you! They'll appear in your
        Supporters list."
    
    Sets sender_notified_on_approval=true.
    """


async def get_referral_by_code(referral_code: str) -> dict | None:
    """
    Looks up a referral by code. Returns the referral record
    including sender_name (for display on the test landing page)
    and intent (for landing variant selection).
    
    Returns None if the code doesn't exist — the test page
    falls back to the intentless variant.
    """


async def get_sent_referrals(student_id: str) -> list[dict]:
    """
    Returns all referrals sent by an authenticated student.
    Used for the in-app "Invites" view.
    Returns list of {
        referral_code, language, intent, channel,
        recipient_email (if email channel),
        link_opened, test_started, test_completed, approved,
        created_at
    }
    """
```

## Pre-Filled Message Templates

Store these in `app/templates/messages/` or as constants in the referral service. They're used for WhatsApp `wa.me` URLs and email bodies.

```python
SHARE_MESSAGES = {
    "validate": {
        "whatsapp": (
            "I'm thinking about learning {language} on Tauka. "
            "You actually speak it — can you try their 10-minute test "
            "and let me know if it's legit? {link}"
        ),
        "email_subject": "{sender_name} wants your opinion on an {language} learning platform",
        "email_body": (
            "Hey,\n\n"
            "I'm considering learning {language} on a platform called Tauka. "
            "Since you actually speak it, I'd really value your opinion. "
            "They have a 10-minute assessment — could you give it a try "
            "and tell me what you think?\n\n"
            "{link}\n\n"
            "Thanks!\n{sender_name}"
        ),
    },
    "peer": {
        "whatsapp": (
            "Found this new site that teaches {language}, "
            "there's even a test to gauge your level. "
            "I'm intrigued, considering learning it. "
            "You should definitely check it out {link}"
        ),
        "email_subject": "Check out this {language} learning platform",
        "email_body": (
            "Hey,\n\n"
            "I found this site called Tauka that teaches {language}. "
            "They have a free test to gauge your level — it's actually "
            "pretty interesting. I'm thinking about learning, figured "
            "you might be into it too.\n\n"
            "{link}\n\n"
            "{sender_name}"
        ),
    },
    # Fallback if intent is None (shouldn't happen in normal flow,
    # but defensive)
    "default": {
        "whatsapp": (
            "Check out this {language} test on Tauka — "
            "it's a quick way to see where you stand. {link}"
        ),
        "email_subject": "Try this {language} assessment",
        "email_body": (
            "Hey,\n\n"
            "Thought you might find this interesting — "
            "a 10-minute {language} assessment on Tauka.\n\n"
            "{link}\n\n"
            "{sender_name}"
        ),
    },
}
```

## Pydantic Models (add to `app/models/schemas.py`)

```python
class CreateReferralRequest(BaseModel):
    language: str
    intent: Literal["validate", "peer"] | None = None
    channel: Literal["whatsapp", "email", "copy_link"]
    # Required for unauthenticated visitors, ignored for authenticated users
    sender_name: str | None = Field(None, min_length=1, max_length=100)
    sender_email: EmailStr | None = None
    # Only for email channel
    recipient_email: EmailStr | None = None

class CreateReferralResponse(BaseModel):
    referral_id: str
    referral_code: str
    share_url: str
    # For WhatsApp: the full wa.me URL with pre-filled text
    whatsapp_url: str | None = None

class ReferralLookupResponse(BaseModel):
    sender_name: str | None
    intent: str | None       # 'validate', 'peer', or None
    language: str
    valid: bool              # false if code not found

class SentReferralItem(BaseModel):
    referral_code: str
    language: str
    intent: str | None
    channel: str
    recipient_email: str | None
    link_opened: bool
    test_started: bool
    test_completed: bool
    approved: bool
    created_at: str
```

## Routes (add to `app/routes/test.py` or create `app/routes/referrals.py`)

```python
# ── PUBLIC (unauthenticated visitors on marketing site) ──

@router.post("/referrals/create")
async def create_referral(req: CreateReferralRequest, request: Request, bg: BackgroundTasks):
    """
    PUBLIC. Creates a referral from an unauthenticated visitor.
    
    Requires sender_name and sender_email (since the visitor has no account).
    Rate limited: 10 referrals per IP per hour.
    
    Returns { referral_code, share_url, whatsapp_url (if whatsapp channel) }
    
    If channel is 'email' and recipient_email is provided,
    sends the referral email as a background task.
    If channel is 'whatsapp', returns the wa.me deep link URL
    with the pre-filled message — the frontend opens this URL.
    If channel is 'copy_link', just returns the share_url.
    """


# ── AUTHENTICATED (students in the app) ──

@router.post("/referrals/create-authenticated")
async def create_referral_authenticated(
    req: CreateReferralRequest,
    bg: BackgroundTasks,
    user_id: str = Depends(get_current_user),
):
    """
    AUTHENTICATED. Creates a referral from a logged-in student.
    
    sender_name and sender_email are pulled from the student's profile.
    sender_student_id is set from the JWT.
    Otherwise identical to the public endpoint.
    """


@router.get("/referrals/mine")
async def my_referrals(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Returns all referrals sent by this student.
    Used for the in-app "Invites" tracking view.
    """


# ── PUBLIC (recipient landing) ──

@router.get("/referrals/lookup/{referral_code}")
async def lookup_referral(referral_code: str):
    """
    PUBLIC. Called by the test page frontend on load when a ref param is present.
    Returns { sender_name, intent, language, valid }.
    
    Also fires record_link_opened() as a background task.
    
    If the code is invalid, returns { valid: false } — the frontend
    falls back to the intentless landing variant.
    """
```

## Modifications to Existing Endpoints

### In `POST /test/{language}/start` (Segment C)

Update to accept and process the referral code:

```python
@router.post("/{language}/start")
async def start_test(language: str, request: Request, bg: BackgroundTasks):
    """
    Existing endpoint — add referral linking.
    
    If req body contains 'referral_code':
    1. Look up the referral.
    2. If valid and intent='validate', set the test session's
       referrer_student_id from the referral's sender_student_id.
    3. Call referral_service.connect_test_session() to link
       the referral to this test session.
    
    The referrer_student_id is what later enables the approval flow
    on the result screen and the supporter relationship creation.
    """
```

### In `POST /test/submit-final` (Segment C)

Update to trigger referral notifications on completion:

```python
@router.post("/submit-final")
async def submit_final(req: FinalSubmitRequest, bg: BackgroundTasks):
    """
    Existing endpoint — add referral completion handling.
    
    After scoring and storing the result:
    1. If the session has a referral_code, call
       referral_service.handle_test_completion() as a background task.
       This notifies the sender that their contact took the test.
    """
```

### In `POST /supporters/approve` (Segment D)

Update to trigger referral approval notifications:

```python
@router.post("/approve")
async def approve(req: ApproveRequest, bg: BackgroundTasks):
    """
    Existing endpoint — add referral approval handling.
    
    After creating the test_supporters record:
    1. Look up the test session's referral_code (via test_sessions).
    2. If a referral exists, call referral_service.handle_approval()
       as a background task. This notifies the sender that their
       contact approved Tauka.
    """
```

## WhatsApp URL Construction

The `wa.me` deep link format:

```python
import urllib.parse

def build_whatsapp_url(message: str) -> str:
    """
    Builds a wa.me URL with pre-filled text.
    No phone number — opens WhatsApp's contact picker.
    """
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/?text={encoded}"
```

This URL opens WhatsApp on the sender's device with the message pre-filled. The sender picks the recipient from their contacts and taps send. Zero API cost, no WhatsApp Business account needed.

## Email Sending (via existing email_service)

For the email channel, the referral service calls `email_service.send_template_email()` with:
- Template: `referral_validate.html` or `referral_peer.html`
- From: "{sender_name} via Tauka <hello@tauka.com>" (uses the sender's name but Tauka's email address — the sender doesn't give their own email as the from address, they give it for reply-to and for the lead capture)
- Reply-To: sender_email (so if the recipient replies, it goes to the sender)
- To: recipient_email
- Data: { sender_name, language, share_url, language_display_name }

## Register in `main.py`

If creating a separate `referrals.py` router:

```python
from app.routes import referrals
app.include_router(referrals.router, prefix="/referrals")
```

Or if adding to the existing test router, the endpoints are already prefixed under `/test`.

---

## Account Portal & Tutor Portal — API Endpoints

> The website account portal uses a mixed architecture. Simple reads (profile, supporters list, subscription status display) go directly from the Next.js frontend to Supabase via `supabase-js` using the user's JWT — same pattern as the mobile app, same RLS policies. Operations involving Stripe, business logic, or sensitive writes go through FastAPI. This section specifies only the FastAPI endpoints — the direct Supabase reads don't need API routes.

### Architecture Decision: What Goes Through FastAPI vs Direct Supabase

| Operation | Path | Why |
|---|---|---|
| Read student profile | Website → Supabase | Simple row read, RLS handles auth |
| Read supporters list | Website → Supabase | Same query as mobile app, RLS handles auth |
| Read active gift status | Website → Supabase | Same query as mobile app |
| Read lesson/streak stats | Website → Supabase | Same data the app displays |
| Toggle supporter visibility | Website → FastAPI | Existing endpoint, validation logic |
| Read billing history | Website → FastAPI → Stripe | Requires Stripe secret key |
| Read payment method | Website → FastAPI → Stripe | Requires Stripe secret key |
| Change subscription plan | Website → FastAPI → Stripe | Creates Stripe Checkout or updates subscription |
| Cancel subscription | Website → FastAPI → Stripe | Cancels Stripe subscription at period end |
| Reactivate subscription | Website → FastAPI → Stripe | Reactivates cancelled Stripe subscription |
| Update payout settings (tutor) | Website → FastAPI | Sensitive financial data, validation needed |
| Read tutor earnings | Website → FastAPI | Aggregation query + payout logic |
| Update tutor availability | Website → Supabase | Simple row writes, RLS handles auth |
| Read tutor's assigned students | Website → Supabase | Read via RLS on the assignment table |

### New Route File: `app/routes/account.py`

```python
router = APIRouter()

# ── SUBSCRIPTION MANAGEMENT ──

@router.get("/subscription")
async def get_subscription(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Returns full subscription state for the portal.
    
    Reads student_profiles from Supabase (tier, source, gift status).
    Calls Stripe API to get:
    - Current subscription details (plan, status, current_period_end)
    - Payment method on file (brand, last4, exp_month, exp_year)
    - Upcoming invoice (next amount, next billing date)
    
    If tier_source is 'gifted', includes gift details
    (gifted_by name or 'angel supporter', expires_at).
    
    Returns {
        tier, tier_source, subscription_status,
        stripe: {
            plan_name, price_monthly, current_period_end,
            payment_method: { brand, last4, exp_month, exp_year } | null,
            upcoming_invoice: { amount, date } | null,
        },
        gift: { gifted_by, anonymous, expires_at } | null
    }
    """


@router.post("/subscription/change")
async def change_subscription(req: ChangeSubscriptionRequest, user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Changes the student's subscription plan.
    
    Cases:
    - Free → Paid: Creates a Stripe Checkout Session (same flow as
      the existing /subscriptions/checkout endpoint). Returns checkout_url.
    - Paid → Different Paid: Updates the Stripe Subscription's price.
      Prorates automatically. Returns { "effective_date": str, "new_tier": str }.
    - Paid → Free: Cancels at period end (calls /subscription/cancel internally).
    
    If the student has an active gift, plan changes apply after the gift expires.
    Don't allow changing to a lower tier than the gifted tier while the gift is active.
    """


@router.post("/subscription/cancel")
async def cancel_subscription(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Cancels subscription at end of current billing period.
    
    Calls Stripe to set cancel_at_period_end = true.
    Updates student_profiles.subscription_status to 'cancelled'.
    The actual tier downgrade happens when Stripe fires the
    customer.subscription.deleted webhook at period end.
    
    Returns { "cancels_at": str, "access_until": str }
    """


@router.post("/subscription/reactivate")
async def reactivate_subscription(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Reactivates a subscription that was cancelled
    but hasn't reached period end yet.
    
    Calls Stripe to set cancel_at_period_end = false.
    Updates student_profiles.subscription_status to 'active'.
    
    Returns { "status": "active", "next_billing": str }
    """


@router.get("/billing-history")
async def billing_history(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Returns the student's invoice history from Stripe.
    
    Calls Stripe List Invoices with the student's stripe_customer_id.
    Returns list of {
        invoice_id, date, amount, currency, status,
        description, pdf_url (Stripe-hosted invoice PDF)
    }
    
    Paginated: accepts ?limit=10&starting_after=inv_xxx
    """


@router.get("/payment-method")
async def get_payment_method(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Returns the student's payment method details.
    Returns { brand, last4, exp_month, exp_year } or null if no method on file.
    """


@router.post("/update-payment-method")
async def update_payment_method(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED. Creates a Stripe Billing Portal session for the student
    to update their payment method. The portal is Stripe-hosted — Tauka
    never handles card data.
    
    Returns { "portal_url": str } — redirect the student there.
    """
```

### New Route File: `app/routes/tutor_portal.py`

```python
router = APIRouter()

# ── EARNINGS & PAYOUTS ──

@router.get("/earnings")
async def get_earnings(
    user_id: str = Depends(get_current_user),
    period: str = Query(default="current_month"),  # 'current_month', 'last_month', 'all'
):
    """
    AUTHENTICATED (tutor role only).
    
    Aggregates session data for the tutor:
    - Sessions completed in the period
    - Gross earnings (sessions × per-session rate)
    - Net earnings (after any platform fee)
    - Payout status for the period
    
    Returns {
        period, sessions_completed, gross_earnings, net_earnings,
        per_session_rate, payout_status, payout_date,
        breakdown: [{ session_id, date, duration, students, type, amount }]
    }
    """


@router.get("/earnings/history")
async def earnings_history(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED (tutor role only).
    
    Returns monthly earnings history.
    List of { month, year, sessions, gross, net, payout_status, payout_date }
    
    Paginated: accepts ?limit=12&offset=0
    """


@router.get("/payout-settings")
async def get_payout_settings(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED (tutor role only).
    Returns { method: 'bank' | 'mpesa', details: { ... } }
    Bank: { bank_name, account_last4, routing_last4 }
    M-Pesa: { phone_last4 }
    Masked for security — only last 4 digits shown.
    """


@router.post("/payout-settings")
async def update_payout_settings(
    req: UpdatePayoutRequest,
    user_id: str = Depends(get_current_user),
):
    """
    AUTHENTICATED (tutor role only).
    Updates payout method. Validates bank details or M-Pesa number.
    Sensitive data — encrypted at rest in Supabase, accessed only
    through this service (never via direct Supabase client reads).
    """


# ── STUDENTS ──

@router.get("/students")
async def get_assigned_students(user_id: str = Depends(get_current_user)):
    """
    AUTHENTICATED (tutor role only).
    
    Returns students assigned to this tutor.
    List of {
        student_id, name, language, current_level,
        lessons_completed, last_session_date, attendance_rate
    }
    
    Note: This could also be a direct Supabase read via RLS,
    but aggregating attendance_rate and last_session_date requires
    a join across multiple tables, so FastAPI handles it.
    """


@router.get("/students/{student_id}")
async def get_student_detail(
    student_id: str,
    user_id: str = Depends(get_current_user),
):
    """
    AUTHENTICATED (tutor role only).
    
    Returns detailed profile for one assigned student.
    {
        name, language, goal, current_level,
        lessons_completed, total_lessons,
        session_history: [{ date, duration, type, topic }],
        progress_over_time: [{ date, level }]
    }
    
    Validates that student_id is actually assigned to this tutor.
    """
```

### Pydantic Models (add to `app/models/schemas.py`)

```python
class ChangeSubscriptionRequest(BaseModel):
    new_tier: Literal["free", "learner", "tutor", "intensive"]
    success_url: str | None = None  # for Stripe Checkout redirect (Free → Paid)
    cancel_url: str | None = None

class UpdatePayoutRequest(BaseModel):
    method: Literal["bank", "mpesa"]
    # Bank fields (required if method='bank')
    bank_name: str | None = None
    account_number: str | None = None
    routing_number: str | None = None
    account_holder_name: str | None = None
    # M-Pesa fields (required if method='mpesa')
    mpesa_phone: str | None = None
    mpesa_name: str | None = None
```

### Role-Based Access Control

The tutor portal endpoints need a role check beyond `get_current_user`. Add a dependency:

```python
# app/dependencies.py

async def require_tutor(user_id: str = Depends(get_current_user)) -> str:
    """
    Validates that the authenticated user has the 'tutor' role.
    Raises 403 if not.
    """
    profile = db.table("student_profiles").select("tier").eq("id", user_id).single().execute()
    # Or check against a roles table / JWT claim, depending on how roles are stored
    if not is_tutor(user_id):
        raise HTTPException(status_code=403, detail="Tutor access required")
    return user_id


async def require_admin(user_id: str = Depends(get_current_user)) -> str:
    """
    Same pattern for admin routes (future).
    """
    if not is_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id
```

Tutor portal routes use `user_id: str = Depends(require_tutor)` instead of `Depends(get_current_user)`.

### Register in `main.py`

```python
from app.routes import account, tutor_portal

app.include_router(account.router, prefix="/account")
app.include_router(tutor_portal.router, prefix="/tutor")
```

### Database Considerations

**Tutor availability** — needs a table if one doesn't exist:

```sql
CREATE TABLE IF NOT EXISTS tutor_availability (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id    uuid NOT NULL REFERENCES auth.users(id),
    day_of_week int NOT NULL,         -- 0=Monday, 6=Sunday
    start_time  time NOT NULL,
    end_time    time NOT NULL,
    timezone    text NOT NULL DEFAULT 'UTC',
    active      boolean DEFAULT true,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now(),
    
    UNIQUE(tutor_id, day_of_week, start_time)
);

ALTER TABLE tutor_availability ENABLE ROW LEVEL SECURITY;

-- Tutors can read and manage their own availability
CREATE POLICY "tutors_manage_own_availability"
    ON tutor_availability FOR ALL
    USING (tutor_id = auth.uid());

-- Students can read tutor availability (for scheduling)
CREATE POLICY "students_read_tutor_availability"
    ON tutor_availability FOR SELECT
    USING (active = true);
```

**Tutor payout settings** — sensitive, encrypted:

```sql
CREATE TABLE IF NOT EXISTS tutor_payout_settings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tutor_id        uuid UNIQUE NOT NULL REFERENCES auth.users(id),
    method          text NOT NULL,        -- 'bank', 'mpesa'
    encrypted_details text NOT NULL,      -- encrypted JSON blob
    details_last4   text,                 -- last 4 digits for display (unencrypted)
    verified        boolean DEFAULT false,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

ALTER TABLE tutor_payout_settings ENABLE ROW LEVEL SECURITY;

-- Tutors can read their own (masked) settings
-- But NOT the encrypted_details column — that's only accessed via FastAPI
CREATE POLICY "tutors_read_own_payout_display"
    ON tutor_payout_settings FOR SELECT
    USING (tutor_id = auth.uid());

-- All writes go through FastAPI (service role) for encryption handling
```

**Important:** The `encrypted_details` column contains full bank account numbers or M-Pesa phone numbers, encrypted at rest using a server-side key (stored in environment variables, never in the database). Only FastAPI can decrypt this — the mobile app and website never see raw payout details. The `details_last4` column is the only unencrypted identifier, used for display purposes ("Account ending in 4521").