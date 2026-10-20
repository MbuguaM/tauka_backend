# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

- **Project name:** tauka-python
- **Description:** FastAPI backend for Tauka — handles Stripe billing, tier management, AI generation, video call tokens, placement tests, supporter/milestone notifications, and tutor payroll.
- **Tech stack:** Python/FastAPI + Supabase (PostgreSQL) + Redis + Stripe + OpenAI/DeepSeek/Gemini + Daily.co/LiveKit
- **This project's role in the larger system:** The sole server-side API in a three-project system (tauka-flutter, tauka-python, tauka-react-web). The only project with the Stripe secret key and the Supabase service role key. All writes to billing, tier, payroll, and test/referral tables go through this service.

## Commands

**Run dev server:**
```bash
uv run fastapi dev
```
Starts at `http://localhost:8000`. API docs at `/docs`.

Equivalent fallback, useful when the CLI misbehaves — it bypasses the console
script and the banner entirely:
```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

**Add dependencies:**
```bash
uv add <package>
```

### Troubleshooting the dev server (Windows)

**`Failed to canonicalize script path`** — the `.exe` launchers in
`.venv/Scripts/` hard-code an absolute path to the interpreter, so **moving or
renaming the project directory breaks every one of them** (`fastapi`, `uvicorn`,
`pytest`, `httpx`, …). Plain `uv sync` will NOT fix it: the installed packages
are still correct, so uv reports "Would make no changes" and skips regenerating
the launchers. Force it:
```bash
uv sync --reinstall
```
This reinstalls from `uv.lock`, so the package set is unchanged — only the
launchers are rewritten. Verify with `uv pip freeze` before and after.

**`UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f680'`** —
`fastapi dev` prints an emoji in its startup banner. When stdout is a pipe or a
redirect (CI, `> dev.log`, a captured terminal) Python falls back to the cp1252
locale encoding and the banner kills the process before the server ever starts.
An interactive console normally works because Python uses the Windows Unicode
console API instead. Make it deterministic:
```powershell
$env:PYTHONUTF8 = 1        # current session
setx PYTHONUTF8 1          # permanent; new terminals only
```
This also stops non-ASCII log output (e.g. the em-dash in the Redis warning)
rendering as mojibake.

**`Import error: No module named 'app'`** — every directory under `app/` needs
an `__init__.py`. Implicit namespace packages are enough for
`python -m uvicorn app.main:app`, which is why this can stay hidden for a long
time, but `fastapi dev`'s app discovery only walks real packages. Add the empty
`__init__.py` rather than switching to the uvicorn invocation.

**`Redis unavailable at redis://localhost:6379 … rate limiting will be skipped`**
— expected, not an error. Rate limiting fails open (PY-005); the API is fully
functional without Redis. Start it with `.\redis-dev.ps1 start` only when you
specifically want to exercise rate limits.

## Environment

Required variables in `.env`:

