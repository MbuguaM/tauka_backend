# Domain: Student & Tutor Identity

> **Owner project:** tauka-flutter (schema); tauka-python (writes student.tier/stripe columns)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §2.1 Role Overview, §5 Student Features, §6 Tutor Features, §9 Subscription & Tier Gating

---

## 1. Business Context

### What real-world problem does this domain solve?
Tauka has two primary human actors beyond admins: students who pay to learn, and tutors who are hired to teach. Each needs a distinct identity record that tracks their relationship to the platform — students need tier/subscription status, assigned tutors, and learning progress counters; tutors need their teaching rate, bio, and language so they can be matched to cohorts and paid correctly. This domain is the contract between the billing system, the learning system, and the teaching system.

### How does this domain fit into the larger system?
`app.student` is the authoritative record of a learner's subscription status. The Python backend writes Stripe data here; the Flutter client reads tier. `app.tutor` is the gate for teaching features: a user cannot create classes, video sessions, or assign decks unless their `id` appears in `app.tutor`. The enrollment tables (`student_course`, `tutor_course`, `tutor_assignment`) form the graph that RLS policies across the entire database use to determine "can this user see this content?"

### User stories this domain serves
- As a student, I am enrolled in a course so I can access its units, dictionary, and Anki decks.
- As a student, I have a subscription tier that unlocks AI practice, video sessions, and full curriculum.
- As a tutor, I can be assigned to courses so I can see enrolled students' progress and profiles.
- As a tutor, I can set my per-session rate so the payroll system can calculate my earnings.
- As an admin, I can assign a tutor to a student so the student gets a dedicated tutor.
- As a student, I can see which tutor I am assigned to.

---

## 2. Design Decisions

### Architecture chosen
The `student` and `tutor` tables use the auth user UUID as primary key (not a separate surrogate key). This eliminates a JOIN layer when checking "is this user a student/tutor?" — policies can do `EXISTS (SELECT 1 FROM app.student WHERE id = auth.uid())` directly. Stripe/billing data lives ONLY on `app.student`, not on `app.user_profiles`, to maintain a single source of truth owned by the Python backend.

### Why this architecture and not alternatives

| Approach | Why We Rejected It |
|---|---|
| Separate `student_profiles` table for Stripe data | Two Python migrations to touch (student + student_profiles) for every billing change. Additional JOIN for every tier check. |
| Store tier on user_profiles only (no student table) | Python backend cannot write to user_profiles without bypassing Flutter's RLS. student table gives Python an isolated authority surface. |
| Use `user_profiles.user_type` as the sole student/tutor indicator | Cannot enforce FK constraints (e.g., student_course.student_id → user with user_type='student') without a dedicated table. Also loses Python's write surface for billing columns. |
| Separate enrollment entity with status lifecycle | Current model uses a simple junction table (student_course). The status/lifecycle is tracked in unit_progress, not enrollment. Enrollment is binary: enrolled or not. |

### Key invariants
- `app.student.id` MUST equal `auth.users.id` — no separate UUID.
- `app.student.tier` MUST be kept in sync with `app.user_profiles.subscription_tier` by `trg_sync_student_tier` (trigger fires automatically on INSERT or UPDATE OF tier on student).
- Valid tier values: `'free' | 'learner' | 'tutor' | 'intensive'`. Never `'tutor_tier'`.
- `app.student.current_streak` is the Python-validated streak. Flutter MUST NOT write this column.
- `app.student.stripe_customer_id` and `stripe_subscription_id` are written ONLY by tauka-python via service role. Flutter client has no write path to these columns.
- A user can have BOTH a `student` row AND a `tutor` row (e.g., a tutor who also takes courses as a student).
- `tutor_started_at IS NOT NULL AND tutor_ended_at IS NULL` means the student currently has an assigned tutor.
- `app.tutor_course` keys on `tutor_id` (Amendment A2 renamed the old `student_id` column; see [[CONFLICT_LOG]] CONFLICT-012, resolved).

### Data flow
1. Student signs up → `app.student` row created by Flutter with `tier='free'`.
2. Student completes Stripe checkout on web → Python backend receives Stripe webhook → Python writes `student.tier='learner'` using service role → `trg_sync_student_tier` fires → `user_profiles.subscription_tier='learner'` updated automatically.
3. Admin assigns tutor → `app.tutor_assignment` row created with `tutor_id + course_id`.
4. Admin links student to tutor → `app.student.tutor_id` and `tutor_started_at` set.
5. Student enrolls in course → `app.student_course` row created.
6. Tutor RLS uses tutor_assignment to determine which student profiles, progress, and exercise results are visible.

