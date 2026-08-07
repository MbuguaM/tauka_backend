# Domain: Course & Content

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5.1 Learn Branch, §5.2 Dictionary Branch, §5.3 Flashcards, §14 Sync Architecture, §16 Offline Behaviour

---

## 1. Business Context

### What real-world problem does this domain solve?
Tauka's core value proposition is structured language lessons that students can work through offline, with a built-in dictionary and flashcard system. This domain manages the curriculum hierarchy (course → unit → lesson), the dictionary content, Anki flashcard decks, and the infrastructure for tutors to propose content corrections and admins to publish new content. It is the foundation that makes "content is owned locally" possible.

### How does this domain fit into the larger system?
Courses contain units; units contain lesson JSON files stored in Supabase Storage (referenced by `unit.json_file` partial path). Students enroll in courses via `student_course` (see [[student_tutor]]). Unit completion is tracked in `unit_progress`. The dictionary and anki_deck tables are course-scoped and gated by enrollment. Content versioning (`content_versions`) enables offline sync conflict detection.

### User stories this domain serves
- As a student, I enroll in a course and my device downloads all unit JSON files, dictionary, and Anki decks.
- As a student, I can work through lessons offline; my progress syncs when I reconnect.
- As a student, I can look up words in the course dictionary and study flashcards from Anki decks.
- As a tutor, I can flag a content error in a unit (course_correction) for admin review.
- As a tutor, I can assign an Anki deck to a student or class.
- As an admin, I can publish new units and update content versions to trigger client re-downloads.

---

## 2. Design Decisions

### Architecture chosen
Content lives in Supabase Storage (JSON files for units, audio files); the DB stores only metadata and partial paths. This keeps the DB lean and enables CDN-level delivery for content files. Dictionary entries and Anki cards are stored as inline JSON arrays (`dict` and `deck` columns) rather than as normalized rows — because entries are always read as a whole collection and never queried individually by the DB.

### Key invariants
- `app.dictionary_entry` does NOT exist — entries live in `dictionary.dict` JSON array.
- `app.word_pair` does NOT exist — cards live in `anki_deck.deck` JSON array.
- `unit.json_file` and `unit.audio_folder` are PARTIAL paths; the full Storage URL is constructed by `FileStorage` in the Flutter app.
- `unit_progress` supersedes the legacy `progress` table. New code must use `unit_progress`.
- `anki_deck_assignment` target: at least one of `assigned_to_user_id` or `assigned_to_class_id` must be non-null (CHECK constraint).
- `content_versions` has UNIQUE(entity_type, entity_id) — one version record per entity.

### Data flow
**Enrollment and download:**
1. Student enrolls → `student_course` row created.
2. Flutter downloads `course/toc.json` → gets list of unit IDs.
3. Flutter downloads each `unit.json_file` → saves locally at `courses/<courseId>/units/<unitId>.json`.
4. Flutter downloads `dictionary.dict` → saves locally.
5. Flutter downloads `anki_deck.deck` → saves locally.

**Offline study:**
- Student works through lessons locally; `unit_progress.sections_completed` updated optimistically.
- On reconnect: `upsert_unit_progress()` SECURITY DEFINER RPC syncs progress to DB.
- `content_versions` checked: if local version < DB version, re-download triggered.

---

## 3. Tables — Detailed Specification

### Table: course

#### Purpose
Top-level curriculum container. Each course targets a specific language/speaker combination. `toc_url` points to the table-of-contents JSON listing all unit IDs. The `dict_url` array is deprecated — dictionary content now lives in `app.dictionary`.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| created_at | timestamptz | NO | now() | — |
| title | text | YES | NULL | Course display name |
| speaker_for | text | YES | NULL | Language this course teaches (e.g., 'amharic') |
| unit_count | bigint | YES | NULL | Denormalized unit count for display |
| dict_url | text[] | YES | NULL | DEPRECATED — use app.dictionary |
| cover_url | text | YES | NULL | Cover image URL |
| toc_url | text | YES | NULL | Partial Storage path to toc.json |
| deleted_at | timestamptz | YES | NULL | Soft delete |

---

### Table: unit

#### Purpose
A single lesson unit within a course. `json_file` is the partial Storage path to the unit's content JSON. `audio_folder` is the partial path prefix for all audio files in this unit. `unit_order` determines display sequence. `is_preliminary` marks units shown before numbered units (e.g., alphabet, intro). §B2 adds `content_version` and `content_updated_at` for sync conflict detection.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| created_at | timestamptz | NO | now() | — |
| title | text | YES | NULL | Unit display name |
| json_file | text | YES | NULL | Partial Storage path to unit content JSON |
| audio_folder | text | YES | NULL | Partial Storage path prefix for audio files |
| course_id | uuid | YES | NULL | FK → course(id) |
| unit_order | integer | YES | 0 | Sort order within course |
| section_type | text | YES | 'content' | Type of unit content |
| is_preliminary | boolean | NO | false | True for pre-numbered units (alphabet, intro) |
| deleted_at | timestamptz | YES | NULL | Soft delete |
| content_version | integer | YES | 1 | Incremented when content is updated (§B2) |
| content_updated_at | timestamptz | YES | NULL | When content was last updated (§B2) |

---

### Table: unit_progress