| Variable | Required | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | Yes | DeepSeek (primary AI provider) |
| `SUPABASE_URL` / `SUPABASE_KEY` | Yes | Supabase (service role key) |
| `SUPABASE_ANON_KEY` | Yes | Supabase anon key (RLS-respecting operations) |
| `SUPABASE_JWT_SECRET` | Yes | Verifying Supabase-issued JWTs |
| `REDIS_URL` | No | Redis (default: `redis://localhost:6379`) |
| `DAILY_API_KEY` | For calls | Daily.co room/token creation |
| `LIVEKIT_API_KEY` / `LIVEKIT_SECRET` | For calls | LiveKit JWT signing |
| `OPENAI_API_KEY` | Optional | OpenAI chat + vision (only if using openai provider) |
| `GEMINI_API_KEY` | Optional | Gemini chat + vision (only if using gemini provider) |
| `MODEL_NAME` | No | Default DeepSeek model (default: `deepseek-chat`) |
| `TOKEN_LIMIT` | No | Monthly token cap (default: `100000`) |
| `STRIPE_SECRET_KEY` | For billing | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | For billing | Stripe webhook signature secret |
| `STRIPE_PRICE_LEARNER_MONTHLY` | For billing | Stripe Price ID for Learner **$32/mo** (must match `pricing.jsx`, authoritative — the old $14 price object must not be reused) |
| `STRIPE_PRICE_TUTOR_MONTHLY` | For billing | Stripe Price ID for Tutor **$75/mo** (was $40) |
| `STRIPE_PRICE_INTENSIVE_MONTHLY` | For billing | Stripe Price ID for Intensive **$145/mo** (was $119) |
| `STRIPE_PRICE_GIFT_1MO` | For gifts | Stripe Price ID for 1-month gift ($40) |
| `STRIPE_PRICE_GIFT_3MO` | For gifts | Stripe Price ID for 3-month gift ($120) |
| `STRIPE_PRICE_ADDON_1V1_50` / `_1V1_25` / `_BUNDLE4_50` / `_BUNDLE4_25` | For add-ons | One-time Stripe prices for the pricing page's 1:1 add-ons (plan 2.6, §129). Empty = that product refuses checkout. Also needs the `addons_1v1` flag |
| `STRIPE_COUPON_GUARANTEE` | No | A 100 %-off coupon for the milestone guarantee's free months (§138). Empty = ops apply it by hand from `/admin/assessments` |
| `MPESA_ENV` | No | `sandbox` (default) or `production` (Daraja host) |
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` / `MPESA_SHORTCODE` / `MPESA_PASSKEY` | For M-Pesa | Daraja credentials (§130). Any empty = M-Pesa refuses with `not_configured`. Also needs the `mpesa_payments` flag |
| `MPESA_CALLBACK_TOKEN` | For M-Pesa | Secret path segment of `POST /mpesa/callback/{token}` (the callback URL is built from `API_BASE_URL`) |
| `MPESA_KES_LEARNER` / `_TUTOR` / `_INTENSIVE` | For M-Pesa | Monthly price per tier in **whole Kenyan shillings** (the KES Stripe prices must match) |
| `PUSH_ENABLED` | No | Default `false`. Turn on only once Firebase is configured and the `send-push` Edge Function is deployed (FC-008, O-20) |
| `WHATSAPP_PROVIDER` | No | `console` (default: logs, sends nothing) or `meta` (WhatsApp Cloud API). Also needs the `whatsapp_reminders` flag (L-011) |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` | For `meta` | Cloud API credentials |
| `WHATSAPP_VERIFY_TOKEN` / `WHATSAPP_APP_SECRET` | For `meta` | Webhook verification and `X-Hub-Signature-256` check on `POST /whatsapp/webhook` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_OPS_CHAT_ID` | No | One bot posting cover requests and no-shows to one ops group (plan 6.4). Either empty = off |
| `EMAIL_PROVIDER` | No | `"console"`, `"smtp"` or `"resend"` (default: `"resend"`). NOT `"sendgrid"` — see below |
| `EMAIL_API_KEY` | For `resend` | Resend API key |
| `EMAIL_FROM_ADDRESS` | No | Sender address (default: `Tauka <support@tauka.io>`). For `smtp` it must be `SMTP_USER` or a verified alias |
| `EMAIL_OUTBOX_DIR` | No | Where `console` writes rendered mail (default: `.outbox`) |
| `ADMIN_EMAIL` | No | Internal recipient for ops notifications (default: `support@tauka.io`). Empty disables them |
| `SMTP_HOST` / `SMTP_PORT` | For `smtp` | Defaults `localhost:1025` (Mailpit) |
| `SMTP_USER` / `SMTP_PASSWORD` | For `smtp` | Leave empty for Mailpit |
| `SMTP_STARTTLS` | No | Default `false`. Set `true` for port 587 |
| `SMTP_SSL` | No | Default `false`. Set `true` for port 465 (implicit TLS) |
| `CORS_ORIGINS` | No | Comma-separated browser origins allowed to call this API |
| `WEB_BASE_URL` | No | Marketing site URL (default: `https://tauka.io`). Injected into every email template |
| `API_BASE_URL` | No | This service's public origin (default: `https://api.tauka.io`). Email unsubscribe/opt-out links resolve to FastAPI routes here, not to the site |
| `TAUKA_ENV_FILE` | No | **A process env var, not a `.env` key.** Which file settings load (default `.env`). `.env.demo` points the service at the local Supabase stack — see `DEMO.md` at the monorepo root |

