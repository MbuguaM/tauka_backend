# Domain: Tutor Management

> **Owner project:** tauka-python (payroll, rates); tauka-flutter (availability, payout settings UI)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §6 Tutor Features, §12 Payroll System

---

## 1. Business Context

### What real-world problem does this domain solve?
Tutors are paid contractors. This domain manages the operational side of being a tutor: availability scheduling (when can students book you), payout settings (where to send earnings), session logging (what was taught), payroll calculation (how much is owed), line items (the audit trail), and rate history (per-session rates over time). Without this domain, the platform cannot pay tutors correctly or let students know when tutors are available.

### How does this domain fit into the larger system?
`tutor_availability` is read by the student booking UI. `tutor_payout_settings` is used by Python to route Stripe Connect transfers. `tutor_sessions` (also in [[video]] domain) is the session log read by `get_tutor_monthly_earnings()` RPC. `tutor_payroll` is created by Python cron; `payroll_line_item` gives the audit trail of what's in each payroll. `tutor_rates` stores historical rate records, separate from `app.tutor.per_session_rate_cents` (which is the current rate).

---

## 2. Design Decisions

### Key invariants
- `tutor_availability.tutor_id` and `tutor_payout_settings.tutor_id` FK to `app.tutor(id)` — NOT `auth.users(id)`. This was an explicit fix (CONFLICT-010 / [R-1]) to enforce that only active tutors can set availability and payout details.
- `tutor_payout_settings` has UNIQUE on `tutor_id` — one payout config per tutor.
- `tutor_payout_settings.encrypted_details` stores encrypted bank/wallet details; `details_last4` is the display-safe suffix.
- `tutor_payroll.total_amount` is a GENERATED ALWAYS AS column (`base_amount + COALESCE(performance_bonus, 0)`) — never write it directly.
- `tutor_payroll` has UNIQUE(tutor_id, period_start, period_end) — one payroll record per pay period per tutor.
- `tutor_rates` stores historical rates with date ranges; `app.tutor.per_session_rate_cents` is the **current** rate used in `get_tutor_monthly_earnings()` RPC.

---

## 3. Tables

### Table: tutor_availability

Weekly recurring availability slots for a tutor.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → app.tutor(id) ON DELETE CASCADE |
| day_of_week | integer | 0=Sunday … 6=Saturday |
| start_time | time | Slot start (local time of timezone) |
| end_time | time | Slot end |
| timezone | text | IANA timezone string (default 'UTC') |
| active | boolean | Whether this slot is currently offered |
| created_at / updated_at | timestamptz | — |

UNIQUE(tutor_id, day_of_week, start_time) — no duplicate slots.

#### RLS
- Tutor: ALL on own slots.
- Students: SELECT active slots.

---

### Table: tutor_payout_settings

Stores encrypted payment details for a tutor. One row per tutor.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → app.tutor(id) ON DELETE CASCADE; UNIQUE |
| method | text | 'stripe_connect' / 'mpesa' / 'bank_transfer' |
| encrypted_details | text | Encrypted bank/wallet details (never log raw) |
| details_last4 | text | Display-safe last 4 digits |
| verified | boolean | Whether payout details have been verified by admin |
| created_at / updated_at | timestamptz | — |

#### RLS
- Tutor: SELECT own row only (no UPDATE from Flutter — write via Edge Function with re-encryption).

---

### Table: tutor_sessions

Completed teaching session log. See also [[video]] domain for `video_session` (scheduled sessions).

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → auth.users(id) — intentionally not app.tutor to survive soft-delete |
| student_ids | uuid[] | Participating student UUIDs |
| student_count | integer | Denormalized count |
| session_type | text | 'cohort' / 'solo' / etc. |
| duration_minutes | integer | Actual session length |
| started_at | timestamptz | Session start |
| ended_at | timestamptz | Session end |
| topic | text | Subject covered |
| notes | text | Tutor notes |
| created_at | timestamptz | — |

#### RLS
- Tutor: SELECT own rows.
- Python backend: reads via service role for `get_tutor_monthly_earnings()` RPC.

---

### Table: tutor_payroll

One payroll record per pay period per tutor. `total_amount` is auto-computed.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → user_profiles(id) ON DELETE RESTRICT (preserve history) |
| period_start | date | Pay period start |
| period_end | date | Pay period end |
| group_sessions | integer | Count of group sessions |
| solo_sessions | integer | Count of solo sessions |
| explore_contributions | integer | Count of Explore content contributions |
| base_amount | numeric(10,2) | Calculated base pay |
| performance_bonus | numeric(10,2) | Optional bonus |
| total_amount | numeric(10,2) | **GENERATED** = base_amount + COALESCE(performance_bonus, 0) — never write directly |
| status | text | 'pending' → 'processing' → 'completed' → 'confirmed' |
| stripe_transfer_id | text | Stripe Connect transfer ID |
| payment_method | text | 'stripe' / 'mpesa' / 'manual' |
| paid_at | timestamptz | When payment was initiated |
| approved_by | uuid | FK → user_profiles(id) — admin who approved |
| created_at | timestamptz | — |

UNIQUE(tutor_id, period_start, period_end).

