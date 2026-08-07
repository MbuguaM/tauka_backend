# Shared Database Schema Generator — Project Prompt

> **How to use this prompt:**
> 1. Paste this entire prompt into each project's AI session (Claude Code, Claude chat, etc.)
> 2. Start with the project that owns the MOST tables — it generates the initial shared-db folder
> 3. Copy the generated `shared-db/` folder into the next project
> 4. Paste this prompt again in that project's session — the AI will merge, extend, and flag conflicts
> 5. Repeat for every project, carrying the `shared-db/` folder forward each time
> 6. After all projects have contributed, copy the final `shared-db/` folder back into every project

---

## BEGIN PROMPT

You are a senior database architect. Your job is to analyze THIS project's codebase and generate (or update) a `shared-db/` folder that documents every database table, its ownership, its design rationale, and its relationships — in extreme detail.

### Context

This project is ONE of several that share a single database. Each project owns certain tables and reads from others it does not own. We are building a `shared-db/` folder that travels between projects. Each project's AI fills in the parts it owns, preserves what other projects wrote, and flags any conflicts.

### Step 1 — Analyze This Project

Before generating anything, thoroughly read the following (adjust paths to match this project's structure):

- All migration files, SQL files, schema files
- The spec/requirements documents
- All model/entity definitions (ORMs, type definitions, etc.)
- All API route handlers (to understand which tables are read vs written)
- Any existing `CLAUDE.md`, `README.md`, or architecture docs
- Any `.env` or config files that reference database table names

From this analysis, build a mental model of:

- Every table this project CREATES (owns)
- Every table this project READS from but did not create (depends on)
- Every table this project WRITES to but did not create (shared write)
- The business domain each table belongs to
- WHY each table exists — what problem it solves, what spec requirement drove it
- HOW tables relate to each other — foreign keys, implicit relationships, data flow
- What DESIGN DECISIONS were made — and what alternatives were rejected

### Step 2 — Check for Existing shared-db/ Folder

Look for a `shared-db/` folder in the project root.

**If it does NOT exist:** You are the first project. Generate everything from scratch per the structure below.

**If it DOES exist:** Another project has already contributed. You MUST:

1. Read every file in `shared-db/` completely
2. PRESERVE everything written by other projects — do not edit, reword, or delete their sections
3. ADD this project's tables, domains, and ownership entries
4. If this project references a table already documented by another project, add a `## Usage by [this-project-name]` subsection to that table's section in the domain file — do NOT rewrite the original documentation
5. DETECT AND FLAG CONFLICTS (see conflict rules below)

### Step 3 — Generate the shared-db/ Folder

Create/update this exact structure:

```
shared-db/
  ARCHITECTURE_INDEX.md
  OWNERS.md
  CONFLICT_LOG.md
  schema/
    db.sql
  domains/
    <domain-name>.md     (one per logical domain)
```

---

## FILE SPECIFICATIONS

### ARCHITECTURE_INDEX.md

```markdown
# Architecture Index
> Auto-generated. Each project adds its own domains and tables.
> Last updated by: [PROJECT_NAME] on [DATE]

## Projects Contributing to This Database
| Project | Description | Domains Owned |
|---|---|---|
| (fill in) | (one-line description) | (list of domain names) |

## Domain Map
| Domain | File | Owner Project | Tables | One-Line Purpose |
|---|---|---|---|---|
| (fill in per domain) |||||

## Global Anti-Patterns
(Add rules that apply across ALL projects. Each project may append to this list but never remove entries.)

- (e.g., Never store money as float — use integer cents)
- (e.g., Never create a generic key-value metadata table)
- (e.g., All timestamps are UTC, stored as TIMESTAMPTZ)
- (e.g., Soft deletes use a `deleted_at` column, never a boolean)

## Quick Lookup — All Tables Alphabetical
| Table | Domain | Owner | Read By | Write By |
|---|---|---|---|---|
| (fill in) |||||
```

---

### OWNERS.md

```markdown
# Ownership Map
> Each project fills in its own rows. Never modify another project's rows.
> Last updated by: [PROJECT_NAME] on [DATE]

## Ownership Rules
1. Only the owning project may CREATE TABLE or ALTER TABLE in its domain
2. Other projects get READ access — query only, never DDL
3. Shared-write tables are explicitly marked — all others are write-owner-only
4. If you need a column on a table you don't own, add to CONFLICT_LOG.md as a REQUEST
5. If two projects both claim ownership of the same table, that is a CONFLICT — log it

## Table Ownership

| Table | Owner Project | Domain | Access Granted To |
|---|---|---|---|
| | | | (list which other projects may read/write, and how) |

## Shared-Write Tables
(Tables where multiple projects may INSERT rows but no single project owns the schema)

| Table | Schema Owner | Projects That Write | Rules |
|---|---|---|---|
| | | | (e.g., "append only, never update other project's rows") |

## Cross-Project Dependencies
(What breaks if a table changes)

| If This Table Changes... | These Projects Are Affected | Specifically... |
|---|---|---|
| | | (e.g., "project-b joins on users.id in 14 queries") |
```

---

### CONFLICT_LOG.md

```markdown
# Conflict Log
> When a project detects a conflict, mismatch, or cross-project need, log it here.
> Conflicts must be resolved by humans before merging back.

## Active Conflicts

### CONFLICT-001: [Short title]
- **Detected by:** [project-name]
- **Date:** [date]
- **Type:** (one of: DUPLICATE_TABLE | COLUMN_MISMATCH | OWNERSHIP_DISPUTE | SCHEMA_REQUEST | NAMING_COLLISION | FK_MISMATCH)
- **Details:**
  (Describe exactly what conflicts. Include both sides. Quote the specific
  SQL or domain file sections that contradict each other.)
- **This project assumes:**
  (What this project is doing for now as a workaround)
- **Resolution needed:**
  (What needs to happen — who needs to decide what)

---
(repeat for each conflict)

## Resolved Conflicts
(Move conflicts here after resolution. Keep for historical reference.)
```

---

### schema/db.sql

This is the MERGED schema file. Rules for writing/updating it:

```sql
-- ============================================================
-- SHARED DATABASE SCHEMA
-- Last updated by: [PROJECT_NAME] on [DATE]
-- ============================================================
-- RULES:
-- 1. Each table is tagged with its owner project and domain
-- 2. Do NOT modify tables owned by other projects
-- 3. If you find a conflict, do NOT fix it here — log it in CONFLICT_LOG.md
-- 4. Tables are grouped by domain, alphabetical within domain
-- ============================================================

-- ============================================================
-- DOMAIN: Identity & Access (Owner: project-a)
-- ============================================================

-- [DOMAIN: Identity & Access] [OWNER: project-a] [SPEC: 2.1 User Management]
-- PURPOSE: One row per human user. Email is the unique identity.
--          Roles are enum, not separate tables, because a user has exactly one role.
-- USED BY: project-b (reads for order ownership), project-c (reads for analytics)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- (continue for all tables...)
```

**When merging another project's db.sql into this one:**

1. If a table exists in the incoming file and NOT here → ADD it with full annotations
2. If a table exists in BOTH → COMPARE column by column:
   - Columns match exactly → keep as-is
   - Incoming has columns this doesn't → FLAG in CONFLICT_LOG.md (do NOT silently add them)
   - Column types differ → FLAG in CONFLICT_LOG.md
   - Column exists here but not in incoming → keep it, it belongs to the owning project
3. If the same table name appears with DIFFERENT owners → CONFLICT, log it, keep both versions commented

---

### domains/<domain-name>.md

**THIS IS THE MOST IMPORTANT FILE. Write it in extreme detail.**

Each domain file must follow this exact structure. Do not abbreviate. Do not use shorthand. Write as if the reader has never seen the codebase and must understand every decision from this file alone.

```markdown
# Domain: [Domain Name]

> **Owner project:** [project-name]
> **Last updated by:** [project-name] on [date]
> **Spec sections:** [list every spec section/requirement that drives this domain]

## 1. Business Context

### What real-world problem does this domain solve?
(Write 3-5 sentences. Not technical. Describe the business/user need
as if explaining to a product manager.)

### How does this domain fit into the larger system?
(What happens upstream that feeds into this domain? What happens
downstream that depends on it? Draw the causal chain.)

### User stories this domain serves
- As a [role], I can [action] so that [outcome]
- (list ALL user stories, not just the primary one)

## 2. Design Decisions

### Architecture chosen
(Describe the high-level approach. E.g., "Event-sourced order
lifecycle with separate payment tracking" or "Simple CRUD with
soft deletes")

### Why this architecture and not alternatives
| Approach | Why We Rejected It |
|---|---|
| (alternative 1) | (specific reason — not just "too complex", explain WHY it would fail for our case) |
| (alternative 2) | (specific reason) |

### Key invariants (things that must ALWAYS be true)
- (e.g., "Every order_item must belong to exactly one order")
- (e.g., "A payment amount_cents can never be negative — refunds are separate rows")
- (e.g., "A user can belong to many orgs but has exactly one role per org")
- (list ALL invariants — these are the rules the AI must never violate)

### Data flow
(Describe step by step how data moves through the tables in this
domain during a typical operation. E.g., "1. User submits checkout
→ 2. Order row created with status=draft → 3. Order items created
from cart → 4. Payment initiated → 5. On payment success, order
status → confirmed")

## 3. Tables — Detailed Specification

### Table: [table_name]

#### Purpose
(2-3 sentences: what this table represents, why it exists as its own
table rather than being a column on another table or a JSONB field)

#### Spec origin
(Which specific spec requirement/section/user story created this table)

#### Row lifecycle
(When is a row created? Updated? Deleted/soft-deleted? What triggers
each state change? E.g., "Created when user signs up. Updated on
profile changes. Soft-deleted when account is deactivated. Hard-deleted
after 90-day retention period.")

#### Columns

| Column | Type | Nullable | Default | Purpose | Constraint/Validation |
|---|---|---|---|---|---|
| id | UUID | NO | gen_random_uuid() | Primary key | PK |
| (every column) ||||||

#### Indexes
| Index Name | Columns | Type | Why |
|---|---|---|---|
| (every index) | | (btree/gin/unique/etc) | (what query pattern it serves) |

#### Foreign Keys
| Column | References | On Delete | On Update | Why This Relationship |
|---|---|---|---|---|
| (every FK) |||||

#### Access patterns
(How is this table queried in practice? List the actual query patterns.)
- **By owning project:** (e.g., "SELECT * WHERE user_id = ? AND status = 'active', ~500 QPS")
- **By other projects:** (e.g., "project-b joins on users.id to resolve order ownership, read-only")

#### What this table is NOT for
(Explicitly state misuses. E.g., "Do NOT store user preferences here —
those go in user_settings JSONB column. Do NOT use this as an activity
log — use the events table.")

---
(Repeat the above ### Table section for every table in this domain)

## 4. Relationships Between Tables in This Domain

(Describe the entity-relationship model in plain language. Not just
"FK from A to B" but the business meaning. E.g., "An order HAS MANY
order_items because a single checkout can contain multiple products.
An order_item CANNOT exist without an order — it is a dependent entity.")

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| | | | |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| | | | (e.g., "column user_id type and name — project-b has 14 JOINs on it") |

## 6. Extension Rules

### When adding a new feature to this domain, follow these rules:

#### If you need a new attribute on an existing entity:
(e.g., "Add a column to the existing table. Do NOT create a
satellite/profile table. We chose wide tables intentionally because...")

#### If you need a new entity related to this domain:
(e.g., "Create a new table in THIS domain file. FK back to the
core table. Follow the naming convention: singular noun, snake_case.
Add it to ARCHITECTURE_INDEX.md and OWNERS.md.")

#### If you need to track history/changes:
(e.g., "Use an _audit table pattern: same columns + changed_at +
changed_by. Do NOT use triggers — handle in application code because...")

#### If you need a new status/state:
(e.g., "Add to the CHECK constraint enum on the status column. Update
the state machine documentation in section 2. Do NOT create boolean
flag columns like is_active, is_deleted — we use status enum + deleted_at.")

#### Specifically do NOT:
- (list every anti-pattern specific to this domain)
- (e.g., "Do NOT create a separate orders_archive table — use the
  status=archived pattern")
- (e.g., "Do NOT add polymorphic foreign keys — if a new entity
  needs to link to orders, use a dedicated FK column")

## 7. Usage by Other Projects

### Usage by [other-project-name]
> Added by: [other-project-name] on [date]

(When another project processes this file and finds it already exists,
they add their usage section here instead of rewriting the domain.
Describe: which tables they read, how they query them, what they
depend on, what would break them.)

---
```

---

## CONFLICT DETECTION RULES

When you encounter the incoming shared-db/ folder from another project, check for ALL of the following:

1. **DUPLICATE_TABLE:** Same table name, different DDL → log both versions
2. **OWNERSHIP_DISPUTE:** Two projects both claim to own the same table → log it
3. **COLUMN_MISMATCH:** Same table, same column name, different type/constraints → log it
4. **NAMING_COLLISION:** Different tables that appear to model the same concept (e.g., `user_notifications` vs `notification_log`) → log it and ask if they should be merged
5. **FK_MISMATCH:** A foreign key references a column that doesn't exist or has a different type in the target table → log it
6. **MISSING_DEPENDENCY:** This project reads from a table that no project has documented yet → log it
7. **IMPLICIT_CONTRACT:** This project depends on specific column names/types in another project's table but that contract isn't documented → add it to cross-domain dependencies

For each conflict, ask yourself: "If both projects deployed to production right now, would the database be consistent?" If the answer is no, it's a conflict.

---

## TONE AND DETAIL LEVEL

- Write domain files as if onboarding a new engineer who will work alone for 3 months
- Every decision needs a "because" — never just state what, always state why
- If a design choice was arbitrary (e.g., "we used UUID instead of SERIAL — no strong reason"), say so explicitly — this prevents future engineers from inventing a justification and building on it
- Assume the reader cannot see the codebase — everything must be self-contained
- Do not use shorthand, abbreviations, or "see above" — each section must be independently readable
- When in doubt, over-document rather than under-document

## END PROMPT