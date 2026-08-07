# Domain: Identity & Access

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §2.1 Role Overview, §2.2 Role Storage, §3.1 First-Time Sign-In, §3.2 Device-Connect, §1 Device-pair model

---

## 1. Business Context

### What real-world problem does this domain solve?
Tauka must know who is using the app, whether they have paid for a higher tier, what device they are on, and whether they have admin privileges. Without this domain, the app cannot decide what content to show, what features to unlock, or whether a user is allowed to make changes to the platform. This domain provides the foundational "who are you?" answer that every other domain relies on.

### How does this domain fit into the larger system?
Every RLS policy in the database checks `auth.uid()` and then joins to `user_profiles` or `admin_users` to determine role and tier. The `user_type` column on `user_profiles` determines whether the user sees the student UI or the tutor/admin UI. The `subscription_tier` column is the read-cache that allows the Flutter client to gate features without a round-trip to `app.student`. Upstream: `auth.users` (Supabase Auth) provides the UUID identity. Downstream: every other domain joins to `user_profiles.id` or `auth.users(id)` as their user anchor.

### User stories this domain serves
- As a student, I can sign in with OTP or Google so that my progress is saved across devices.
- As a student, I can connect a second device (desktop) by scanning a QR code so I can study on any platform.
- As a tutor, I see the tutor UI and not the student UI because my user_type is `tutor`.
- As an admin, I have full platform authority because my row exists in `admin_users`.
- As a user, my account cannot be accessed on more than 2 devices simultaneously.

---

## 2. Design Decisions

### Architecture chosen
One `user_profiles` row per `auth.users` row, created immediately on first sign-up. The profile extends auth with onboarding fields (known languages, study method), subscription tier cache, XP/streak tracking, and device metadata. `admin_users` is a separate table (not a column on user_profiles) to avoid the 42P17 recursive RLS policy problem.

### Why this architecture and not alternatives

| Approach | Why We Rejected It |
|---|---|
| Store everything in `auth.users.raw_user_meta_data` | Supabase Auth metadata is not RLS-controllable, has size limits, and cannot be indexed. Separate table allows proper RLS, indexes, and typed columns. |
| Single `users` table with role column | Admin role checks inside the admin table policy would be self-referential (42P17 infinite recursion). The `admin_users` table + `is_admin()` SECURITY DEFINER function cleanly breaks the recursion. |
| Boolean `is_admin` column on user_profiles | Would require user_profiles policy to query user_profiles within itself for admin checks — same recursion problem. |
| Store tier/stripe on user_profiles | Rejected (see [[student_tutor]] domain). Stripe data is authoritative on `app.student`. `subscription_tier` on user_profiles is a denormalized read-cache only. |

### Key invariants
- Every `auth.users` row MUST have a corresponding `user_profiles` row (enforced by application logic on sign-up).
- `user_profiles.id` is ALWAYS equal to `auth.users.id` — no separate UUID generation.
- `subscription_tier` on `user_profiles` MUST equal `student.tier` for student accounts — kept in sync by `trg_sync_student_tier` trigger.
- `user_type` is either NULL, `'student'`, or `'tutor'` — never modified by the Flutter client directly (§48 RLS policy blocks self-promotion).
- `role` column (`'student' | 'tutor' | 'admin'`) may only be written by service_role or admin policy — never by the authenticated user's own UPDATE policy.
- A user can have at most `device_limit` (default 2) devices registered in `app.devices`.
- `deleted_at IS NOT NULL` means the account is soft-deleted; all SELECT policies include `AND deleted_at IS NULL`.

### Data flow
1. User triggers Supabase Auth (OTP or Google OAuth) → `auth.users` row created.
2. Flutter calls `SupabaseService.upsertUserProfile()` → `user_profiles` row created with onboarding fields.
3. Onboarding flow collects `user_type`, `study_method`, `known_languages` → `user_profiles` row updated.
4. Python backend writes `app.student.tier` after Stripe event → `trg_sync_student_tier` fires → `user_profiles.subscription_tier` updated automatically.
5. `AuthStateManager._fetchAndSetRole()` reads `subscription_tier` and `user_type` from `user_profiles` → persisted to `authfile.json` for offline cold start.
6. `TierGatingService` subscribes to Supabase Realtime on `user_profiles` → instant unlock on tier upgrade.

---

## 3. Tables — Detailed Specification

### Table: user_profiles