#### RLS
- Tutor: SELECT own payroll.
- Admin: SELECT all; INSERT; UPDATE.

---

### Table: payroll_line_item

Audit trail for what's included in each payroll record.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| payroll_id | uuid | FK → tutor_payroll(id) ON DELETE CASCADE |
| item_type | text | 'group_session' / 'solo_session' / 'explore_contribution' / 'performance_bonus' |
| session_id | uuid | FK → video_session(id) ON DELETE SET NULL — if session-based |
| reference_id | uuid | Generic reference ID for non-session items |
| description | text | Human-readable description |
| amount | numeric(10,2) | Amount for this line |
| created_at | timestamptz | — |

#### RLS
- Tutor: SELECT own line items (via parent payroll_id JOIN).
- Admin: SELECT all; INSERT.

---

### Table: tutor_rates

Historical rate records for audit and rate-change tracking.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| tutor_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| session_type | text | 'group' / 'solo' / 'explore_contribution' |
| rate_amount | numeric(10,2) | Rate for this period |
| currency | text | Currency code |
| effective_from | date | Rate effective start |
| effective_to | date | Rate effective end (null = current) |
| created_at | timestamptz | — |

UNIQUE(tutor_id, session_type, effective_from) — no duplicate rates for the same start date.

#### RLS
- Tutor: SELECT own rates.
- Admin: ALL.

---

## 4. Relationships Between Tables in This Domain

`tutor_availability` and `tutor_payout_settings` are independent per-tutor config tables. `tutor_sessions` feeds into `tutor_payroll` (Python aggregates sessions to create payrolls). `tutor_payroll` → `payroll_line_item` (one payroll → many line items). `tutor_rates` is a historical log; the current rate is `app.tutor.per_session_rate_cents`.

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.tutor | student_tutor | tutor_availability and tutor_payout_settings FK to app.tutor(id) | tutor.id |
| app.video_session | video | payroll_line_item.session_id FK | video_session.id |
| app.user_profiles | identity_access | tutor_payroll.tutor_id, tutor_rates.tutor_id | user_profiles.id |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| app.tutor_sessions | tauka-python | `get_tutor_monthly_earnings()` aggregates this | tutor_id, started_at |
| app.tutor_payroll | tauka-python | Python creates payroll records via service role | tutor_id, period_start, period_end, status |

---

## 6. Extension Rules

#### If you need a new payment method
Add the value to `tutor_payroll.payment_method` CHECK constraint AND update `tutor_payout_settings.method` enum if needed.

#### If you need to track rate changes
INSERT a new `tutor_rates` row with `effective_from = today` and set `effective_to` on the previous row. Do NOT update `tutor.per_session_rate_cents` history — that column tracks only the current rate.

#### Specifically do NOT
- Do NOT write `tutor_payroll.total_amount` directly — it is a GENERATED column.
- Do NOT store raw bank details in `tutor_payout_settings.encrypted_details` unencrypted — this column must always be encrypted before INSERT.
- Do NOT delete `tutor_payroll` rows — use `status = 'confirmed'` as the terminal state; billing history must survive.

---

## 7. Usage by tauka-react-web

> Added by: tauka-react-web on 2026-05-26

### `app.tutor_availability` — read + write via supabase-js on /tutor/schedule

The `/tutor/schedule` page reads and upserts availability slots directly via supabase-js. This is the one table in the tutor management domain that the web client writes directly (the `tutors_manage_own_availability` RLS policy permits ALL operations where `tutor_id = auth.uid()`).

- **Read:** On page load, the page fetches all rows for `tutor_id = auth.uid()`. Renders a weekly grid with current active slots.
- **Write:** When a tutor taps/drags to change availability, the page upserts rows: `INSERT ... ON CONFLICT (tutor_id, day_of_week, start_time) DO UPDATE SET end_time, active, timezone`.
- **Delete:** When a tutor removes a slot, the page sets `active = false` (soft removal) rather than deleting the row, to preserve history.

The `UNIQUE(tutor_id, day_of_week, start_time)` constraint is load-bearing for the upsert pattern — do not remove it.

### Tables read via FastAPI (JWT passed as Bearer token)

**Tutor earnings** — `/tutor/earnings` calls `GET /tutor/earnings` and `GET /tutor/earnings/history` (FastAPI). These internally call the `get_tutor_monthly_earnings()` RPC against `tutor_sessions`. The web client does not read `tutor_sessions` or `tutor_payroll` directly.

**Payout settings** — `/tutor/earnings` calls `GET /tutor/payout-settings` (FastAPI), which returns only `{ method, details_last4, verified }` — never the encrypted details. `POST /tutor/payout-settings` writes new payout details via FastAPI (encrypts before writing to `tutor_payout_settings.encrypted_details`). The web client never reads or writes `encrypted_details` directly.

### What the web client does NOT do
- Does NOT read `tutor_payroll` or `payroll_line_item` directly — served via FastAPI with aggregation.
- Does NOT read `tutor_payout_settings.encrypted_details` — the RLS policy exposes only display columns.
- Does NOT read `tutor_rates` — rates are embedded in the FastAPI earnings response.