### JWT verification (`app/dependencies.py`, 2026-10-19)

`get_current_user` accepts **HS256** tokens against `SUPABASE_JWT_SECRET` (production's
legacy scheme today) **and ES256/RS256** tokens against the project's JWKS at
`SUPABASE_URL/auth/v1/.well-known/jwks.json`, which is what the local stack issues, and what
production will issue once Supabase moves the project to signing keys. The algorithm in the
header only picks the path. HS256 is checked against the secret alone; an asymmetric token is
checked only against the published key for its `kid`, whose own algorithm must match. An
unreachable JWKS returns 503, never a pass.

There is **no `APP_BASE_URL`** — removed 2026-08-08. Every destination this service
emits is a browser URL: Stripe `success_url` / `cancel_url` / `return_url`, and the
CTAs in email. Account and subscription management live on tauka-react-web by
design; the Flutter app is usage-only and cannot be a redirect target. The setting
defaulted to `https://app.tauka.com`, a host that exists nowhere in the system —
the app's real deep-link host is `app.tauka.app` (`tauka://` universal links),
which Stripe cannot redirect a browser to regardless. Use `WEB_BASE_URL`.

### Email providers

`email_service.py` dispatches on `EMAIL_PROVIDER`:

- **`console`** — renders each message to `EMAIL_OUTBOX_DIR` as a timestamped
  `.html` and logs a one-line summary. No network, no account, and it cannot
  mail a real person by accident. Use this for local work.
- **`smtp`** — any SMTP host. Defaults target a local catcher:
  `docker run -p 1025:1025 -p 8025:8025 axllent/mailpit`, inbox at
  `http://localhost:8025`.

  Against a real host, the TLS flag must match the port — they are two different
  protocols, not two settings for the same one:

  | Port | Set | Mechanism |
  |---|---|---|
  | 587 | `SMTP_STARTTLS=true`, `SMTP_SSL=false` | Connect in cleartext, upgrade via `STARTTLS` |
  | 465 | `SMTP_SSL=true` | TLS handshake on the first byte (implicit TLS / SMTPS) |
  | 1025 | both `false` | Mailpit, no TLS |

  `SMTP_SSL` wins if both are set — `STARTTLS` is not offered on an
  already-encrypted connection, so it is skipped rather than attempted.
  Getting this wrong does not refuse the connection, it **hangs until the 15s
  timeout on every send**, so `_provider()` logs an error when
  `SMTP_PORT=465` is paired with `SMTP_SSL=false`.

  The host authenticates `SMTP_USER`, but recipients see `EMAIL_FROM_ADDRESS`.
  Most providers (Zoho, Google Workspace, Fastmail) reject a `From` that is not
  the authenticated mailbox or a verified alias of it — keep the two consistent.
- **`resend`** — production. Needs `EMAIL_API_KEY` and a domain verified in Resend.

`"sendgrid"` appears in older docs but **has never been implemented**. An
unrecognised value now logs an error and falls back to `console` rather than
silently sending nothing. See `shared-features/CONFLICT_LOG.md` FC-014.

Sends are best-effort: callers are `BackgroundTasks` on user-facing flows, so a
mail outage must never fail the request that triggered it. Every path returns
`{"id", "status"}` and logs instead of raising — but a misconfiguration is
logged loudly rather than failing silently on the first send.

### CORS