---

## 3. Tables — Detailed Specification

### Table: student

#### Purpose
One row per student account. Dual-owned: Flutter creates/manages the row structure; Python writes billing/tier columns. This table is the source of truth for subscription tier, Stripe identity, and learning milestone counters.

#### Spec origin
§2.1 Role Storage, §9 Subscription & Tier Gating. §01 in tauka_full_schema.sql adds Stripe/tier fields.

#### Row lifecycle
Created by Flutter on sign-up (tier='free'). `tier` updated by Python on Stripe webhook. `tutor_id` set/cleared by admin action. `lessons_completed`, `current_streak`, `ai_conversations` updated by Python cron/webhooks. `deleted_at` set on account deletion — NEVER hard deleted (billing records must survive).

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | uuid | NO | — | PK = auth.users.id | FK → auth.users(id) CASCADE, FK → (implicitly required: user_profiles row must exist) |
| created_at | timestamptz | NO | now() | — | — |
| is_con_creator | text | YES | NULL | Legacy content creator flag | — |
| account_status | text | YES | 'active' | Active/suspended/banned | — |
| tutor_id | uuid | YES | NULL | Assigned tutor | FK → app.tutor(id) ON DELETE SET NULL |
| deleted_at | timestamptz | YES | NULL | Soft delete | — |
| tier | text | NO | 'free' | Subscription tier — Python authoritative | Values: free/learner/tutor/intensive |
| tier_source | text | NO | 'self' | How tier was obtained | Values: self/gifted/admin |
| stripe_customer_id | text | YES | NULL | Stripe customer ID | UNIQUE; Python writes only |
| stripe_subscription_id | text | YES | NULL | Active Stripe subscription | Python writes only |
| subscription_status | text | YES | 'none' | Stripe subscription state | Values: none/active/past_due/cancelled/trialing |
| active_gift_id | uuid | YES | NULL | Currently active gift subscription | FK → app.gift_subscriptions(id) (deferred FK added after §14) |
| original_tier | text | YES | NULL | Tier before gift was applied | — |
| current_period_end | timestamptz | YES | NULL | Stripe billing period end | Python writes only |
| lessons_completed | int | YES | 0 | Total lessons completed | Python writes only |
| current_streak | int | YES | 0 | Python-validated milestone streak | Python writes only; see [[identity_access]] for Flutter streak |
| ai_conversations | int | YES | 0 | Total AI conversations | Python writes only |
| cohort_sessions_attended | int | YES | 0 | Total cohort video sessions | Python writes only |
| assessed_level | text | YES | NULL | CEFR level from placement test | Python writes |
| last_unit_milestone | int | YES | 0 | Highest unit number completed | Python writes |
| streak_14_notified | boolean | YES | false | Prevent duplicate 14-day milestone notification | Python writes |
| first_ai_convo_notified | boolean | YES | false | Prevent duplicate first-AI-convo notification | Python writes |
| first_cohort_notified | boolean | YES | false | — | Python writes |
| last_notified_level | text | YES | NULL | Last level for which notification was sent | Python writes |
| language | text | YES | NULL | Primary learning language | Flutter writes |
| tutor_started_at | timestamptz | YES | NULL | When tutor assignment began | Flutter/admin writes |
| tutor_ended_at | timestamptz | YES | NULL | When tutor assignment ended | Flutter/admin writes |
| updated_at | timestamptz | YES | now() | Auto-updated by trigger | Managed by trg_student_updated_at |

#### Access patterns
- **Student (Flutter):** `SELECT * WHERE id = auth.uid()` — own row, reads tier and stripe status.
- **Tutor (Flutter):** Reads student rows for assigned students via `student_select_tutor_assigned` policy.
- **Python backend:** Writes tier/stripe columns using service role (bypasses RLS).
- **Admin (Flutter):** Full access via `student_all_admin` policy.

#### What this table is NOT for
- Do NOT store exercise scores or progress here — use `app.exercise_result` and `app.unit_progress`.
- Do NOT duplicate Stripe data on `user_profiles` — read tier from `user_profiles.subscription_tier` (trigger-synced).
- Do NOT write `current_streak` from Flutter — that is Python's responsibility.

---

### Table: tutor

#### Purpose
One row per tutor. The existence of a row here is what grants a user teaching privileges. The `id` equals `auth.users.id`. Two FKs (to auth.users AND to user_profiles) allow PostgREST to join profiles via the shared id.

