# Domain: Test & Referral

> **Owner project:** tauka-python (primary writer); tauka-flutter (reads test results); tauka-react-web (test UI, supporter pages, gift checkout)
> **Last updated by:** tauka-react-web on 2026-05-26
> **Spec sections:** §10 Placement Test, §11 Referral & Supporter System, §14 Gift Subscriptions; partial_simplify_share.md (email channel removed); partial_verification_strategy.md (OTP scoped to test gate only)

---

## 1. Business Context

### What real-world problem does this domain solve?
New learners take a placement test to determine their CEFR level before enrolling. The test is shareable — students invite friends and supporters to witness their progress. Supporters who follow a student's journey can gift a subscription upgrade. This domain covers the entire funnel: test questions → test sessions → share events → referrals → supporters → milestone notifications → gift subscriptions → testimonials.

### How does this domain fit into the larger system?
`test_questions` is the question bank for the placement test (AI-generated, human-reviewed). `test_sessions` records each test attempt (including anonymous pre-signup attempts). `test_referrals` tracks invite links; `test_supporters` records people who opted-in to follow a student. `milestone_notifications` queues messages to supporters when students hit milestones. `gift_subscriptions` records when a supporter upgrades a student's tier — linked to `app.student.active_gift_id`. `testimonial_requests` captures post-gift supporter testimonials for marketing.

---

## 2. Design Decisions

### Key invariants
- `test_sessions` can exist for **anonymous** users (no `referrer_student_id`) — the test is public.
- `test_referrals.referral_code` is UNIQUE — codes are cryptographically generated, never reused.
- `test_supporters` has UNIQUE(supporter_email, student_id) — a supporter email can only opt-in once per student.
- `gift_subscriptions.tier` uses the canonical value `'tutor'` (not `'tutor_tier'`) — see CONFLICT-003.
- `app.student.active_gift_id` FK → `app.gift_subscriptions(id)` is a deferred FK added after §14 (table creation order constraint).
- `testimonial_requests` are publicly readable when `status = 'published'` — no auth required.
- **Share channels are exactly `'whatsapp'` and `'copy_link'` only.** The `'email'` channel was removed per `partial_simplify_share.md`. `test_referrals.recipient_email` is retained in the schema (nullable) but is no longer written.
- **OTP verification is scoped to the mid-test email gate (`purpose = 'test_gate'`) only.** The share/invite component and the post-test share component do NOT verify email — they use browser autofill. The `email_verifications` table `purpose` column accepts only `'test_gate'`; `'share_invite'` must never be used.

### Data flow
1. Visitor on test page opens the share panel. They enter their name and email (browser autofill, no OTP). They choose WhatsApp or Copy Link — no email channel.
2. `POST /referrals/create` (FastAPI) → `test_referrals` row created with `referral_code`; `test_share_events` row created.
3. Friend opens link → `test_referrals.link_opened = true`.
4. Friend takes Phase 1 (5 questions). At the phase transition screen, they enter name + email and verify via OTP: `POST /verify/send` → `email_verifications` row created; `POST /verify/check` → `email_verifications.used_at` set.
5. `POST /test/capture-email` requires a verified `email_verifications` row (purpose `'test_gate'`) before returning Phase 2 questions. → `test_sessions.email` and `name` recorded; `test_referrals.test_session_id` set.
6. Friend completes test → `test_sessions` completed. Friend opts in as supporter → `test_supporters` row created.
7. Student hits milestone → Python cron queries `test_supporters`; creates `milestone_notifications` row; sends email.
8. Supporter gifts subscription → Stripe webhook → Python creates `gift_subscriptions` row; sets `app.student.active_gift_id`.
9. Admin approves testimonial → `testimonial_requests.status = 'published'`; visible publicly on landing page.

---

## 3. Tables

### Table: test_questions

AI-generated question bank for the placement test.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| language | text | Language this question tests |
| cefr_level | text | A1/A2/B1/B2/C1/C2 |
| skill_area | text | grammar/vocabulary/listening/reading |
| question_type | text | Type of question format |
| content | jsonb | Full question content (varies by type) |
| correct_answer | text | Canonical correct answer |
| audio_url | text | Optional audio file for listening questions |
| image_url | text | Optional image |
| fsi_lesson_ref | text | FSI lesson reference for curriculum alignment |
| ai_generated | boolean | Whether AI created this question (default true) |
| human_reviewed | boolean | Whether a human has reviewed it (default false) |
| active | boolean | Whether to include in tests |
| flag_count | integer | Number of times flagged as problematic |
| created_at / updated_at | timestamptz | — |