`app/main.py` registers `CORSMiddleware` with origins from `CORS_ORIGINS`.
tauka-react-web is always a different origin (`:5173` in dev,
`www.tauka.com` in prod) and `api.js` sends `Content-Type: application/json`,
which is not CORS-safelisted — so the browser preflights every call with
`OPTIONS`. **Any new web origin must be added to `CORS_ORIGINS` or every
tauka-react-web call to this API fails**, and the placement test degrades to an
unscorable local question bank labelled "Preview mode". See
`shared-features/CONFLICT_LOG.md` FC-013.
| `ENCRYPTION_KEY` | For payouts | Fernet key for encrypting payout settings at rest |

### Server-side feature flags and the hourly job (2026-10-19)

Features from the enrolment plan's later tracks check `app.feature_flag` through
`app/core/flags.py` `is_ready(key)`, which **fails closed**: no row, a read error, or any
status but `ready` means off. They are seeded `coming` (§131). Flip a row to `ready` to turn
a feature on — no deploy — and flip the matching key in tauka-react-web's `featureStatus.js`.

`GET /tasks/hourly` (same `CRON_SECRET` as `/tasks/daily`) makes the Daily rooms for pod
sessions in the next 24 hours (`cohort_room_service`, 2026-10-20: nothing did before, so pod
Join could never light up; no URL → no `video_session` row), runs the exchange hourly job
(lapsed requests passed on, rooms 90 min ahead, unrated sessions settled), then sends the 24 h /
1 h session reminders (exchanges included) and seat-offer alerts. It needs an external hourly
scheduler; Vercel's cron here runs daily.

### Features addendum (2026-10-20, §142 / §143)

- `exchange_service` + `/exchange` (exchange partners) and `summary_service` + `/summaries`
  (lesson summaries), behind `exchange_partners`, `exchange_practise_now`,
  `exchange_message_flags`, `lesson_summaries`, `summary_questions` — all `coming`. Flow:
  `shared-features/flows/exchange.md`; domain: `tauka/shared-db/domains/exchange.md`.
- **The partner role is written here** (`join_partner` creates `user_profiles` with
  `user_type='partner'`), the same precedent as `/auth/provision`. The learner language columns
  on `user_profiles` are **not**: the app writes its own row; `/summaries/prefs` only reads.
- **X-16:** summaries and AI task-card drafts never contain model-written Fidel. The model gets
  the lesson's words as a numbered list and writes `{{n}}`; `app/core/fidel_translit.py` gives
  the transliteration. A reply with Ethiopic is retried, then refused.
- WhatsApp template `exchange_request` ({1} = local time) must be registered with Meta before
  `WHATSAPP_PROVIDER=meta` sends exchange requests; reminders reuse `session_reminder_*`.

### Two traps found 2026-10-20

- **`client.schema("app")` used to build a new PostgREST client — and an httpx client that
  loads the CA bundle — on every call** (≈0.5 s per query on Windows). `supabase_client.py`
  now caches one per schema (`_cache_schemas`); `app/core/supabase_client.py` uses the same.
- **Never `.in_(col, ids or ["-"])` on a uuid column**: PostgREST rejects the whole query with
  22P02. FakeDB accepts it, so the unit tests stay green. Return early on an empty list, or use
  the nil UUID `00000000-0000-0000-0000-000000000000` (matches nothing).

## Architecture

Layered under `app/` (routes → services → core), readable from `ls app/`. Two rules
that the layout does not tell you:

- `migrations/` — SQL migration files, **never edit existing ones**; add a new file.
- All Supabase tables live in the **`app` schema** — use `supabase.schema("app").table(...)`, never omit the qualifier.

## AI Service (`POST /ai/generate`)

Providers and modes are dispatched in `ai_services.call_ai()`; the request schema is
in `app/models/schemas.py`. Note `deepseek` has no `image_translation` support.

