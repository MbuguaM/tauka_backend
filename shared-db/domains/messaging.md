# Domain: Messaging & Classes

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5.4 Chat Branch, §2.5 Branch Contents, §15 Notification System

---

## 1. Business Context

### What real-world problem does this domain solve?
Language learning is social. Students need to communicate with their classmates and tutor; tutors need to send announcements to their classes. This domain provides the infrastructure for real-time and asynchronous text communication, class grouping, and in-app notifications. Without it, learning is isolated and the community aspect of Tauka (which is a key retention driver) would not exist.

### How does this domain fit into the larger system?
`app.classes` is the organisational unit that groups students under a tutor. `class_members` determines who is in which class, which in turn controls who can see whose messages and which notifications are relevant. `conversations` and `messages` power the Chat branch. The messaging layer is also used by the AI domain: `ai_conversation` has a `classic_conversation_id` FK back to `conversations`. The `rate_limits` table protects the messaging endpoints from spam.

### User stories this domain serves
- As a student, I can send messages to my classmates and receive replies in real time.
- As a student, I can start a direct message thread with another student in my class.
- As a tutor, I can send announcements to my subclass that all students receive as notifications.
- As a tutor, I can create a class and add students to it.
- As an admin, I can broadcast a message to all students via an `admin_broadcast` conversation.
- As a student, I see unread message counts and notification badges.

---

## 2. Design Decisions

### Architecture chosen
Classes → Subclasses → ClassMembers form the group hierarchy. Conversations are separate entities linked to classes (for group chats) or not (for direct messages between students). The recursive RLS problem — where a policy on `class_members` would query `class_members` to check membership — is solved by three SECURITY DEFINER helper functions: `my_class_ids()`, `my_teacher_class_ids()`, and `is_conversation_participant()`.

### Key invariants
- A user can only see messages in conversations they participate in (enforced by `is_conversation_participant()` in RLS).
- A user can only send messages if `sender_id = auth.uid()` AND they are a conversation participant.
- A teacher can only create notifications in subclasses where they hold the 'teacher' role AND `sender_id = auth.uid()` (prevents sender impersonation).
- `app.rate_limits` is NEVER queried directly by clients — only via the `check_rate_limit()` SECURITY DEFINER function.
- `conversation_participants` rows are created via SECURITY DEFINER functions only (`get_or_create_student_conversation`, `upsert_conversation_participant`, `add_all_students_to_conversation`) — direct INSERT is admin-only.
- `message_read_receipts` and `notification_receipts` can only be created for messages/notifications the user is actually a participant of (cross-table check in WITH CHECK).

### Data flow
**Student DM flow:**
1. Student A taps Student B's profile → Flutter calls `get_or_create_student_conversation(a, b)` SECURITY DEFINER function.
2. Function checks `students_share_class(a, b)` — raises exception if they don't share a class.
3. Function creates a `conversations` row (type='student_to_student') and two `conversation_participants` rows.
4. Student A sends message → INSERT into `messages` with `conversation_id` and `sender_id = auth.uid()`.
5. Student B's Flutter client receives Realtime event → marks as read → INSERT into `message_read_receipts`.

**Notification flow:**
1. Teacher calls `create_subclass_notification(subclass_id, sender_id, title, content, is_urgent)`.
2. SECURITY DEFINER function inserts notification + fans out `notification_receipts` to all students in the subclass.
3. Flutter clients receive Realtime event on `notification_receipts` for their own user_id.

---

## 3. Tables — Detailed Specification

### Table: classes

#### Purpose
The top-level grouping unit. A class contains students and teachers. Classes are created by active tutors or admins. The §07 additions (health_score, health_status, current_lesson, level) allow admin dashboard cohort health monitoring.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | uuid_generate_v4() | PK |
| name | text | NO | — | Class display name |
| description | text | YES | NULL | Optional description |
| created_at | timestamptz | YES | now() | — |
| created_by | uuid | YES | NULL | FK → auth.users(id) |
| deleted_at | timestamptz | YES | NULL | Soft delete |
| health_score | smallint | YES | NULL | 0–100 cohort health score (§07) |
| health_status | text | YES | NULL | healthy/attention/intervention (§07) |
| current_lesson | integer | YES | 1 | Current lesson number tracked for cohort (§07) |
| level | text | YES | NULL | Proficiency level of the class (§07) |

---

### Table: subclasses

#### Purpose
A time-bounded session within a class. Notifications are sent at the subclass level. Students belong to a class AND optionally to a subclass. The date range (start_date, end_date) allows past subclasses to remain in the database for history without polluting active queries.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | uuid_generate_v4() | PK |
| class_id | uuid | YES | NULL | Parent class | FK → classes(id) ON DELETE CASCADE |
| name | text | NO | — | Subclass name |
| description | text | YES | NULL | — |
| start_date | timestamptz | NO | — | Session start |
| end_date | timestamptz | NO | — | Session end; CHECK (end_date > start_date) |
| deleted_at | timestamptz | YES | NULL | Soft delete |

---