#### RLS
- Authenticated users: no direct read access — questions are served via Edge Function to prevent answer scraping.

---

### Table: test_sessions

Each test attempt. Can be anonymous (no auth user) or linked to a referral.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| language | text | Language being tested |
| status | text | 'phase_1' → 'phase_2' → 'completed' |
| email | text | Tester's email (may be anonymous) |
| name | text | Tester's name |
| referrer_student_id | uuid | FK → auth.users(id) — who shared the link (nullable for organic) |
| referral_code | text | Which referral code was used |
| question_ids | uuid[] | Ordered list of question IDs for this session |
| answers | jsonb | Map of question_id → answer |
| phase_1_score | jsonb | Phase 1 scoring breakdown |
| final_score | jsonb | Final scoring breakdown |
| cefr_result | text | Determined CEFR level (A1–C2) |
| adaptive_state | jsonb | State for adaptive question selection |
| started_at | timestamptz | — |
| phase_2_started_at | timestamptz | — |
| completed_at | timestamptz | — |
| ip_hash | text | Hashed IP for fraud detection |
| user_agent | text | Browser user agent |
| created_at | timestamptz | — |

#### RLS
- No authenticated user policies — sessions are served via service role in Edge Functions.

---

### Table: test_share_events

Tracks when a test link was shared via which channel. **Only `'whatsapp'` and `'copy_link'` are valid channels** — the email channel was removed per `partial_simplify_share.md`.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| test_session_id | uuid | FK → test_sessions(id) |
| channel | text | **'whatsapp' or 'copy_link' only** — email channel removed |
| recipient_count | integer | Number of recipients (always 1 for copy_link) |
| created_at | timestamptz | — |

---

### Table: test_referrals

Invite links created by students to share the test.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| referral_code | text | UNIQUE — the shareable code |
| sender_type | text | 'student' / 'admin' / 'organic' |
| sender_name / sender_email | text | Sender identity |
| sender_student_id | uuid | FK → auth.users(id) — if sender is a student |
| intent | text | Why they shared (e.g., 'invite_friend') |
| channel | text | **'whatsapp' or 'copy_link' only** — email channel removed per partial_simplify_share.md |
| recipient_email | text | **Retained in schema (nullable), no longer written** — email channel removed; column kept for future re-enablement |
| language | text | Language of the test being referred |
| link_opened | boolean | Whether the link was clicked |
| link_opened_at | timestamptz | — |
| test_started | boolean | Whether the friend started the test |
| test_session_id | uuid | FK → test_sessions(id) — linked when test starts |
| test_completed | boolean | Whether the friend completed the test |
| approved | boolean | Admin approval for referral reward |
| sender_notified_on_completion | boolean | Prevents duplicate notification |
| sender_notified_on_approval | boolean | Prevents duplicate notification |
| created_at | timestamptz | — |

#### RLS
- Students: SELECT own referrals (`sender_student_id = auth.uid()`).

---

### Table: test_supporters

People who opted in to follow a student's learning journey.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| supporter_email | text | Supporter's email |
| supporter_name | text | Supporter's name |
| student_id | uuid | FK → auth.users(id) — the student being followed |
| test_session_id | uuid | FK → test_sessions(id) — test that triggered the opt-in |
| status | text | 'approved' / 'pending' / 'opted_out' |
| opted_in_at | timestamptz | — |
| student_visible | boolean | Whether student can see this supporter |
| last_notified_at | timestamptz | — |
| milestone_email_count | integer | Total milestone emails sent |
| gift_nudge_shown | boolean | Prevents duplicate gift prompt |
| gift_nudge_shown_at | timestamptz | — |
| engagement_score | integer | Python-calculated engagement metric |
| created_at | timestamptz | — |

UNIQUE(supporter_email, student_id) — one opt-in per supporter per student.

#### RLS
- Students: SELECT own supporters. UPDATE `student_visible` flag on own supporters.

---

### Table: milestone_notifications