**Request flow:**
1. `routes/ai.py` — validates image_url present for image_translation
2. `rate_limit_service.py` — checks Redis per `(provider, user_id)` — each provider has independent limits; image requests add a 1,000-token surcharge
3. `ai_services.call_ai()` — dispatches to the correct provider + mode function
4. `token_service.count_tokens()` — uses tiktoken cl100k_base for unknown models (DeepSeek, Gemini)
5. `usage_service.log_usage()` — fire-and-forget background task; event type = `ai_tokens:{provider}:{mode}`

## Calling Service (`POST /calls/token`)

Vendor selection order: request override → `app.video_vendor_config` active row → `"daily"`.
`room_url` is null for LiveKit.

The active vendor lives in `app.video_vendor_config`, which enforces a **single active
row via a partial unique index** — so the vendor can be switched server-side with no
client release.

## Messaging Service (`POST /messages/send`)

Aligned with the DB schema — keyed on `conversation_id`, **not** a sender/receiver pair.
Always call `messaging_service.get_or_create_student_conversation(s1, s2)` (which wraps
the `app.get_or_create_student_conversation` RPC) to obtain the id before sending.

## Lyric Annotation Service (`POST /ai/lyrics/annotate`)

Separate from `/ai/generate` on purpose — `routes/lyric_annotation.py`,
`services/lyric_annotation_service.py`, prompt in `ai_prompts.build_lyric_annotate`.

- **Its own rate-limit namespace**, `lyric_annotation` (60 rpm, 250k tokens/day),
  keyed by the tutor. Do **not** raise the `deepseek` plan to fit content
  production — that is the student chat budget and raising it hands every student
  a bigger allowance.
- **Ledger rows carry `mode='lyric_annotate'`.** `app.ai_usage` had zero rows
  before this feature, so these are the first it will ever hold. The stats page
  must exclude non-chat modes explicitly, or every per-student number it shows
  includes work no student did.
- **`_call_deepseek` now takes `max_tokens`, `timeout` and returns
  `(content, finish_reason)`.** Without an explicit `max_tokens` the provider
  default of 4096 applies, and a whole annotated song is 6,000+ output tokens —
  the response came back truncated with no error. `finish_reason == "length"` is
  what drives the split-on-overflow path; without it, truncation is
  indistinguishable from a model that dropped half the lines.
- **One request per song, split only on overflow.** Chunking is not a quality
  mechanism here — the corpus was generated whole-song — it is what happens when
  a response runs out of room.
- **`lyric_annotate` is NOT in `FRAMED_MODES`.** It uses the same nonce fence,
  but that tuple drives `/ai/generate`'s dispatch, and listing it there made a
  500 reachable from a client-supplied mode string.

## Rate Limiting

Redis key namespace: `rate:{provider}:{user_id}:{minute}` and
`tokens:{provider}:{user_id}:day|month:{date}`. Per-provider limits are defined in
`rate_limit_service.py`; each provider is limited independently.

## Adding a New Feature

Follow the existing layering (schema → service → route → register in `app/main.py`), plus:

- All I/O must use `async def` / `await`
- Usage logging must never raise — wrap in try/except

---

## Shared Database — CRITICAL

This project shares a database with `tauka-flutter` and `tauka-react-web`. The shared schema documentation lives in `shared-db/` at the project root.

### Files You Must Read Before Touching the Database

| File | When to Read | Purpose |
|---|---|---|
| `shared-db/ARCHITECTURE_INDEX.md` | ALWAYS before any DB work | Quick lookup of all domains, tables, and owners |
| `shared-db/OWNERS.md` | ALWAYS before any DB work | Who owns what, what you're allowed to touch |
| `shared-db/domains/[relevant].md` | Before modifying or querying a specific domain | Full rationale, extension rules, anti-patterns |
| `shared-db/schema/db.sql` | When writing queries or migrations | Canonical column names, types, constraints |
| `shared-db/CONFLICT_LOG.md` | Before creating new tables | Check if your idea was already flagged as a conflict |

### What This Project Owns

This project OWNS the following domains and may CREATE/ALTER their tables:

- **test_referral** → test_questions, test_sessions, test_share_events, test_referrals, test_supporters, milestone_notifications, gift_subscriptions, testimonial_requests, email_verifications
- **tutor_management** → tutor_sessions, tutor_payout_settings, tutor_referral_link, referral_signup, rate_card, tutor_rate_version, float_ledger, payout_run, payout_line
  - `tutor_payroll`, `payroll_line_item` and `tutor_rates` were **DROPPED 2026-08-27 (§75)** — see CONFLICT-040. Do not re-create them, and do not repair `supabase/functions/payroll/`.
- **platform_config** → feature_flag, tier_gating_rules, config, stripe_events, achievements

### What This Project Reads But Does NOT Own

These tables are owned by other projects. You may SELECT from them but NEVER create, alter, drop, or write to them (except the shared-write rules below):

- `user_profiles` (owned by tauka-flutter) — **we now CREATE the row** at signup (`POST /auth/provision`), with `user_type` hardcoded to `'student'` and `age_confirmed_at` written from the web's 18+ checkbox (L-005). Otherwise read-only: reads for user data and admin role checks. Never write `subscription_tier` — trigger-owned.
- `admin_users` (owned by tauka-flutter) — reads for admin permission checks
- `video_session` (owned by tauka-flutter) — reads `id`, `tutor_id`, `scheduled_at`, `status` for earnings calculation
- `tutor` (owned by tauka-flutter) — reads `country_code` to select a rate card, and `created_at` for tenure. **`per_session_rate_cents` is ORPHANED (§75)** — held at 0 and read only as a fallback in `get_earnings`
- `cohort_session` / `one_to_one_session` (owned by tauka-flutter) — read to count **delivered** sessions for a payout run. `cohort_session.lead_tutor_id` is who *led* the occurrence and may be a guest
- `tutor_assignment` (owned by tauka-flutter) — reads for cohort tutor–student mapping
- `push_tokens` (owned by tauka-flutter) — reads token list for FCM push dispatch

### What This Project Writes To (Shared-Write)

- `student` (schema owned by tauka-flutter) — **we now CREATE the row** at signup (`POST /auth/provision`, 2026-08-28: signup moved to tauka-react-web and this service provisions the rows). The insert carries **`id` only** — never `tier`, which the column default owns until billing changes it. We may also UPDATE `tier`, `stripe_customer_id`, `stripe_subscription_id`, `subscription_status`, `current_period_end`, `active_gift_id` via service role. NEVER delete, and never write any other columns. The trigger `trg_sync_student_tier` automatically syncs `user_profiles.subscription_tier` — do NOT write that column directly.

---

## Database Change Workflow — MANDATORY

Follow these steps IN ORDER for any database-related work. Do not skip steps.

### Adding a Column to a Table We Own

1. Read `shared-db/ARCHITECTURE_INDEX.md` — confirm we own the table
2. Read `shared-db/domains/[domain].md` — check extension rules for guidance
3. Check the "Cross-Domain Dependencies" section — see if other projects depend on this table in ways your change could break
4. Add the column to `shared-db/schema/db.sql` with full annotation
5. Update the domain file's column table and any affected sections
6. Write the migration in `migrations/`

### Adding a New Table to a Domain We Own

1. Read `shared-db/ARCHITECTURE_INDEX.md` — confirm we own the domain
2. Read `shared-db/domains/[domain].md` — check extension rules
3. Check `shared-db/schema/db.sql` for any table with a similar name/purpose
4. Check `shared-db/CONFLICT_LOG.md` — has this been discussed before?
5. Create the table in `shared-db/schema/db.sql` with full annotations (`[DOMAIN:]`, `[OWNER: tauka-python]`, `[SPEC:]` tags and `PURPOSE:` / `USED BY:` comments)
6. Add full documentation to the relevant `shared-db/domains/[domain].md`
7. Update `shared-db/ARCHITECTURE_INDEX.md` — add to domain table list and alphabetical lookup
8. Update `shared-db/OWNERS.md` — add to table ownership
9. Write the migration

### Adding a New Domain

