# Domain: Broadcast

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5.4 Chat Branch — Broadcasts, §15 Notification System

---

## 1. Business Context

### What real-world problem does this domain solve?
Tutors and admins need a way to publish announcements, tips, or content to groups of students beyond direct messaging. The broadcast system is a feed-style channel: posts appear in the Chat tab, students can react and comment, and read status is tracked for engagement analytics. Language exchange matching provides a complementary social feature where students can find conversation partners.

### How does this domain fit into the larger system?
`broadcast` posts appear in the Chat branch alongside direct messages. Posts can be targeted by program, level, or cohort (`target_type`/`target_id`). `broadcast_reaction` and `broadcast_comment` provide social engagement. `broadcast_reads` tracks who has seen each post. `language_exchange_match` is a separate feature (matching students for conversation practice) that was previously stored in the legacy `app.langexchange` table — all new code must use `app.language_exchange_match`.

---

## 2. Design Decisions

### Key invariants
- Only tutors or admins may INSERT broadcasts (`author_id = auth.uid() AND (is_tutor() OR is_admin())`).
- Scheduled broadcasts (`scheduled_at IS NOT NULL`) are not visible until `scheduled_at <= now()` — enforced by the SELECT policy.
- `broadcast_reaction` has UNIQUE(broadcast_id, user_id, emoji) — one reaction per emoji per user per post.
- `broadcast_reads` has composite PK (user_id, broadcast_id) — one read record per user per post.
- `language_exchange_match` has UNIQUE(user_a_id, user_b_id) — no duplicate pairs.
- `language_exchange_match.rematch_available_at` is auto-set by trigger to `matched_at + 14 days` — do NOT set it manually.
- `app.langexchange` (legacy) uses TEXT columns for UUIDs — do NOT use it for new code. Use `app.language_exchange_match`.

---

## 3. Tables

### Table: broadcast

A post published by a tutor or admin.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| author_id | uuid | FK → user_profiles(id) ON DELETE SET NULL |
| content | text | Post text content |
| attach_url | text | Optional attachment URL |
| attach_type | text | 'image' / 'audio' / 'explore' (CHECK constraint) |
| target_type | text | 'program' / 'level' / 'cohort' (CHECK constraint) |
| target_id | uuid | ID of the program, level entity, or cohort being targeted |
| target_level | text | Level string if target_type = 'level' |
| is_pinned | boolean | Whether pinned to the top of the feed |
| scheduled_at | timestamptz | If set, post is hidden until this time |
| published_at | timestamptz | When actually published |
| created_at | timestamptz | — |
| deleted_at | timestamptz | Soft delete |

#### RLS
- Authenticated: SELECT non-deleted posts where `scheduled_at IS NULL OR scheduled_at <= now()`.
- Tutor or admin: INSERT (must set `author_id = auth.uid()`).
- Admin: UPDATE, DELETE.

---

### Table: broadcast_reaction

Per-user emoji reactions to a broadcast post.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| broadcast_id | uuid | FK → broadcast(id) ON DELETE CASCADE |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| emoji | text | Emoji character or shortcode |
| created_at | timestamptz | — |

UNIQUE(broadcast_id, user_id, emoji) — one reaction per emoji type per user.

#### RLS
- Authenticated: SELECT all reactions.
- User: INSERT own reactions (`user_id = auth.uid()`).
- User or admin: DELETE own reactions.

---

### Table: broadcast_comment

Comments on a broadcast post.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| broadcast_id | uuid | FK → broadcast(id) ON DELETE CASCADE |
| author_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| content | text | Comment text |
| created_at | timestamptz | — |
| deleted_at | timestamptz | Soft delete |

#### RLS
- Authenticated: SELECT non-deleted comments.
- User: INSERT own comments (`author_id = auth.uid()`).
- User or admin: UPDATE own comments.

---

### Table: broadcast_reads

Read receipt for broadcast posts.

| Column | Type | Purpose |
|---|---|---|
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE — composite PK |
| broadcast_id | uuid | FK → broadcast(id) ON DELETE CASCADE — composite PK |
| read_at | timestamptz | — |

PRIMARY KEY(user_id, broadcast_id).

#### RLS
- User: ALL on own read records only (`user_id = auth.uid()`).

---

### Table: language_exchange_match

Active language exchange pairings between students.

**NOTE:** The legacy `app.langexchange` table (TEXT UUID columns, in social_media domain) is NOT used for new code. All new language exchange features use this table.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_a_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| user_b_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| conversation_id | uuid | FK → conversations(id) ON DELETE SET NULL — linked DM conversation |
| matched_at | timestamptz | When the match was created |
| rematch_available_at | timestamptz | Auto-set by trigger to matched_at + 14 days — do NOT set manually |
| status | text | 'active' / 'ended' / 'rematch_requested' |
| ended_at | timestamptz | When match ended |
| ended_by | uuid | FK → user_profiles(id) — who ended the match |

UNIQUE(user_a_id, user_b_id) — no duplicate pairs.

#### RLS
Not explicitly defined in the applied SQL (the CREATE TABLE was commented out in §27). Apply RLS before activating the language exchange feature flag.

---

## 4. Relationships Between Tables in This Domain

`broadcast` → `broadcast_reaction` (users react). `broadcast` → `broadcast_comment` (users comment). `broadcast` → `broadcast_reads` (users mark as read). `language_exchange_match` is independent — it links two users and optionally creates a `conversations` row (messaging domain).

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.user_profiles | identity_access | All broadcast tables reference user_profiles.id | user_profiles.id |
| app.conversations | messaging | language_exchange_match.conversation_id FK | conversations.id |

### Tables in THIS domain that other projects use
None — this domain is a consumer of user_profiles and conversations, not a provider.

---

## 6. Extension Rules

#### If you need a new broadcast target type
Add the value to the `broadcast.target_type` CHECK constraint.

#### If you need a new attachment type
Add the value to the `broadcast.attach_type` CHECK constraint.

#### Specifically do NOT
- Do NOT use `app.langexchange` for new language exchange features — use `app.language_exchange_match`.
- Do NOT set `language_exchange_match.rematch_available_at` manually — the trigger sets it to `matched_at + 14 days`.
- Do NOT create per-broadcast-type tables — use `target_type` + `target_id` columns.