Queue of milestone emails to be sent to supporters by Python cron.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| student_id | uuid | FK → auth.users(id) |
| supporter_id | uuid | FK → test_supporters(id) |
| milestone_type | text | Type of milestone (e.g., 'streak_7', 'lesson_10') |
| milestone_data | jsonb | Supporting data for the email template |
| status | text | 'pending' → 'sent' |
| sent_at | timestamptz | — |
| created_at | timestamptz | — |

#### RLS
- Students: SELECT own milestone records.

---

### Table: gift_subscriptions

A supporter's purchase of a subscription upgrade for a student via Stripe.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| supporter_id | uuid | FK → test_supporters(id) |
| student_id | uuid | FK → auth.users(id) |
| stripe_payment_id | text | Stripe payment intent ID |
| stripe_receipt_url | text | Receipt URL |
| tier | text | Tier being gifted (uses canonical 'tutor' not 'tutor_tier') |
| duration_months | integer | Gift duration |
| amount_cents | integer | Amount charged |
| currency | text | Payment currency |
| anonymous | boolean | Whether supporter wants to be anonymous |
| status | text | 'pending' → 'active' → 'expired' |
| activated_at | timestamptz | When gift was applied |
| expires_at | timestamptz | When gift expires |
| expiry_warned | boolean | Prevents duplicate expiry warning |
| renewal_nudge_sent | boolean | Prevents duplicate renewal prompt |
| refunded_at | timestamptz | If refunded |
| created_at | timestamptz | — |

#### RLS
- Students: SELECT own gift rows.
- tauka-python: writes via service role.

---

### Table: testimonial_requests

Post-gift testimonial collection for marketing use.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| supporter_id | uuid | FK → test_supporters(id) |
| gift_id | uuid | FK → gift_subscriptions(id) |
| status | text | 'pending' → 'approved' → 'published' |
| sent_at | timestamptz | When request was sent |
| quote_text | text | The testimonial text |
| display_name | text | How the supporter wants to be named |
| display_preference | text | 'full_name' / 'first_name' / 'anonymous' |
| approved_by_admin | boolean | Admin approval flag |
| published_at | timestamptz | When made public |
| created_at | timestamptz | — |

#### RLS
- Publicly readable when `status = 'published'` (no auth required).

---

### Table: email_verifications

> **Added by:** tauka-react-web on 2026-05-26

OTP code storage for the mid-test email gate. A row is created when `POST /verify/send` is called and marked used when `POST /verify/check` succeeds. Python cron purges expired rows.

This table is **NOT in `tauka_full_schema.sql`** — it is a tauka-python-owned table managed in the FastAPI service. It is documented here because its lifecycle is driven by the test flow.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| email | text | Email address the code was sent to |
| code | text | 6-digit code (stored as bcrypt hash, not plaintext) |
| purpose | text | **Only `'test_gate'`** — `'share_invite'` must never be used |
| created_at | timestamptz | Code creation time |
| expires_at | timestamptz | Code expiry (typically created_at + 10 minutes) |
| used_at | timestamptz | Set when code is successfully verified; NULL = not yet used |
| attempts | integer | Number of failed verification attempts |
| ip_address | text | Sender's IP for rate limiting |

#### Rate limiting (enforced in Python, not DB)
- Max 3 OTP sends per email per hour.
- Max 10 OTP sends per IP per hour.
- Max 5 failed verification attempts before the code is invalidated.

#### RLS
- No authenticated user policies — this table is accessed only by the Python service via service role. No Flutter or web client reads this table directly.

#### What this table is NOT for
- Do NOT use this table for email verification at the share step — the share component uses browser autofill with no verification.
- Do NOT add a `'share_invite'` purpose — the previous spec included this; it was removed.

---

## 4. Relationships Between Tables in This Domain

