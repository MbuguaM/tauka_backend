# Domain: AI

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5.6 AI Practice, §8 AI Chat Integration

---

## 1. Business Context

### What real-world problem does this domain solve?
AI-driven practice is a key differentiator for Tauka. Students can have open-ended language conversations with an LLM, use structured drill practice (grammar, vocabulary, pronunciation), and get in-context AI assistance within classic class chats. This domain stores the conversation and session records that make AI practice resumable, reviewable by tutors, and rate-limited.

### How does this domain fit into the larger system?
`ai_conversation` is the top-level record of a user-to-AI session. It optionally links to a classic `app.conversations` row (for the "AI assist" feature inside class chats). `ai_message` stores individual turns. `ai_practice_session` tracks structured practice runs (drill/pronunciation/conversation mode); `ai_practice_turn` logs each exchange within a session. The `app.student.ai_conversations` counter (Python-maintained) is a denormalized count, not a source of truth.

---

## 2. Design Decisions

### Key invariants
- `app.ai_message.conversation_id` — canonical column name is `conversation_id`, NOT `ai_conversation_id`. The column name `ai_conversation_id` appeared in an early draft (additional_spec.sql §12) but was SUPERSEDED. Any Dart code still using `ai_conversation_id` must be updated (divergence note D1).
- `ai_conversation` and `ai_message` use FORCE ROW LEVEL SECURITY — even the table owner cannot bypass RLS.
- Users can only access their own AI conversations; admins can read all.
- `ai_practice_session` and `ai_practice_turn` are readable by tutors (to review student practice quality).
- `ai_practice_session` supports soft delete (`deleted_at`); `ai_message` does not.

### Data flow
1. User opens AI chat → Flutter calls `ai_conversation` INSERT (or reads existing).
2. User sends message → Flutter INSERTs `ai_message` row with `role='user'`.
3. Edge Function calls LLM → Flutter INSERTs response as `ai_message` with `role='assistant'`.
4. On conversation end: Flutter updates `ai_conversation.last_message_at`.
5. For practice sessions: Flutter creates `ai_practice_session` row, then INSERTs `ai_practice_turn` rows for each exchange.

---

## 3. Tables

### Table: ai_conversation

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → auth.users(id) ON DELETE CASCADE |
| title | text | Conversation display title (user-editable or auto-generated) |
| classic_conversation_id | uuid | FK → app.conversations(id) ON DELETE SET NULL — links AI assist to a class chat |
| created_at | timestamptz | — |
| last_message_at | timestamptz | Updated on each new message for sort ordering |
| deleted_at | timestamptz | Soft delete |

#### Access patterns
- User: ALL on own rows (`user_id = auth.uid() AND deleted_at IS NULL`).
- Admin: SELECT all.

---

### Table: ai_message

**CRITICAL: Column is `conversation_id`, NOT `ai_conversation_id`.** See divergence note D1 in `tauka_full_schema.sql` header.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| conversation_id | uuid | FK → ai_conversation(id) ON DELETE CASCADE — **canonical name** |
| role | text | 'user' or 'assistant' (CHECK constraint) |
| content | text | Message text |
| shorthand_used | text | '@meaning' / '@grammar' / '@check' / etc. — which shorthand triggered this message |
| raw_input | text | Full text the user typed before AI processing |
| created_at | timestamptz | — |

No soft delete — messages are permanent within a conversation. Delete the parent `ai_conversation` to purge.

---

### Table: ai_practice_session

Structured practice runs separate from open-ended AI chat. Three modes: conversation (free-form), drill (grammar/vocab exercises), pronunciation.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| mode | text | 'conversation' / 'drill' / 'pronunciation' (CHECK constraint) |
| lesson_number | integer | Optional — which lesson number this practice ties to |
| duration_seconds | integer | Total session length |
| turns_count | integer | Number of AI exchanges in the session |
| error_count | integer | Grammar errors detected |
| vocabulary_added | integer | New words encountered |
| summary_text | text | AI-generated session summary |
| created_at | timestamptz | — |
| completed_at | timestamptz | When the session ended |
| deleted_at | timestamptz | Soft delete |

#### Access patterns
- Student: SELECT/INSERT/UPDATE own rows.
- Tutor or admin: SELECT all (for progress review).

---

### Table: ai_practice_turn

Individual exchanges within an `ai_practice_session`. Includes grammar correction metadata.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| session_id | uuid | FK → ai_practice_session(id) ON DELETE CASCADE |
| turn_index | integer | Sequence position within the session |
| role | text | 'user' or 'assistant' (CHECK constraint) |
| content | text | Turn text |
| audio_url | text | Optional: pronunciation recording URL |
| grammar_error | boolean | Whether this turn contained a grammar error |
| correction_text | text | The corrected version if grammar_error = true |
| tokens_used | integer | LLM token cost for billing/monitoring |
| created_at | timestamptz | — |

#### RLS
Access is derived from the parent session: a user can read/write turns if they own the parent `ai_practice_session`. This is implemented via an EXISTS subquery on `ai_practice_session` in the policy.

---

## 4. Relationships Between Tables in This Domain

`ai_conversation` → (many) `ai_message`. `ai_practice_session` → (many) `ai_practice_turn`. The two sub-graphs are independent: `ai_conversation` is for open-ended chat; `ai_practice_session` is for structured drills. They can co-exist for the same user but are not linked to each other.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.conversations | messaging | ai_conversation.classic_conversation_id FK | conversations.id column name and type |
| app.user_profiles | identity_access | ai_practice_session.user_id FK | user_profiles.id |
| auth.users | Supabase Auth | ai_conversation.user_id FK | auth.users.id |

### Tables in THIS domain that other projects use
None currently — this domain is self-contained.

---

## 6. Extension Rules

#### If you need to add a new AI shorthand
Add the new shorthand value to the Dart `AiShorthand` enum and document it in the spec. No DB migration needed — `shorthand_used` is a free-text column.

#### If you need to add a new practice mode
Add the new value to the `mode` CHECK constraint on `ai_practice_session`.

#### Specifically do NOT
- Do NOT use `ai_conversation_id` as a column name — the canonical name is `conversation_id` (divergence note D1).
- Do NOT create a separate `ai_practice_result` table — summary data lives on `ai_practice_session`.
- Do NOT expose `ai_message` rows to tutors — AI conversations are private to the student.
