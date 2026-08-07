# Domain: Content Governance

> **Owner project:** tauka-flutter (Flutter submits; admin reviews)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §6 Tutor Features — Content Submission, §5.2 Dictionary — Contributions

---

## 1. Business Context

### What real-world problem does this domain solve?
Tauka's content quality depends on a review pipeline. Tutors and advanced students can propose new lessons, units, dictionary entries, or Explore content — but nothing goes live without admin approval. Dictionary contributions solve the "missing word" problem by letting the community extend course dictionaries with community-sourced entries. This domain provides the submission queue and approval workflow for both general content and dictionary-specific contributions.

### How does this domain fit into the larger system?
`content_approval_requests` is the general submission queue: any content type (lesson, unit, dictionary entry, explore content) can be submitted by a tutor or admin. `dictionary_contributions` is a specialized queue for community-sourced dictionary words targeting Amharic or Arabic specifically. Both tables follow a pending → approved/rejected → published workflow. Approved dictionary contributions are eventually incorporated into the relevant `app.dictionary.dict` JSON array by an admin; approved content requests are applied to `app.unit` or other entities.

---

## 2. Design Decisions

### Key invariants
- Only tutors or admins can submit `content_approval_requests` (INSERT policy: `is_tutor() OR is_admin()`).
- `content_approval_requests.content_type` CHECK constraint: `'lesson' | 'unit' | 'dictionary_entry' | 'explore_content'`.
- `dictionary_contributions.language` CHECK constraint: `'amharic' | 'arabic'` — only supported languages.
- Both tables have `reviewed_by` and `reviewed_at` columns populated by admin on approval/rejection.
- `content_approval_requests.payload` is JSONB — structure depends on `content_type`. No fixed schema.
- Approved dictionary contributions must be manually applied to `app.dictionary.dict` by admin — there is no automatic merge trigger.

---

## 3. Tables

### Table: content_approval_requests

General content submission queue for tutors and admins.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| contributor_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| content_type | text | 'lesson' / 'unit' / 'dictionary_entry' / 'explore_content' (CHECK constraint) |
| content_id | uuid | Optional — ID of existing entity being modified |
| title | text | Display title of the submission |
| description | text | What was changed or added |
| payload | jsonb | Full content payload (structure varies by content_type) |
| status | text | 'pending' → 'approved' / 'rejected' → 'published' |
| reviewed_by | uuid | FK → user_profiles(id) ON DELETE SET NULL — admin reviewer |
| reviewed_at | timestamptz | When reviewed |
| review_note | text | Admin review note |
| created_at | timestamptz | — |

#### RLS
- Contributor: SELECT own submissions; INSERT own (tutor or admin role required).
- Admin: SELECT all; UPDATE (to approve/reject/publish).

---

### Table: dictionary_contributions

Community dictionary word submissions for Amharic and Arabic.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| contributor_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| word | text | The word being contributed |
| transliteration | text | Optional romanized transliteration |
| definition | text | Word definition |
| example_sentence | text | Usage example |
| audio_path | text | Optional Storage path for pronunciation audio |
| language | text | 'amharic' or 'arabic' (CHECK constraint) |
| status | text | 'pending' / 'approved' / 'rejected' |
| reviewed_by | uuid | FK → user_profiles(id) ON DELETE SET NULL |
| reviewed_at | timestamptz | — |
| created_at | timestamptz | — |

#### RLS
- Contributor: SELECT own contributions; INSERT own.
- Admin: SELECT all; UPDATE (to approve/reject).

---

## 4. Relationships Between Tables in This Domain

`content_approval_requests` and `dictionary_contributions` are independent parallel queues. Both share the same approval lifecycle but serve different content types. Approved `dictionary_contributions` are consumed by admin to update `app.dictionary.dict` (course_content domain) — there is no automatic FK join.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.user_profiles | identity_access | contributor_id and reviewed_by FKs | user_profiles.id |
| app.dictionary | course_content | Approved contributions merged into dictionary.dict manually | dictionary.id, dictionary.dict structure |
| app.unit | course_content | content_approval_requests.content_id may reference unit.id | unit.id |

### Tables in THIS domain that other projects use
None — this domain is a consumer; no other domain has FK dependencies on it.

---

## 6. Extension Rules

#### If you need a new content type for approval requests
Add the new value to the `content_type` CHECK constraint on `content_approval_requests`.

#### If you need to support a new dictionary language
Add the language value to the `language` CHECK constraint on `dictionary_contributions`. Also add a corresponding `app.languages` entry (see [[language_registry]]).

#### If you need automated merge of approved dictionary contributions
Implement a PostgreSQL trigger or Edge Function on `dictionary_contributions` UPDATE (when `status` changes to `'approved'`) that appends the contribution to the appropriate `app.dictionary.dict` JSON array. Document the trigger here and in the course_content domain.

#### Specifically do NOT
- Do NOT auto-publish contributions without admin review — the status must pass through 'approved' first.
- Do NOT create a `dictionary_entry` table — entries live in `app.dictionary.dict` JSON array (course_content domain invariant).
- Do NOT store large binary files (audio) in the `payload` JSONB — store in Supabase Storage and reference via `audio_path`.
