# Domain: Language Registry

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §1 Product Overview (language targeting)

---

## 1. Business Context

### What real-world problem does this domain solve?
Tauka targets multiple languages (Amharic, Arabic, and others). To support filtering, matching, and display of language names consistently across the platform, a normalized language reference table is needed. Without it, language codes would be stored as arbitrary strings with no validation or canonical naming.

### How does this domain fit into the larger system?
**NOTE: As of the current implementation, neither `app.languages` nor `app.user_languages` is referenced by any Dart service in the Flutter app.** Language data currently lives in `app.user_profiles.known_languages` (JSONB array of language code strings). These tables exist for a planned future normalization. Keep them if a structured language registry is planned; they are safe to drop if that is never built.

---

## 2. Design Decisions

### Architecture chosen
Simple two-table normalized registry: `languages` is a reference list; `user_languages` is the per-user association with proficiency levels. Currently unused by application code.

### Key invariants
- `languages.code` is UNIQUE — no duplicate language codes.
- `user_languages.type` is `'known'` or `'target'` — a user can have multiple languages of each type.
- UNIQUE(user_id, language_id, type) prevents duplicate associations.

---

## 3. Tables

### Table: languages
Reference list of supported languages. Seeded with 12 languages (en, es, fr, de, zh, ja, ko, ar, pt, ru, hi, it).

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| code | varchar(10) | ISO language code (UNIQUE) |
| name | varchar(100) | Human-readable language name |
| created_at | timestamptz | — |

### Table: user_languages
Per-user language profile. Multiple rows per user (one per language type).

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → auth.users(id) ON DELETE CASCADE |
| language_id | uuid | FK → languages(id) ON DELETE CASCADE |
| type | varchar(20) | 'known' or 'target' |
| proficiency | varchar(50) | Self-reported proficiency |
| target_proficiency | varchar(50) | Goal proficiency level |
| current_proficiency | varchar(50) | Assessed current proficiency |
| is_primary | boolean | Primary language for the type |
| learning_since | date | When the user started learning |
| created_at | timestamptz | — |
| deleted_at | timestamptz | Soft delete |

---

## 4. Cross-Domain Dependencies
None currently — this domain is not referenced by any other domain's RLS policies or FKs.

---

## 6. Extension Rules

#### If you need to add a new supported language
INSERT a new row into `app.languages`. No code changes required.

#### Specifically do NOT
- Do NOT create a per-language content table — content is associated via the `course` table's `speaker_for` column.
- Do NOT delete the seeded languages — other tables may store language codes as text references.