`test_questions` → (served to) `test_sessions` (via Edge Function). `test_sessions` ← `test_referrals` (referral tracks which session the invited friend took). `test_sessions` ← `test_supporters` (supporter opt-in triggered by a test session). `test_supporters` → `milestone_notifications` (Python queues emails). `test_supporters` → `gift_subscriptions` (supporter gifts a subscription). `gift_subscriptions` → `testimonial_requests` (post-gift testimonial requested). `gift_subscriptions` ← `app.student.active_gift_id` (cross-domain: active gift tracked on student).

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| auth.users | Supabase Auth | test_sessions.referrer_student_id, test_supporters.student_id | auth.users.id |
| app.student | student_tutor | student.active_gift_id → gift_subscriptions.id (deferred FK) | student.active_gift_id column |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| app.gift_subscriptions | student_tutor | student.active_gift_id FK | gift_subscriptions.id |
| app.test_supporters | tauka-python | Python cron queries supporters for milestone emails | supporter_email, student_id, status, last_notified_at |
| app.gift_subscriptions | tauka-react-web | /account/supporters renders active/past gifts; RLS policy students_read_own_gifts must remain | student_id, tier, duration_months, amount_cents, status, expires_at, anonymous, activated_at |
| app.test_supporters | tauka-react-web | /account/supporters renders supporter list; student can toggle visibility | student_id, supporter_email, supporter_name, student_visible, status |
| app.testimonial_requests | tauka-react-web | Landing page renders published testimonials (no auth); public RLS policy public_read_published_testimonials must remain | quote_text, display_name, display_preference, status='published' |

---

## 6. Extension Rules

#### If you need a new milestone type
Add the new `milestone_type` string value in Python. No DB migration needed — `milestone_type` is a free-text column.

#### If you need a new test question type
Add to the Dart question parser. The `content` column is JSONB — structure varies by type without schema changes.

#### Specifically do NOT
- Do NOT use `'tutor_tier'` as a tier value in `gift_subscriptions.tier` — use `'tutor'` (CONFLICT-003).
- Do NOT duplicate Stripe data in `test_supporters` — Stripe columns live on `app.student` only.
- Do NOT make `test_questions` directly readable by Flutter clients — route through an Edge Function.
- Do NOT add an `'email'` channel value to `test_referrals.channel` or `test_share_events.channel` — the email channel was deliberately removed to reduce friction. If email sharing is re-added, it requires a new spec decision, not a silent code change.
- Do NOT add a `'share_invite'` purpose to `email_verifications` — OTP is scoped to `'test_gate'` only.

---

## 7. Usage by tauka-react-web

> Added by: tauka-react-web on 2026-05-26

### Tables read directly via supabase-js (user's JWT + RLS)

**`app.test_supporters`** — `/account/supporters` page reads the authenticated student's supporter list using the `students_read_own_supporters` policy (`student_id = auth.uid()`). The page renders `supporter_name`, `supporter_email`, `status`, and `student_visible`. The student can toggle `student_visible` using `PATCH` via `POST /supporters/visibility` FastAPI endpoint (which updates the column via service role, not direct client write).

**`app.gift_subscriptions`** — `/account/supporters` page reads the student's active and past gifts using the `students_read_own_gifts` policy (`student_id = auth.uid()`). Renders gift tier, duration, anonymous flag, expiry date, and activation date.

**`app.testimonial_requests`** — Landing page (`/`) reads all published testimonials using the `public_read_published_testimonials` policy (`status = 'published'`). This is a public, unauthenticated read. The query is: `SELECT quote_text, display_name, display_preference, published_at FROM app.testimonial_requests WHERE status = 'published'`.

### Tables read via FastAPI (JWT passed as Bearer token)

**Test session management** — The full test flow (`/test/[language]`) calls FastAPI endpoints: `POST /test/{language}/start`, `POST /test/submit-phase-1`, `POST /test/capture-email` (requires OTP at gate), `POST /test/submit-final`. No direct DB access from the web client.

**Gift checkout** — `/gift/checkout` calls `POST /gifts/checkout` → FastAPI creates Stripe Checkout → webhook activates gift. No direct DB writes from the web client.

**Referral creation** — The share panel on the test page calls `POST /referrals/create` (unauthenticated) or `POST /referrals/create-authenticated` (logged-in student). Channel is `'whatsapp'` or `'copy_link'` only.

### What the web client does NOT do
- Does NOT read `test_sessions` directly — test session state is managed server-side by FastAPI.
- Does NOT read `test_questions` — served via FastAPI Edge Function to prevent answer scraping.
- Does NOT write to `test_supporters` — supporter opt-in is handled server-side via `POST /supporters/opt-in`.
- Does NOT write to `email_verifications` — OTP send/verify are FastAPI endpoints (`POST /verify/send`, `POST /verify/check`).