#### Spec origin
§2.1 Tutor Role, §6 Feature Specifications — Tutor.

#### Row lifecycle
Created when a user completes the tutor onboarding pipeline (admin action). `status` changes from 'pending' → 'active' when approved. Soft-deleted when a tutor leaves the platform.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | uuid | NO | — | PK = auth.users.id | FK → auth.users(id) CASCADE; FK → user_profiles(id) CASCADE |
| created_at | timestamptz | NO | now() | — | — |
| rating | bigint | YES | NULL | Average tutor rating (1–5 scale) | — |
| status | text | YES | 'active' | Tutor account status | Values: active/inactive/pending |
| deleted_at | timestamptz | YES | NULL | Soft delete | — |
| per_session_rate_cents | int | YES | 0 | Tutor's earning rate per session in cents | Added via §02 |
| language | text | YES | NULL | Teaching language | Added via §02 |
| bio | text | YES | NULL | Public-facing tutor bio | Added via §02 |
| updated_at | timestamptz | YES | now() | — | Auto-updated by trg_tutor_updated_at |

#### Access patterns
- **Any authenticated user:** SELECT active tutors via `tutor_select_active` policy.
- **Tutor (Flutter):** UPDATE own row (bio, language) via `tutor_update_own`.
- **Python backend:** Reads `per_session_rate_cents` for earnings calculation.

#### Write ordering (REQUIRED)
The two FKs on `id` mean a session is NOT sufficient to insert here. `app.user_profiles`
must already hold the row, or the insert fails with `23503`
(`tutor_user_profile_fkey`). Same for `app.student`.

Write order is therefore always: **`auth.users` -> `app.user_profiles` -> `app.tutor` /
`app.student` -> `app.tutor_course` / `app.student_course`.**

This is not theoretical. Onboarding fired the `app.tutor` insert unawaited, racing the
`user_profiles` write it depends on, and discarded the resulting 23503 in a bare
`catch (_) {}`. Production carried zero `app.tutor` rows; the only visible symptom was a
23503 from `tutor_course` on the course picker, several screens later. Fixed 2026-08-19 in
`OnboardingData.updateUserType` and `SupabaseService.upsertTutor`/`upsertStudent`, which
now ensure the profile row first and return whether they succeeded.

---

### Table: student_course

#### Purpose
Enrollment junction: which students are enrolled in which courses. Controls which dictionaries and Anki decks a student can read (via RLS policies on those tables). Simple many-to-many — no lifecycle states (enrollment is binary).

#### Row lifecycle
Created when a student enrolls. Deleted (hard delete) when a student unenrolls. No soft delete needed — if a student re-enrolls, a new row is inserted.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| student_id | uuid | NO | — | Student identity | FK → app.student(id) ON DELETE CASCADE |
| course_id | uuid | NO | — | Enrolled course | FK → app.course(id) ON DELETE CASCADE |
| level | bigint | YES | NULL | Student's level within the course | — |
| (PK) | — | — | — | Composite PK | PRIMARY KEY (student_id, course_id) |

---

### Table: tutor_course

#### Purpose
Associates tutors with the courses they teach. The key column is `tutor_id`; the old, misleadingly named `student_id` was renamed by Amendment A2 (CONFLICT-012, resolved 2026-08-19). The FK constraint still carries its historical name, `tutor_course_student_id_fkey` — that is cosmetic.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| tutor_id | uuid | NO | — | The tutor teaching this course | FK → app.tutor(id) ON DELETE CASCADE |
| course_id | uuid | NO | — | Course the tutor teaches | FK → app.course(id) ON DELETE CASCADE |
| student_rating | bigint | YES | NULL | Average rating tutor gave to this course content | — |

---

### Table: tutor_assignment

#### Purpose
Admin-managed assignment of a tutor to a specific course. This is the table that RLS policies across `unit_progress`, `exercise_result`, `user_profiles`, and `dictionary` use to determine which students a tutor can see. A tutor can only read data for students enrolled in courses they are assigned to.

#### Row lifecycle
Created by admin when assigning a tutor to a course. Soft-deleted when the assignment ends. The `class_id` column (added via §B3) optionally narrows the assignment to a specific class.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK | — |
| tutor_id | uuid | YES | NULL | The assigned tutor | FK → app.tutor(id) ON DELETE CASCADE |
| course_id | uuid | YES | NULL | The course being assigned | FK → app.course(id) ON DELETE CASCADE |
| assigned_by | uuid | YES | NULL | Admin who made the assignment | FK → auth.users(id) ON DELETE SET NULL |
| assigned_at | timestamptz | YES | now() | — | — |
| deleted_at | timestamptz | YES | NULL | Soft delete | — |
| class_id | uuid | YES | NULL | Optional class scoping | FK → app.classes(id); added via §B3 |