#### Purpose
Tracks each student's progress through each unit. Supersedes the legacy `progress` table. Per-section completion state lives in `sections_completed` JSONB. The `upsert_unit_progress()` SECURITY DEFINER RPC provides idempotent sync.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| unit_id | uuid | NO | — | FK → unit(id) ON DELETE CASCADE |
| user_id | uuid | NO | — | FK → auth.users(id) ON DELETE CASCADE |
| course_id | uuid | NO | — | FK → course(id) ON DELETE CASCADE (for tutor RLS) |
| status | text | NO | 'not_started' | not_started/in_progress/completed |
| completion_percentage | numeric(5,2) | YES | 0.0 | 0–100% |
| sections_completed | jsonb | YES | {} | Map of section_id → completion state |
| started_at | timestamptz | YES | NULL | When student first opened this unit |
| completed_at | timestamptz | YES | NULL | When all sections were completed |
| last_accessed_at | timestamptz | YES | now() | Last time student touched this unit |

---

### Table: progress (LEGACY)

#### Purpose
Original progress tracking table. Superseded by `unit_progress`. Keep until all Dart code migrated. Do NOT add new features to this table.

---

### Table: dictionary

#### Purpose
Course-scoped dictionary. The entire dictionary for a course is a single row with a `dict` JSON column containing an array of `DictionaryEntry` objects. This choice was made because the dictionary is always read as a complete collection — normalized rows would require a full table scan anyway, and JSON allows richer entry structure without schema migrations.

**CRITICAL:** `app.dictionary_entry` does NOT exist. Never create it. All entries are in `dictionary.dict`.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| title | text | NO | — | Dictionary display name |
| created_by | uuid | YES | NULL | FK → auth.users(id) |
| main_lang | text | NO | — | Source language code |
| target_lang | text | NO | — | Target language code |
| course_id | uuid | NO | — | FK → course(id) ON DELETE CASCADE |
| dict | json | NO | — | Array of DictionaryEntry: {id, word, translation, pronunciation, part_of_speech, example, audio_url} |
| created_at | timestamptz | YES | now() | — |
| deleted_at | timestamptz | YES | NULL | Soft delete |

#### Access patterns
Only enrolled students and their assigned tutors can read via `dictionary_select_enrolled_or_tutor` RLS policy.

---

### Table: anki_deck

#### Purpose
Flashcard deck for spaced-repetition study. Like `dictionary`, the entire deck is stored as a JSON array in the `deck` column. Enrolled students and assigned tutors can read; tutors can assign decks to students or classes via `anki_deck_assignment`.

**CRITICAL:** `app.word_pair` does NOT exist. Cards are in `anki_deck.deck`.

---

### Table: anki_deck_assignment

#### Purpose
Allows tutors to assign a deck to a specific student or to an entire class. At least one of `assigned_to_user_id` or `assigned_to_class_id` must be non-null.

---

### Table: course_correction

#### Purpose
Tutors can flag a specific field in a unit for correction. `field_path` is a JSON path string (e.g., `sections[2].content[0].text`). Admin reviews and approves/rejects. Approved corrections are applied to the unit content and a new `content_version` is published.

---

### Table: content_versions

#### Purpose
Single-row-per-entity version counter for courses, units, and lessons. The Flutter client compares its locally-cached version against the DB version on each sync to decide whether to re-download content. UNIQUE(entity_type, entity_id) ensures one record per entity.

---

### Table: program
Currently unused. Planned for structured course scheduling (lessons per week, duration). Do NOT build features on this table until the program feature is spec'd.

### Table: contemporary
Currently unused. The Explore feature uses `yt_playlist`/`yt_video` instead. Candidate for dropping.

---

## 4. Relationships Between Tables in This Domain

`course` → (many) `unit` → (tracked by) `unit_progress` (per student per unit). `course` → (one) `dictionary` → (inline) `DictionaryEntry[]`. `course` → (many) `anki_deck` → (inline) `WordPair[]`. Tutors can flag `unit` issues via `course_correction`. `content_versions` tracks the version of each `course`/`unit`/`lesson` entity.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.student_course | student_tutor | dictionary and anki_deck RLS policies join student_course to check enrollment | student_course column names: student_id, course_id |
| app.tutor_assignment | student_tutor | dictionary and anki_deck RLS policies check tutor assignment | tutor_assignment column names: tutor_id, course_id, deleted_at |

---

## 6. Extension Rules

#### If you need to add new fields to a dictionary entry
Modify the `DictionaryEntry` structure in Dart models (`mainModels.dart`). The `dict` JSON column is untyped — no DB migration needed. Ensure old entries without the new field are handled by nullable parsing.

#### If you need to normalize dictionary entries (one row per word)
This would be a breaking schema change. Create a `CONFLICT_LOG.md` entry as a SCHEMA_REQUEST before proceeding. The migration must include a data migration of all existing `dict` JSON arrays.

#### Specifically do NOT
- Do NOT create `app.dictionary_entry` — entries live in `dictionary.dict` JSON.
- Do NOT create `app.word_pair` — cards live in `anki_deck.deck` JSON.
- Do NOT use the legacy `progress` table for new progress tracking — use `unit_progress`.
- Do NOT store content files (JSON, audio) in the database — use Supabase Storage with the `unit.json_file` partial path pattern.
