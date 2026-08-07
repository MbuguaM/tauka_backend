# Domain: Cohort

> **Owner project:** tauka-python (creates/manages cohorts); tauka-flutter (reads)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §7 Admin Dashboard — Cohort Management

---

## 1. Business Context

### What real-world problem does this domain solve?
Tutors teach groups of students who may span multiple class sections or be organized by language level. Cohorts are admin-managed groupings that allow richer scheduling, bulk assignments, and cohort-level payment tracking — separate from the messaging-focused `app.classes` system. This domain stores the group structure, membership, payments, and content assignments for cohorts.

### How does this domain fit into the larger system?
`app.cohorts` is intentionally separate from `app.classes`. Classes are the messaging/notification organizational unit (see [[messaging]]); cohorts are the billing/scheduling unit. A cohort may correspond to one or more classes, but the mapping is informal and not enforced by FK. `cohort_payments` tracks per-student cohort fees via Stripe. `cohort_assignments` lets tutors/admins assign Anki decks or content to a cohort.

---

## 2. Design Decisions

### Key invariants
- `cohorts` is admin-writable only; all authenticated users can read active cohorts.
- `cohort_memberships.status` follows a lifecycle: 'active' → 'paused' / 'graduated' / 'dropped'.
- `cohort_payments.amount` uses `numeric(10,2)` — never float for money values.
- `cohort_assignments.deck_id` has no FK constraint — it references `anki_deck.id` but the FK was not created to avoid circular dependency. Application code must validate.
- Cohorts are separate from `app.classes` by design — do not add a `class_id` FK to cohorts.

---

## 3. Tables

### Table: cohorts

Top-level cohort group. Admin-managed; readable by all authenticated users.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| name | text | Cohort display name |
| description | text | Optional description |
| tutor_id | uuid | FK → user_profiles(id) ON DELETE SET NULL — assigned tutor |
| max_students | integer | Capacity cap |
| start_date | date | Cohort start |
| end_date | date | Cohort end |
| is_active | boolean | Whether currently active |
| created_at | timestamptz | — |

#### RLS
- Authenticated: SELECT all cohorts.
- Admin: ALL (INSERT, UPDATE, DELETE).

---

### Table: cohort_memberships

Per-student membership in a cohort.

| Column | Type | Purpose |
|---|---|---|
| cohort_id | uuid | FK → cohorts(id) ON DELETE CASCADE — composite PK |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE — composite PK |
| joined_at | timestamptz | — |
| status | text | 'active' / 'paused' / 'graduated' / 'dropped' |

PRIMARY KEY(cohort_id, user_id).

#### RLS
- Student: SELECT own memberships.
- Tutor or admin: SELECT all.
- Admin: INSERT, UPDATE (to change status).

---

### Table: cohort_payments

Per-student cohort fee payment record.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| cohort_id | uuid | FK → cohorts(id) ON DELETE SET NULL |
| student_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| tutor_id | uuid | FK → user_profiles(id) ON DELETE SET NULL — which tutor receives payment |
| amount | numeric(10,2) | Amount charged |
| currency | text | Currency code (default 'USD') |
| status | text | 'pending' / 'paid' / 'failed' / 'refunded' |
| stripe_payment_intent_id | text | Stripe payment reference |
| paid_at | timestamptz | — |
| created_at | timestamptz | — |

#### RLS
- Student: SELECT own payment rows.
- Tutor: SELECT own payment rows (where tutor_id = auth.uid()).
- Admin: ALL.

---

### Table: cohort_assignments

Content or Anki deck assignments to a cohort.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| cohort_id | uuid | FK → cohorts(id) ON DELETE CASCADE |
| assigned_by | uuid | FK → user_profiles(id) ON DELETE SET NULL |
| deck_id | uuid | Anki deck ID (no FK — references anki_deck.id logically) |
| content_ref | text | Freetext content reference for non-deck assignments |
| due_date | date | When assigned content is due |
| created_at | timestamptz | — |

#### RLS
- Cohort member, tutor, or admin: SELECT.
- Tutor or admin: ALL (INSERT, UPDATE, DELETE).

---

## 4. Relationships Between Tables in This Domain

`cohorts` → (members) `cohort_memberships` (students). `cohorts` → (payments) `cohort_payments` (per-student fees). `cohorts` → (assignments) `cohort_assignments` (assigned content/decks).

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.user_profiles | identity_access | cohorts.tutor_id, cohort_memberships.user_id, cohort_payments.student_id | user_profiles.id |
| app.anki_deck | course_content | cohort_assignments.deck_id (logical ref, no FK) | If anki_deck.id type changes |

### Tables in THIS domain that other projects use
None — cohort data is read by Flutter and Python dashboards but not depended on by FK from other domains.

---

## 6. Extension Rules

#### If you need a new membership status
Add the value to the `cohort_memberships.status` CHECK constraint.

#### If you need to track cohort content history
Add a `cohort_assignment_history` table with FK to `cohort_assignments`. Do NOT use `cohort_assignments` as a log table.

#### Specifically do NOT
- Do NOT add `class_id` FK to `cohorts` — cohorts are intentionally separate from `app.classes`.
- Do NOT use float types for `cohort_payments.amount` — always use `numeric(10,2)`.
- Do NOT hard-delete `cohort_payments` rows — payment history must survive; use `status = 'refunded'`.