---

## 4. Relationships Between Tables in This Domain

A `student` has enrolled in zero or more courses via `student_course`. A `tutor` teaches zero or more courses via `tutor_course`. A `tutor` is formally assigned to teach a course to specific students via `tutor_assignment`. A `student` may be assigned to a specific tutor via `student.tutor_id`. These two assignment mechanisms are independent: `tutor_assignment` gives the tutor READ access to course-enrolled students' data; `student.tutor_id` gives the tutor a direct personal relationship with the student.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| auth.users | Supabase Auth | student.id and tutor.id reference auth.users.id | Renaming or retyping auth.users.id breaks both tables |
| app.user_profiles | identity_access | tutor_user_profile_fkey: tutor.id also FKs to user_profiles.id | Deleting user_profiles without cascading would violate FK |
| app.gift_subscriptions | test_referral | student.active_gift_id references gift_subscriptions.id | Renaming gift_subscriptions.id breaks the FK |
| app.classes | messaging | tutor_assignment.class_id references classes.id | — |
| app.course | course_content | student_course and tutor_assignment reference course.id | Renaming course.id breaks all enrollment and assignment FKs |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| app.student | tauka-python | Writes tier, stripe columns via service role | Column names: tier, stripe_customer_id, stripe_subscription_id, subscription_status, current_period_end, active_gift_id |
| app.student | RLS policies (entire DB) | Subqueries: `WHERE id IN (SELECT sc.student_id FROM student_course JOIN tutor_assignment...)` | student.id, student.tutor_id, student.tutor_ended_at |
| app.tutor_assignment | RLS policies (entire DB) | Determines tutor READ access to student data across 6+ tables | tutor_id, course_id, deleted_at column names |

---

## 6. Extension Rules

#### If you need a new student attribute
Add a column to `app.student`. Decide ownership: if Flutter writes it, add it freely. If Python writes it, document in OWNERS.md as Python-authoritative.

#### If you need a new billing state
Add to `subscription_status` CHECK constraint enum AND update Python billing service. Do NOT add new boolean columns like `is_past_due`.

#### If you need to track tutor–student history
Add timestamped rows to a new `tutor_student_history` table rather than modifying `student.tutor_id` history. The current model tracks only the current tutor.

#### Specifically do NOT
- Do NOT write `student.tier` from Flutter — Python is the sole writer.
- Do NOT add a `subscription_tier` column to `app.student` — it already has `tier`.
- Do NOT create a `tutor_profile` satellite table — extend `app.tutor` with columns.
- `tutor_course.student_id` was renamed to `tutor_id` (CONFLICT-012, resolved). Any query still filtering on `student_id` is broken and must be corrected.

---

## 7. Usage by tauka-react-web

> Added by: tauka-react-web on 2026-05-26

### `app.student` — read via supabase-js on the account portal

The `/account` (student home) and `/account/subscription` pages read `app.student` directly using the `student_select_own` RLS policy (`id = auth.uid()`). The web client reads the following columns to render the portal:

| Column | Page | Purpose |
|---|---|---|
| tier | /account, /account/subscription | Show current plan badge (Free / Learner / Tutor / Intensive) |
| tier_source | /account/subscription | Distinguish self-paid vs gifted tier |
| subscription_status | /account/subscription | Show 'active', 'past_due', 'cancelled', 'trialing' state badge |
| current_period_end | /account/subscription | Show billing renewal date |
| active_gift_id | /account/subscription | Detect active gift (non-null means gift is in effect) |
| assessed_level | /account | Show CEFR level from placement test |
| current_streak | /account | Show learning streak counter |
| lessons_completed | /account | Show total lessons completed |

The web client **never writes** `tier`, `stripe_customer_id`, `stripe_subscription_id`, `subscription_status`, `current_period_end`, `active_gift_id`, or `current_streak`. These are Python-authoritative. Plan changes, cancellations, and reactivations all go through FastAPI (`POST /account/subscription/change`, `/cancel`, `/reactivate`).

### `app.tutor` — not read directly

The web client does not read `app.tutor` directly via supabase-js. Tutor portal data (earnings, payout settings, students) is served via FastAPI to handle aggregation and masking. The only exception is `tutor_availability` — see tutor_management domain.
