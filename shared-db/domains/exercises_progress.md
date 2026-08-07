# Domain: Exercises & Progress

> **Owner project:** tauka-flutter (Flutter writes); tauka-python (reads for earnings/milestones)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5.1 Learn Branch — Exercises, §14 Sync Architecture, §16 Offline Behaviour

---

## 1. Business Context

### What real-world problem does this domain solve?
Students need to demonstrate learning through exercises, track pronunciation, and review flashcards using spaced repetition. This domain captures the raw data from all interactive practice: multiple-choice results, handwriting recognition sessions, Anki card reviews, voice recordings, and periodic progress snapshots. This data feeds tutor dashboards, Python milestone calculations, and the SRS scheduling algorithm.

### How does this domain fit into the larger system?
`exercise_result` is produced by the exercise pages (multichoice, blindpage, touchpage) and synced offline-first. `flashcard_reviews` drives the SRS algorithm in the Anki system. `voice_recordings` captures pronunciation attempts for AI or tutor review. `progress_snapshots` is a denormalized summary used by the tutor dashboard. `handwriting_session` records each handwriting recognition attempt for supported languages.

---

## 2. Design Decisions

### Key invariants
- `app.exercise_result` uses the **singular** table name. `app.exercise_results` (plural) does NOT exist — this resolved CONFLICT-002. `supabaseService.dart` references `exercise_results` (plural) and must be updated to match the canonical singular name.
- `app.exercise_sessions` does NOT exist — the lifecycle columns (`lesson_id`, `started_at`, `completed_at`) were added to `exercise_result` directly via §28 ALTER TABLE.
- `flashcard_reviews` has UNIQUE(user_id, card_id) — each card has one SRS state record per user (upsert pattern for SRS updates).
- `progress_snapshots` has UNIQUE(user_id, course_id) — one snapshot per course per user (upsert pattern).
- `voice_recordings.storage_path` is the Supabase Storage path; the Flutter app constructs the full URL via `FileStorage`.

### Data flow
**Exercise result sync:**
1. Student completes exercise locally → Flutter builds `ExerciseResult` in memory.
2. On reconnect: `SupabaseService` upserts rows into `exercise_result` with `synced_at = now()`.
3. Python reads results to compute milestones and update `app.student.lessons_completed`.

**Flashcard SRS:**
1. Student reviews a card → Flutter updates local SRS state.
2. On sync: Flutter upserts `flashcard_reviews` row (UNIQUE(user_id, card_id) → ON CONFLICT UPDATE).
3. Next review `due_date` stored in DB for cross-device SRS continuity.

---

## 3. Tables

### Table: exercise_result

**CANONICAL NAME: `exercise_result` (singular).** See divergence note D2.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) |
| unit_id | uuid | FK → app.unit(id) — optional |
| session_title | text | Display name of the exercise session |
| exercise_type | text | Type of exercise (multichoice, blindpage, touchpage, etc.) |
| difficulty_mode | text | 'easy' / 'hard' / 'extraHard' (CHECK constraint) |
| score | integer | Number of correct answers |
| total | integer | Total questions |
| elapsed_seconds | integer | Time taken |
| answers | jsonb | Raw answer data |
| submitted_at | timestamptz | When submitted |
| synced_at | timestamptz | When synced to DB (null = pending sync) |
| deleted_at | timestamptz | Soft delete |
| lesson_id | uuid | FK optional — added via §28 ALTER TABLE |
| started_at | timestamptz | When session started — added via §28 |
| completed_at | timestamptz | When session completed — added via §28 |

#### RLS
- Student: INSERT and SELECT own rows.
- Tutor: SELECT rows for assigned students only (via `app.student.tutor_id` subquery — fixed in §28 to replace broken §B8 policy).
- Admin: SELECT all.

---

### Table: handwriting_session

Records each handwriting recognition attempt. Currently supports Amharic, Arabic, and Chinese.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) |
| language_code | text | 'am' / 'ar' / 'zh' (CHECK constraint) |
| strokes_count | integer | Number of strokes drawn |
| recognition_result | text | What the recognizer returned |
| confidence | double precision | Recognizer confidence score |
| correct_answer | text | The expected answer |
| is_correct | boolean | Whether recognition matched expected |
| created_at | timestamptz | — |

#### RLS
- User: INSERT and SELECT own rows.
- Admin: SELECT all.

---

### Table: flashcard_reviews

SRS state for each card per user. UNIQUE(user_id, card_id) ensures one record per card — updates use ON CONFLICT DO UPDATE.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| card_id | uuid | ID of the Anki card (references anki_deck.deck JSON inline — no FK) |
| deck_id | uuid | Optional: which deck the card belongs to |
| reviewed_at | timestamptz | Last review timestamp |
| grade | integer | SM-2 grade 0–5 (CHECK constraint) |
| interval_days | integer | Days until next review |
| ease_factor | numeric(4,2) | SM-2 ease factor, default 2.50 |
| due_date | date | Next scheduled review date |

UNIQUE(user_id, card_id) — upsert pattern for SRS updates.

#### RLS
- Student: ALL on own rows only.

---

### Table: voice_recordings

Pronunciation recordings submitted by students for AI or tutor review.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| lesson_id | uuid | Optional — which lesson this recording is for |
| storage_path | text | Supabase Storage path to audio file |
| duration_ms | integer | Recording duration in milliseconds |
| phoneme_score | numeric(4,2) | Pronunciation accuracy score 0–100 (optional; set by AI) |
| created_at | timestamptz | — |

#### RLS
- Student: INSERT own; SELECT own.
- Tutor or admin: SELECT all.

---

### Table: progress_snapshots

Denormalized course-level progress summary. UNIQUE(user_id, course_id) — one snapshot per course. Used by the tutor dashboard for cohort health views.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| course_id | uuid | Which course (no FK — course may be soft-deleted) |
| lessons_completed | integer | Total lessons completed in this course |
| accuracy | numeric(5,4) | Overall accuracy 0–1 |
| created_at | timestamptz | When snapshot was taken |

UNIQUE(user_id, course_id) — upsert pattern.

#### RLS
- Student: ALL on own rows (upsert).
- Tutor or admin: SELECT all.

---

## 4. Relationships Between Tables in This Domain

All five tables are independently linked to `user_id` only — they do not form a chain. `exercise_result` optionally links to `unit` (course_content domain). `flashcard_reviews.card_id` conceptually references `anki_deck.deck` JSON entries but has no DB FK (inline JSON). `voice_recordings` and `handwriting_session` are standalone telemetry.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.unit | course_content | exercise_result.unit_id FK | unit.id |
| app.user_profiles | identity_access | All tables reference user_profiles.id | user_profiles.id |
| app.student | student_tutor | exercise_result RLS tutor policy uses student.tutor_id | student.tutor_id, student.tutor_ended_at |

---

## 6. Extension Rules

#### If you need a new exercise type
Add the new `exercise_type` value in the Dart `ExerciseType` enum. No DB migration needed — `exercise_type` is a free-text column.

#### If you need to add new SRS algorithm fields
Add columns to `flashcard_reviews`. The SM-2 algorithm state (interval, ease_factor, due_date) is already present; new fields for SM-5 or FSRS can be added without breaking existing rows.

#### Specifically do NOT
- Do NOT create `app.exercise_results` (plural) — the canonical name is `exercise_result` (singular) (CONFLICT-002).
- Do NOT create `app.exercise_sessions` — lifecycle columns (started_at, completed_at, lesson_id) live on `exercise_result` (§28 ALTER TABLE).
- Do NOT create `app.flashcard_review_entry` — all SRS state lives on `flashcard_reviews`.