1. Confirm no existing domain covers this concept — read ALL domain files, not just the index
2. Propose by creating `shared-db/domains/[new-domain].md`
3. Add it to `shared-db/ARCHITECTURE_INDEX.md`
4. Add ownership to `shared-db/OWNERS.md`
5. **STOP and inform the developer** — new domains affect the whole system and need human approval

### Querying a Table We Don't Own

1. Read `shared-db/domains/[owning-domain].md` — understand purpose, columns, access patterns
2. Check the "What this table is NOT for" section — ensure your query aligns with intended use
3. Use only documented columns. If you need a column that doesn't exist, log a `SCHEMA_REQUEST` in `shared-db/CONFLICT_LOG.md` and add a `# NEEDS: [column] on [table]` TODO in your code
4. Add your usage to the domain file under "Usage by Other Projects"

### When You Find a Conflict

If you discover a duplicated table, mismatched column type, missing table, or a broken FK:
1. Log it in `shared-db/CONFLICT_LOG.md` with full details
2. Add a comment in your code: `# CONFLICT: see CONFLICT_LOG.md #[number]`
3. Use a workaround and document it
4. Inform the developer — do NOT silently fix it

---

## Spec and Requirements

- **Spec file:** `spec.md` (full multi-segment spec for all backend features)
- **The spec is READ ONLY — never modify it**
- Every table must trace back to a spec section
- If a feature request doesn't map to the spec, flag it to the developer

## Code Conventions

- All timestamps are UTC, stored as `TIMESTAMPTZ`
- Money is stored as **integer minor units**, never float. Single-currency tables use `amount_cents`; anything spanning currencies uses `*_minor` **plus an explicit `currency` (ISO 4217, NOT NULL)** — §75 onwards, because a rate card per country (D-17) means "cents" is wrong for a Ugandan shilling. `numeric` is for an FX **rate** only, never an amount
- A money balance is **never a column**: `float_ledger` and `session_entitlement` are append-only and a balance is a fold. Never fold across currencies
- IDs are UUID — never mix with serial integers
- Soft deletes use `deleted_at TIMESTAMPTZ NULL`, never boolean flags
- Table names are **singular snake_case**: `student`, not `students`
- Foreign key columns: `[table]_id` (e.g., `student_id`, `supporter_id`)
- Enum columns use `TEXT + CHECK` constraints, not PostgreSQL `ENUM` types
- All tables live in the `app` schema — never `public`
- `subscription_tier` canonical values: **`free_tier | learner_tier | tutor_tier | intensive_tier`**
  (renamed 2026-08-24, migration §64 — supersedes CONFLICT-003). The `_tier` suffix
  keeps tiers disjoint from **roles**, where `tutor` also lives.
  **This project is the one that can break billing with a mismatch:** it writes
  `app.student.tier` from Stripe webhooks via the service role, which bypasses RLS
  but *not* CHECK constraints. A stale literal here does not fail loudly — the
  webhook writes, Postgres rejects, and a paying customer silently keeps their old
  tier. `TIER_RANK`, `_PRICE_MAP` and the price-id branches in `stripe_service.py`
  must all use the suffixed values.
- The test/referral share flow supports exactly two channels: `whatsapp` and `copy_link` — `email` channel was removed

## Testing Requirements

- Every new table must have at least one test that verifies:
  - Row creation with valid data succeeds
  - Required constraints are enforced (NOT NULL, UNIQUE, FK, CHECK)
- Every new query against a table we don't own must include a comment explaining what we expect from that table's schema

## Reminders

- NEVER create a table without checking if it already exists in `shared-db/`
- NEVER modify a table you don't own — not even "small" changes
- NEVER assume you know the schema from memory — always read the files
- ALWAYS update `shared-db/` documentation BEFORE writing migrations
- ALWAYS check `shared-db/CONFLICT_LOG.md` before creating anything new
- The Python backend uses `supabase.schema("app").table(...)` for all table access — never omit the schema qualifier
- When in doubt, ask the developer rather than guessing