#### Purpose
The primary profile row for every user in the system. Extends `auth.users` with human-readable identity (name, avatar), onboarding completion state, a cached copy of the subscription tier (for fast client-side gating without joining to `app.student`), and Flutter client-side streak/XP counters. This is the single most-read table in the database.

#### Spec origin
§2.1 Role Overview, §2.2 Role Storage, §3.1 First-Time Sign-In; onboarding field consolidation removed former `app.onboarding` table.

#### Row lifecycle
Created immediately when a user first authenticates (before onboarding). Updated throughout onboarding (user_type, known_languages, study_method). `subscription_tier` updated by trigger when `student.tier` changes. `streak_days`, `xp_total`, `xp_week` updated by Flutter via `update_user_streak()` and `add_user_xp()` RPCs after each study session. Soft-deleted (`deleted_at` set) on account deactivation; never hard-deleted.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | uuid | NO | — | PK; equals auth.users.id | FK → auth.users(id) ON DELETE CASCADE |
| first_name | text | NO | — | Display name (first) | NOT NULL |
| last_name | text | YES | NULL | Display name (last) | — |
| avatar_url | text | YES | NULL | Storage URL for profile photo | — |
| location | text | YES | NULL | User-provided location text | — |
| phone_number | text | YES | NULL | Optional phone for OTP | — |
| deleted_at | timestamptz | YES | NULL | Soft delete marker | — |
| created_at | timestamptz | YES | now() | Row creation time | — |
| known_languages | jsonb | YES | [] | Array of language codes the user already speaks | — |
| user_type | text | YES | NULL | Student or tutor role | CHECK IN ('tutor','student') or NULL |
| study_method | text | YES | NULL | Onboarding: preferred study style | — |
| teaching_options | jsonb | YES | [] | Onboarding: tutors' preferred teaching modes | — |
| completed_at | timestamptz | YES | NULL | Onboarding completion timestamp | — |
| onboarding_created_at | timestamptz | YES | NULL | When onboarding was started | — |
| onboarding_location | jsonb | YES | NULL | Location data collected during onboarding | — |
| subscription_tier | text | YES | 'free' | Denorm cache of student.tier; NEVER write from Flutter client | CHECK IN ('free','learner','tutor','intensive') |
| tier_updated_at | timestamptz | YES | NULL | When tier was last synced from student | — |
| streak_days | int | YES | 0 | Flutter client-side optimistic streak (see [[student_tutor]] for Python-validated streak) | — |
| last_active_date | date | YES | NULL | Date of last study session; used by streak reset cron | — |
| xp_total | int | YES | 0 | Lifetime XP earned | — |
| xp_week | int | YES | 0 | XP this week; reset every Monday by pg_cron | — |
| subscription_expires_at | timestamptz | YES | NULL | When the current subscription expires | — |
| device_limit | int | YES | 2 | Max devices allowed (default 2: 1 mobile + 1 desktop) | — |
| devices | jsonb | YES | [] | Cached device metadata for offline validation | — |
| proficiency_level | text | YES | NULL | User-assessed language proficiency | CHECK IN ('beginner','intermediate','advanced') |
| role | text | NO | 'student' | Supabase role column for RLS shortcut | CHECK IN ('student','tutor','admin'); added via §B1 |
| suspended_at | timestamptz | YES | NULL | If non-null, account is suspended | Added via §B1 |
| suspended_by | uuid | YES | NULL | Admin who issued suspension | FK → user_profiles(id); added via §B1 |
| suspension_reason | text | YES | NULL | Reason for suspension | Added via §B1 |

#### Indexes
| Index Name | Columns | Type | Why |
|---|---|---|---|
| idx_user_profiles_deleted_at | deleted_at WHERE deleted_at IS NULL | Partial btree | All SELECT policies filter deleted_at IS NULL |
| idx_user_profiles_subscription_tier | subscription_tier WHERE NOT NULL | Partial btree | Tier-based queries (leaderboards, cohort stats) |
| idx_user_profiles_streak | streak_days DESC WHERE > 0 | Partial btree | Leaderboard queries ordered by streak |
| idx_user_profiles_xp_week | xp_week DESC WHERE > 0 | Partial btree | Weekly XP leaderboard |

#### Foreign Keys
| Column | References | On Delete | On Update | Why This Relationship |
|---|---|---|---|---|
| id | auth.users(id) | CASCADE | — | Profile is an extension of the auth identity; deleting the auth user removes the profile |
| suspended_by | app.user_profiles(id) | — | — | Self-referential; tracks which admin suspended the user |

