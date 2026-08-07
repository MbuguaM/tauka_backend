# Conflict Log
> When a project detects a conflict, mismatch, or cross-project need, log it here.
> Conflicts must be resolved by humans before merging back.

## Active Conflicts

_None at this time. All schema conflicts present during initial development have been resolved. See Resolved Conflicts below._

---

## Resolved Conflicts

### CONFLICT-001: ai_message column name — conversation_id vs ai_conversation_id
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** COLUMN_MISMATCH
- **Details:**
  `additional_spec.sql §12` used the column name `ai_conversation_id` for the FK in `app.ai_message`.
  `full_database_definitions.sql` and the canonical schema use `conversation_id`.
  The Dart service `aiChatStateManager.dart` was referencing the wrong column name.
- **Resolution:** `conversation_id` is the authoritative column name in `app.ai_message`. All Dart code updated to use `conversation_id`. The `ai_conversation_id` name is deprecated and must never be re-introduced.

---

### CONFLICT-002: Exercise result table name — singular vs plural
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** NAMING_COLLISION
- **Details:**
  `additional_spec.sql §10` created `app.exercise_result` (singular).
  `supabaseService.dart` was calling the table `exercise_results` (plural).
  The spec referenced both `exercise_sessions` (an unrealised alternative design) and `exercise_result`.
- **Resolution:** `app.exercise_result` (singular) is the canonical table name. `supabaseService.dart` updated. `exercise_sessions` was never created.

---

### CONFLICT-003: subscription_tier value spelling — tutor vs tutor_tier
- **Detected by:** tauka-python
- **Date:** 2026-05-09
- **Type:** COLUMN_MISMATCH
- **Details:**
  Multiple locations in earlier SQL files used the value `'tutor_tier'` in CHECK constraints and seed data.
  The Python backend's canonical tier enum uses `'tutor'`.
  This caused tier comparisons to silently fail for tutor-tier students.
- **Resolution:** All CHECK constraints, seed data, and application code use `'tutor'` (not `'tutor_tier'`). The valid tier values are: `free | learner | tutor | intensive`.

---

### CONFLICT-004: Stripe data ownership — user_profiles vs student
- **Detected by:** tauka-python
- **Date:** 2026-05-09
- **Type:** OWNERSHIP_DISPUTE
- **Details:**
  Early spec versions proposed adding `stripe_customer_id` and `stripe_subscription_id` to `app.user_profiles`.
  The Python backend maintains these as authoritative in `app.student`.
  Duplicating them on `user_profiles` would create two sources of truth with no sync mechanism.
- **Resolution:** `stripe_customer_id`, `stripe_subscription_id`, `subscription_status`, and `current_period_end` live ONLY on `app.student`. The Flutter client reads tier via `app.user_profiles.subscription_tier` (kept in sync by trigger). Flutter MUST NOT read Stripe IDs directly.

---

### CONFLICT-005: Dual streak tracking — student vs user_profiles
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** IMPLICIT_CONTRACT
- **Details:**
  `app.student.current_streak` is written by the Python backend after server-side validation (14-day milestone detection, cron jobs).
  `app.user_profiles.streak_days` is the Flutter client's optimistic local streak — updated by the `app.update_user_streak()` RPC on each study session.
  Both columns exist by design for different purposes.
- **Resolution:** Both columns are kept. Document authority: `app.student.current_streak` = Python-validated, milestone-aware streak. `app.user_profiles.streak_days` = Flutter client-side optimistic streak. The Flutter client MUST NOT write `app.student.current_streak`.

---

### CONFLICT-006: video_session singular vs plural + session_ratings naming
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** NAMING_COLLISION
- **Details:**
  Multiple SQL files proposed `app.video_sessions` (plural) and `app.session_ratings` (non-standard names).
  The canonical schema uses `app.video_session` (singular, §B7) and `app.video_session_rating` (§19).
  Creating both would cause FK ambiguity and double the migration surface.
- **Resolution:** `app.video_session` (singular) and `app.video_session_rating` are canonical. The plural variants were never created.

---

### CONFLICT-007: yt_playlist/yt_video FK default bug (D6)
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** SCHEMA_REQUEST
- **Details:**
  `app.yt_playlist.user_id` has `DEFAULT gen_random_uuid()` which produces FK violations when user_id is not explicitly supplied (the default value is not a valid `auth.users.id`). Same issue on `app.yt_video.playlist_id`.
  This default is a copy-paste error from the uuid PK pattern applied to an FK column.
- **This project assumes:** Flutter always supplies `user_id` explicitly on insert. The default never fires in current use.
- **Resolution needed:** Apply Amendment A6: `ALTER TABLE app.yt_playlist ALTER COLUMN user_id DROP DEFAULT; ALTER TABLE app.yt_video ALTER COLUMN playlist_id DROP DEFAULT;` — scheduled as next maintenance migration.

---

### CONFLICT-008: soft_delete() security vulnerability (D5)
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** SCHEMA_REQUEST
- **Details:**
  The original `app.soft_delete()` and `app.restore_deleted()` functions in `full_database_definitions.sql §8` had no admin guard, allowing any authenticated user to soft-delete any row in any `app`-schema table.
- **Resolution:** Admin-guarded versions in `rls_policies.sql §0` replace the originals and are the canonical versions in this schema. The replacement functions check `app.is_admin()` before executing.

---

### CONFLICT-009: achievements table duplicate definition
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** DUPLICATE_TABLE
- **Details:**
  `app.achievements` and `app.user_achievements` were defined in two places in `additional changes.sql` (§28 and §35), with slightly different xp_reward values and descriptions.
- **Resolution:** Single authoritative definition in `tauka_full_schema.sql §39`. `ON CONFLICT (key) DO UPDATE` ensures re-runs refresh descriptions. Duplicate §28/§35 blocks removed.

---

### CONFLICT-010: tutor_availability / tutor_payout_settings FK target
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** FK_MISMATCH
- **Details:**
  Early versions had `tutor_availability.tutor_id` and `tutor_payout_settings.tutor_id` referencing `auth.users(id)`. This allows any authenticated user to insert a tutor_availability row for themselves even if they are not a tutor.
- **Resolution ([R-1]):** Both FKs reference `app.tutor(id)`, enforcing that only existing tutor records can have availability or payout settings.

---

### CONFLICT-011: notifications sender_id impersonation bug
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** IMPLICIT_CONTRACT
- **Details:**
  The original `notifications_insert_teacher_or_admin` policy allowed a teacher to set `sender_id` to any UUID, enabling impersonation of another teacher or admin in notification content.
- **Resolution:** Policy updated with `sender_id = auth.uid()` guard in the `WITH CHECK` clause.

---

### CONFLICT-012: tutor_assignment.student_id misleading column name
- **Detected by:** tauka-flutter
- **Date:** 2026-05-09
- **Type:** IMPLICIT_CONTRACT
- **Details:**
  `app.tutor_course` has a column named `student_id` that actually stores the tutor's UUID (FK → `app.tutor`). This is a historical naming error that was left in place.
  Amendment A2 in Part C of the schema recommends renaming the column to `tutor_id`.
- **This project assumes:** Queries to `app.tutor_course` must remember `student_id` contains the tutor's ID. The confusing name is documented.
- **Resolution needed:** Rename `app.tutor_course.student_id` → `tutor_id` via a migration. Blocked on ensuring no application code directly references this column name without going through a SupabaseService abstraction.
