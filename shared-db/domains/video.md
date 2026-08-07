# Domain: Video

> **Owner project:** tauka-flutter (schema); tauka-python (reads tutor_sessions for earnings)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5 Feature Specifications — Video Sessions, §B7, §6 Tutor Features

---

## 1. Business Context

### What real-world problem does this domain solve?
Live video sessions are a core premium feature: students book 1:1 or group sessions with tutors, attend them via an embedded video SDK, and receive notes and ratings afterward. The live vendor (Daily.co, Agora, or LiveKit) is configurable at runtime so the platform can switch providers without an app update. Post-session records feed into the tutor earnings calculation.

### How does this domain fit into the larger system?
`video_vendor_config` is read by the Flutter client at startup to select the active SDK. `video_session` is the scheduled session record created by the tutor; `video_session_participant` records enrolled students. After a session, tutors write notes via `video_session_note`, students submit ratings via `video_session_rating`, and recordings may be stored in `session_recordings`. `tutor_sessions` is a separate, Python-readable summary table used for earnings calculation — it is NOT the same as `video_session`.

### Key distinctions
- `app.video_session` = scheduled session (Flutter creates/reads; student attends)
- `app.tutor_sessions` = completed session log (Flutter creates; Python reads for payroll)
- `app.video_sessions` (PLURAL) does NOT exist — never create it (CONFLICT-006).

---

## 2. Design Decisions

### Architecture chosen
The vendor abstraction (`video_vendor_config`) is a single-active-row table: only one row may have `is_active = true` at a time, enforced by a partial unique index. The Flutter app reads this once on startup via `VideoService` and caches the result. This allows switching from Daily.co to Agora without releasing a new app version.

### Key invariants
- Only one `video_vendor_config` row may be `is_active = true AND deleted_at IS NULL` (partial UNIQUE index enforces this).
- `video_session.tutor_id` FK references `app.tutor(id)` — only active tutors can create sessions.
- `video_session_participant` is a composite-PK junction (session_id, student_id) — no duplicate enrollments.
- `video_session_note` and `video_session_rating` have UNIQUE(session_id, student_id) — one note/rating per student per session.
- `tutor_sessions.tutor_id` references `auth.users(id)`, NOT `app.tutor(id)` — this is intentional so historical records survive if a tutor row is soft-deleted.

---

## 3. Tables

### Table: video_vendor_config

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| vendor | text | 'daily' / 'agora' / 'livekit' (CHECK constraint) |
| is_active | boolean | True for the live vendor; partial UNIQUE prevents two active rows |
| config_json | jsonb | Vendor-specific SDK configuration (room URL patterns, API keys) |
| deleted_at | timestamptz | Soft delete |
| updated_at | timestamptz | Auto-updated by trigger |

#### Access patterns
- All authenticated users: SELECT the active vendor row on startup.
- Write: service_role only (no direct admin write from Flutter; managed by Edge Functions or service API).

---

### Table: video_session

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → app.tutor(id) — session owner |
| title | text | Session display name |
| scheduled_at | timestamptz | When the session is scheduled to start |
| duration_minutes | int | Expected duration |
| room_url | text | Vendor-provided room URL |
| deleted_at | timestamptz | Soft delete |

#### RLS
- Tutors: ALL on own sessions (`tutor_id = auth.uid()`).
- Students: SELECT where they appear in `video_session_participant`.
- Admins: SELECT all.

---

### Table: video_session_participant

| Column | Type | Purpose |
|---|---|---|
| session_id | uuid | FK → video_session(id) ON DELETE CASCADE — composite PK |
| student_id | uuid | FK → app.student(id) — composite PK |

#### RLS
- Students: SELECT own rows (`student_id = auth.uid()`).

---

### Table: video_session_rating

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| session_id | uuid | FK → video_session(id) ON DELETE CASCADE |
| student_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| stars | smallint | 1–5 rating (CHECK constraint) |
| feedback | text | Optional text feedback |
| created_at | timestamptz | — |

UNIQUE(session_id, student_id) — one rating per student per session.

#### RLS
- Student: SELECT own; INSERT for self.
- Tutor or admin: SELECT all.

---

### Table: video_session_note

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| session_id | uuid | FK → video_session(id) ON DELETE CASCADE |
| student_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| tutor_id | uuid | FK → user_profiles(id) ON DELETE SET NULL |
| body | text | Note content |
| lesson_completed | boolean | Whether the planned lesson was completed |
| created_at | timestamptz | — |
| updated_at | timestamptz | — |

UNIQUE(session_id, student_id) — one note per student per session.

#### RLS
- Student: SELECT own.
- Tutor: SELECT and write own (`tutor_id = auth.uid()`).
- Admin: SELECT all.

---

### Table: session_recordings

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| session_id | uuid | FK → video_session(id) ON DELETE CASCADE |
| storage_path | text | Supabase Storage path to recording file |
| duration_s | int | Recording length in seconds |
| created_at | timestamptz | — |

#### RLS
- Participant (student or tutor): SELECT if they were in the session.
- Admin: SELECT all.

---

### Table: tutor_sessions

Completed teaching sessions — source of truth for Python earnings calculation. Distinct from `video_session` which tracks scheduled/live sessions.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → auth.users(id) — intentionally not app.tutor to survive soft-delete |
| student_ids | uuid[] | Array of participating student UUIDs |
| student_count | int | Number of students in the session |
| session_type | text | 'cohort' or other session type |
| duration_minutes | int | Actual session length |
| started_at | timestamptz | When the session started |
| ended_at | timestamptz | When the session ended |
| topic | text | Subject of the session |
| notes | text | Tutor notes |
| created_at | timestamptz | — |

#### Access patterns
- Tutor: SELECT own rows.
- Python backend: reads via service role to calculate `get_tutor_monthly_earnings()` RPC.

---

## 4. Relationships Between Tables in This Domain

`video_session` → (participants) `video_session_participant` → (students). Post-session: `video_session_rating` and `video_session_note` both reference `video_session` + a student. `session_recordings` stores media artifacts for a session. `video_vendor_config` is independent — read by the Flutter SDK selector, not joined to sessions.

`tutor_sessions` is a separate tracking table (not joined to `video_session`) used for payroll aggregation by the Python backend via the `get_tutor_monthly_earnings()` RPC.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.tutor | student_tutor | video_session.tutor_id FK | Renaming tutor.id breaks session creation |
| app.student | student_tutor | video_session_participant.student_id FK | — |
| app.user_profiles | identity_access | video_session_note, video_session_rating reference user_profiles.id | — |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| app.video_session | tutor_management | payroll_line_item.session_id FK | video_session.id |
| app.tutor_sessions | tauka-python | `get_tutor_monthly_earnings()` RPC aggregates this table | tutor_id, started_at, per_session_rate_cents via app.tutor |

---

## 6. Extension Rules

#### If you need to add session metadata
Add a column to `video_session`. Do NOT create a `video_session_metadata` satellite table.

#### If you need a new video vendor
Add a new value to the `vendor` CHECK constraint and insert a new `video_vendor_config` row (with `is_active = false`). Update `VideoService` in Flutter to handle the new vendor.

#### Specifically do NOT
- Do NOT create `app.video_sessions` (plural) — `app.video_session` (singular) is canonical (CONFLICT-006).
- Do NOT create `app.session_ratings` as a standalone table — use `app.video_session_rating`.
- Do NOT store vendor API keys directly in the `config_json` — use Supabase Vault or Edge Function secrets.