#### Access patterns
- **By owning project (Flutter):** `SELECT * FROM app.user_profiles WHERE id = auth.uid()` — own row read on every app launch (~200 QPS during peak).
- **By tutor (Flutter):** Tutor reads profiles of assigned students via `user_profiles_select_tutor_assigned` policy.
- **By admin (Flutter):** Admin reads all profiles via `user_profiles_select_admin`.
- **By Python backend:** Reads `subscription_tier`, `user_type` for billing decisions; never writes directly (uses trigger).
- **By Realtime (Flutter):** `TierGatingService` subscribes to UPDATE events on own row to detect tier changes.

#### What this table is NOT for
- Do NOT store Stripe customer IDs or subscription IDs here — those live on `app.student` (see [[student_tutor]]).
- Do NOT use this as an activity log — use `app.progress_snapshots` or `app.exercise_result`.
- Do NOT store session tokens or auth credentials — those belong in Supabase Auth internals.
- Do NOT update `subscription_tier` from Flutter client code — the §48 RLS policy blocks it, and it is maintained by `trg_sync_student_tier`.

---

### Table: admin_users

#### Purpose
A separate table for admin identity, distinct from `user_profiles`. Its existence as a separate table (rather than a `role` column on `user_profiles`) is a deliberate architectural choice to prevent a 42P17 infinite recursion error: any RLS policy on `user_profiles` that queries `user_profiles` to check if the user is an admin would recurse infinitely. The `app.is_admin()` SECURITY DEFINER function queries `admin_users` while bypassing RLS, breaking the cycle.

#### Spec origin
§2.1 Admin role, §7 Feature Specifications — Admin.

#### Row lifecycle
Created by invitation only — either directly in the Supabase dashboard or by an existing admin. Never created by the Flutter app's public API. Soft-deleted (deleted_at set) when admin access is revoked. Hard delete not performed to preserve audit trail.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | uuid | NO | — | PK; equals auth.users.id | FK → auth.users(id) ON DELETE CASCADE |
| role | varchar(50) | YES | 'admin' | Admin role name (always 'admin' currently) | — |
| permissions | jsonb | YES | {users:true,content:true,settings:true} | Granular permission flags | — |
| created_at | timestamptz | YES | now() | — | — |
| created_by | uuid | YES | NULL | Which admin created this row | FK → auth.users(id) |
| updated_at | timestamptz | YES | now() | — | Auto-updated by trigger |
| deleted_at | timestamptz | YES | NULL | Soft delete | — |

#### Access patterns
- **By is_admin() function:** `SELECT 1 FROM app.admin_users WHERE id = auth.uid() AND deleted_at IS NULL` — called by every admin-check RLS policy.
- **By admin (Flutter):** SELECT all admins for admin management UI.

#### What this table is NOT for
- Do NOT add admin-specific preferences here — those belong on `user_profiles`.
- Do NOT check admin status via `user_profiles.user_type = 'admin'` — always use `app.is_admin()`.

---

### Table: devices

#### Purpose
One row per registered device per user. Supports the device-pair model: students are allowed one mobile + one desktop device by default. The `device_id` is a platform-specific identifier (FCM token or hardware ID). Enables QR-code device-connect flow and prevents session sharing between too many devices.

#### Spec origin
§1 Core Principles (device-pair model), §3.2 Device-Connect Sign-In, feature flag `device_pairing`.

#### Row lifecycle
Created when a user registers a new device (first launch or QR connect). `last_seen` updated on every app launch. Soft-deleted (deleted_at set) when device is deregistered. `is_primary` marks the first/preferred device.

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK | — |
| user_id | uuid | NO | — | Owning user | FK → auth.users(id) ON DELETE CASCADE |
| device_id | text | NO | — | Platform device identifier | UNIQUE |
| platform | text | NO | — | OS type | CHECK IN ('ios','android','windows','macos','linux','web') |
| is_primary | boolean | YES | false | Marks the primary device | — |
| last_seen | timestamptz | YES | now() | Last app launch from this device | — |
| created_at | timestamptz | YES | now() | — | — |
| deleted_at | timestamptz | YES | NULL | Soft delete; added via §B11 | — |

#### Access patterns
- **By owning project (Flutter):** `SELECT * WHERE user_id = auth.uid()` — on app launch to check device count.
- **By admin (Flutter):** SELECT all devices for device management.

#### What this table is NOT for
- Do NOT store session tokens here — use Supabase Auth session management.
- Do NOT enforce the device limit in application code alone — it must also be enforced in a server-side function or trigger.