### Table: class_members

#### Purpose
Many-to-many junction between users and classes, with role (teacher/student). A user can be a teacher in one class and a student in another. The UNIQUE(class_id, user_id) constraint prevents duplicate membership. The `subclass_id` optionally places the member in a specific session.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | uuid_generate_v4() | PK |
| class_id | uuid | YES | NULL | FK → classes(id) ON DELETE CASCADE |
| subclass_id | uuid | YES | NULL | FK → subclasses(id) ON DELETE SET NULL |
| user_id | uuid | YES | NULL | FK → auth.users(id) ON DELETE CASCADE |
| role | text | NO | — | 'teacher' or 'student' |
| joined_at | timestamptz | YES | now() | — |
| deleted_at | timestamptz | YES | NULL | Soft delete |

---

### Table: conversations

#### Purpose
A logical thread of messages. Typed by context: student-to-student DM, teacher-to-student, class group chat, or admin broadcast. The `class_id` FK links group conversations to their class for context. `last_message_at` is updated on each new message for sort ordering.

#### Columns

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| id | uuid | NO | uuid_generate_v4() | PK |
| type | text | NO | — | student_to_student / teacher_to_student / class_group / admin_broadcast |
| class_id | uuid | YES | NULL | FK → classes(id) ON DELETE SET NULL |
| created_at | timestamptz | YES | now() | — |
| last_message_at | timestamptz | YES | NULL | Updated on each new message |
| deleted_at | timestamptz | YES | NULL | Soft delete |

---

### Table: conversation_participants

#### Purpose
Join table between users and conversations. RLS uses `is_conversation_participant()` SECURITY DEFINER function (rather than querying this table directly in policies) to avoid the 42P17 recursive policy evaluation problem. Direct INSERT is admin-only; all other creation goes through SECURITY DEFINER functions.

---

### Table: messages

#### Purpose
Individual text messages within a conversation. Soft-deleted (sender or admin can set deleted_at). Read receipts tracked separately in `message_read_receipts`.

---

### Table: message_read_receipts

#### Purpose
Records when a user read a specific message. The WITH CHECK policy ensures a user can only create a receipt for a message in a conversation they participate in — preventing receipt spoofing across conversations.

---

### Table: notifications

#### Purpose
Subclass-scoped announcements from teachers or admins. The §06 additions (notification_type, action_url, priority) extend the base notification for a 20-type notification registry. Created via the `create_subclass_notification()` SECURITY DEFINER function which also fans out receipts.

---

### Table: notification_receipts

#### Purpose
Per-user read status for notifications. Created by the `create_subclass_notification()` function; the WITH CHECK policy prevents a user from creating receipts for notifications outside their class membership.

---

### Table: rate_limits

#### Purpose
Sliding-window rate limiting for API actions (e.g., message sends, AI calls). Accessed exclusively via `check_rate_limit()` SECURITY DEFINER function — no direct SELECT/INSERT from the Flutter client. Has no RLS policies (function bypasses them). Only keep if Edge Functions will use it; not currently called from Dart.

---

## 4. Relationships Between Tables in This Domain

`classes` → `subclasses` (one class has many time-bounded sessions). `class_members` connects users to classes with a role. `conversations` may optionally belong to a class (group chats) or be independent (DMs). `conversation_participants` tracks who is in each conversation. `messages` belong to conversations. `notifications` belong to subclasses and fan out to `notification_receipts` per student member.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| auth.users | Supabase Auth | class_members.user_id, conversation_participants.user_id, messages.sender_id | FK integrity |
| app.tutor | student_tutor | classes INSERT policy checks tutor status | Renaming tutor.status or tutor.id breaks class creation policy |
| app.ai_conversation | ai | ai_conversation.classic_conversation_id FK references conversations.id | Removing conversations.id breaks the AI chat link |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| app.conversations | ai domain | ai_conversation.classic_conversation_id FK | conversations.id column name and type |
| app.classes | student_tutor domain | tutor_assignment.class_id FK (§B3) | classes.id |
| app.class_members | multiple RLS policies | my_class_ids(), my_teacher_class_ids() SECURITY DEFINER functions | class_id, user_id, role, deleted_at column names |

---

## 6. Extension Rules

#### If you need a new message type
Add to the `conversations.type` CHECK constraint. Do NOT create a separate `direct_messages` table.

#### If you need to track message editing history
Add a `message_edits` table with FK to messages. Do NOT use the messages table itself as a history log.

#### If you need a new notification type
Add to the `notification_type` column (text, no CHECK constraint — intentionally open). Document the new type in the §15 notification spec. Do NOT create a new table per notification type.

#### Specifically do NOT
- Do NOT query `class_members` directly in RLS policies — use `my_class_ids()` or `my_teacher_class_ids()`.
- Do NOT query `conversation_participants` directly in RLS — use `is_conversation_participant()`.
- Do NOT INSERT into `conversation_participants` directly from Flutter — use the SECURITY DEFINER functions.
- Do NOT access `rate_limits` directly — always use `check_rate_limit()`.
