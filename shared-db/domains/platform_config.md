# Domain: Platform Config

> **Owner project:** tauka-flutter (reads); tauka-python (writes stripe_events); admin UI (writes config/flags)
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §13 Feature Flags, §9 Tier Gating, §16 Platform Configuration

---

## 1. Business Context

### What real-world problem does this domain solve?
A live app needs runtime configurability: which features are ready vs. coming soon, which tiers unlock which features, app-wide config values (minimum version, store URLs), Stripe event audit logging, push notification tokens, and gamification (achievements, XP). This domain provides all these knobs without requiring an app release to change behaviour.

### How does this domain fit into the larger system?
`feature_flag` is read by the Flutter app at startup to show/hide Coming Soon labels. `tier_gating_rules` is read by the Flutter app to decide which UI flows are locked behind a paywall. `config` provides key-value config (min_version, maintenance_mode). `stripe_events` is an audit log written by the Python Stripe webhook handler. `push_tokens` stores FCM tokens for push notification delivery. `achievements` + `user_achievements` implement the gamification layer. All tables in this domain are readable by all authenticated users; writes are admin or service-role only (except push_tokens which users manage themselves).

---

## 2. Design Decisions

### Key invariants
- `feature_flag.key` is UNIQUE — no duplicate flag keys.
- `tier_gating_rules.feature` is the PRIMARY KEY — one rule per feature.
- `tier_gating_rules.min_tier` values are `'free' | 'learner' | 'tutor' | 'intensive'` — the canonical tier enum. Never `'tutor_tier'` (CONFLICT-003).
- `achievements.key` is UNIQUE — no duplicate achievement keys.
- `user_achievements` has composite PK (user_id, achievement_id) — one award per achievement per user.
- `stripe_events.stripe_event_id` is UNIQUE — idempotent webhook processing.
- `push_tokens` has UNIQUE(user_id, token) — no duplicate device tokens.
- `config` is a key-value table with `key` as PRIMARY KEY — admin-writable, authenticated-readable.

---

## 3. Tables

### Table: feature_flag

Runtime feature toggle registry.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| key | text | UNIQUE flag name (e.g., 'ai_practice', 'broadcasts') |
| status | text | 'ready' or 'coming' (CHECK constraint) |
| description | text | Human-readable description for admin UI |
| notify_me_count | integer | How many users tapped "Notify Me" for this feature |
| updated_at / created_at | timestamptz | — |

#### Seeded flags (status at launch)
`ai_practice`, `ai_pronunciation`, `broadcasts`, `language_exchange`, `cohort_management`, `payroll`, `handwriting_desktop`, `handwriting_amharic`, `tiktok_oauth`, `explore_annotations`, `explore_share_cards`, `device_pairing`, `push_notifications`, `offline_sync_conflict`, `srs_wired`, `exercise_results_sync` — all seeded as `'coming'`.

#### RLS
- Authenticated: SELECT all flags.
- Admin: INSERT, UPDATE.

---

### Table: tier_gating_rules

Defines the minimum tier required for each premium feature.

| Column | Type | Purpose |
|---|---|---|
| feature | text | PRIMARY KEY — feature name |
| min_tier | text | 'free' / 'learner' / 'tutor' / 'intensive' (CHECK constraint) |
| description | text | — |
| updated_at | timestamptz | — |

#### Seeded rules
| Feature | Min Tier |
|---|---|
| ai_practice | learner |
| ai_unlimited | learner |
| ai_pronunciation | intensive |
| video_session | learner |
| anki_sync | learner |
| explore_download | learner |
| language_exchange | free |
| broadcasts | free |
| cohort_management | tutor |
| content_creation | tutor |
| admin_dashboard | tutor |

#### RLS
- Authenticated: SELECT all rules.
- Admin: ALL (INSERT, UPDATE, DELETE).

---

### Table: config

Key-value platform configuration store.

| Column | Type | Purpose |
|---|---|---|
| key | text | PRIMARY KEY |
| value | text | Config value |
| updated_at | timestamptz | — |

#### Seeded keys
`min_version`, `android_store_url`, `ios_store_url`, `stripe_checkout_url`, `maintenance_mode`, `maintenance_message`.