---

## 4. Relationships Between Tables in This Domain

`user_profiles` is the anchor. Both `admin_users` and `devices` have FKs pointing to `auth.users(id)` (not to `user_profiles.id` directly, though they are equivalent since user_profiles.id = auth.users.id). A user may or may not be an admin — having a `user_profiles` row does not imply an `admin_users` row. A user may have zero to `device_limit` rows in `devices`. The `suspended_by` FK on `user_profiles` creates a self-referential relationship indicating which admin issued a suspension.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| auth.users | Supabase Auth | PK source; user_profiles.id references auth.users.id | Any change to auth.users.id type breaks the entire schema |
| app.student | student_tutor | trg_sync_student_tier reads student.tier to update user_profiles.subscription_tier | Renaming student.tier breaks the trigger and all tier-gating |
| app.tutor_assignment | student_tutor | tutor READ policy joins tutor_assignment to determine which student profiles a tutor can see | Removing tutor_id or course_id from tutor_assignment breaks tutor READ access to student profiles |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| user_profiles | tauka-python | Reads id, subscription_tier, user_type for billing and access decisions | Column names: id, subscription_tier, user_type |
| user_profiles | Every other domain | FK target for user identity (most FK columns reference user_profiles.id or auth.users.id) | id column type (uuid) and name |
| admin_users | is_admin() function (called by every admin policy) | `SELECT 1 WHERE id = auth.uid() AND deleted_at IS NULL` | id column name, deleted_at column name |

---

## 6. Extension Rules

#### If you need a new attribute on an existing entity
Add a column to `user_profiles` directly. Do NOT create a satellite profile table. Do NOT create a user_settings key-value table. We chose wide tables because profile data is always read atomically (one row fetch per session).

#### If you need a new user-scoped preference or setting
Add a typed column to `user_profiles` with a sensible DEFAULT. Use `jsonb` only when the structure is genuinely variable (e.g., `teaching_options`).

#### If you need a new status/state for accounts
Add to the `role` CHECK constraint enum. Update the `app.is_admin()` equivalent helper function. Do NOT add boolean flag columns.

#### If you need to track account history
Create an `user_profiles_audit` table: same columns + `changed_at` + `changed_by`. Handle in application code, not triggers, to maintain explicit audit trail. Do NOT use the `user_profiles` table itself as an audit log.

#### Specifically do NOT
- Do NOT add Stripe IDs to `user_profiles` — they belong on `app.student`.
- Do NOT create a separate `user_settings` or `preferences` table — extend `user_profiles` with typed columns.
- Do NOT check admin status anywhere other than `app.is_admin()`.
- Do NOT add a `is_deleted` boolean — use `deleted_at IS NOT NULL` pattern exclusively.
- Do NOT create a `user_roles` join table — a user has exactly one role (user_type/role column).

---

## 7. Usage by tauka-react-web

> Added by: tauka-react-web on 2026-05-26

### Authentication

The web app uses the same Supabase Auth instance as the mobile app. Login is handled at `/login` via `supabase-js` (email/password + Google + Apple OAuth). Session is stored client-side — no httpOnly cookies, no custom session middleware. The Supabase JWT is passed as `Authorization: Bearer` to FastAPI for all business-logic endpoints.

Post-login redirect logic is based on `user_profiles.user_type`:
- `user_type = 'student'` → redirect to `/account`
- `user_type = 'tutor'` → redirect to `/tutor`
- `admin_users` row exists → redirect to `/admin` (future)

### `app.user_profiles` — read via supabase-js on every portal page

The web client reads `user_profiles` on initial auth to determine which portal to show. The most important columns:

| Column | Page | Purpose |
|---|---|---|
| id | all | Anchor for all supabase-js queries |
| user_type | all | Route to student vs tutor portal |
| first_name, last_name | all | Display in nav header |
| subscription_tier | /account | Tier display (cross-check with student.tier) |
| proficiency_level | /account | Display assessed level badge |
| avatar_url | all | Profile image in nav |

The web client reads `user_profiles` using the `user_profiles_select_own` policy (`auth.uid() = id AND deleted_at IS NULL`).

### What the web client does NOT do with this domain
- Does NOT write to `user_profiles.user_type` or `subscription_tier` — these are protected by the `users update own profile` RLS policy (§48 in tauka_full_schema.sql).
- Does NOT directly manage `admin_users` rows — admin access is future scope.
- Does NOT read or write `devices` — device pairing is a Flutter-only feature.