#### RLS
- Authenticated: SELECT all config.
- Admin: ALL.

---

### Table: stripe_events

Audit log of processed Stripe webhook events. Written by Python Stripe handler via service role.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| stripe_event_id | text | UNIQUE — Stripe's event ID (idempotency key) |
| type | text | Stripe event type (e.g., 'customer.subscription.updated') |
| payload | jsonb | Full webhook payload |
| processed_at | timestamptz | — |

#### RLS
- No client policies — service role only. Flutter clients cannot read or write this table.

---

### Table: push_tokens

FCM device tokens for push notification delivery.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE |
| token | text | FCM/APNS device token |
| platform | text | 'android' / 'ios' / 'web' (CHECK constraint) |
| created_at | timestamptz | — |

UNIQUE(user_id, token) — no duplicate tokens.

#### RLS
- User: ALL on own tokens only.
- Python backend: reads tokens via service role to send push notifications.

---

### Table: achievements

Gamification achievement definitions. Seeded at deploy time.

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| key | text | UNIQUE achievement key (e.g., 'streak_7', 'first_lesson') |
| name | text | Display name |
| description | text | What the student did to earn it |
| icon_name | text | Material icon name for display |
| xp_reward | integer | XP awarded when earned |
| created_at | timestamptz | — |

#### Seeded achievements
`first_lesson`, `streak_7`, `streak_30`, `drills_50`, `drills_200`, `first_video`, `first_anki`, `anki_100`, `first_recording`, `dictionary_contrib`, `exchange_partner`, `xp_500`, `xp_5000`.

#### RLS
- Authenticated: SELECT all.

---

### Table: user_achievements

Junction table recording which achievements each user has earned.

| Column | Type | Purpose |
|---|---|---|
| user_id | uuid | FK → user_profiles(id) ON DELETE CASCADE — composite PK |
| achievement_id | uuid | FK → achievements(id) ON DELETE CASCADE — composite PK |
| earned_at | timestamptz | When awarded |

PRIMARY KEY(user_id, achievement_id).

#### RLS
- User: SELECT own earned achievements.
- Python backend: INSERT via service role when milestone is reached.

---

## 4. Relationships Between Tables in This Domain

`feature_flag` and `tier_gating_rules` are independent read-only config tables (seeded by admin). `config` is a flat key-value store. `stripe_events` is a write-only audit log. `push_tokens` is user-managed device registration. `achievements` → `user_achievements` (junction: each user can earn each achievement once).

---

## 5. Cross-Domain Dependencies

### Tables in OTHER domains that this domain reads from
| External Table | Owned By | How We Use It | What Breaks If It Changes |
|---|---|---|---|
| app.user_profiles | identity_access | push_tokens.user_id, user_achievements.user_id | user_profiles.id |

### Tables in THIS domain that other projects use
| Our Table | Used By | How They Use It | What We Must Not Change |
|---|---|---|---|
| app.push_tokens | tauka-python | Reads tokens to send push notifications via service role | user_id, token, platform columns |
| app.stripe_events | tauka-python | Writes Stripe event audit log | stripe_event_id (idempotency) |

---

## 6. Extension Rules

#### If you need a new feature flag
INSERT a row into `app.feature_flag` with `status = 'coming'`. No app update needed — the flag appears in the Flutter client's "Coming Soon" list immediately.

#### If you need a new tier gate
INSERT a row into `app.tier_gating_rules`. Ensure `min_tier` uses the canonical values (`free`, `learner`, `tutor`, `intensive`).

#### If you need a new achievement
INSERT a row into `app.achievements`. Python awards it by inserting into `user_achievements` when the relevant milestone is detected.

#### If you need a new app config value
INSERT a row into `app.config`. Use a descriptive `key` string. Retrieve in Flutter via `SupabaseService`.

#### Specifically do NOT
- Do NOT use `'tutor_tier'` in `tier_gating_rules.min_tier` — use `'tutor'` (CONFLICT-003).
- Do NOT add `subscription_tier` CHECK constraint values outside `free | learner | tutor | intensive`.
- Do NOT expose `stripe_events` to Flutter clients — service role only.
- Do NOT duplicate push token registration — UNIQUE(user_id, token) prevents it; use upsert with ON CONFLICT DO NOTHING.
