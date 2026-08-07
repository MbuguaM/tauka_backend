# Architecture Index
> Auto-generated. Each project adds its own domains and tables.
> Last updated by: tauka-react-web on 2026-05-26

## Projects Contributing to This Database

| Project | Description | Domains Owned |
|---|---|---|
| tauka-flutter | Flutter offline-first language-learning app (iOS/Android/Windows/macOS). Handles all student and tutor UI, offline cache, real-time chat, exercises, AI chat, and video sessions. | identity_access, messaging, language_registry, course_content, social_media, video, ai, exercises_progress, test_referral, tutor_management, cohort, broadcast, platform_config, content_governance, student_tutor |
| tauka-python | Python backend service (FastAPI). Owns Stripe billing, tier management, milestone detection, payroll calculation, and pg_cron/Edge Function triggers. Writes to app.student (tier/stripe columns) via service role. | (writes to student_tutor domain's student table; reads user_profiles, video_session, tutor_sessions; owns email_verifications for OTP) |
| tauka-react-web | React/Vite marketing site + student/tutor account portal (www.tauka.com). Handles all payment flows, the public placement test UI, the supporter gift flow, and the account management portal. No in-app purchases — all Stripe interactions happen here. | (reads from test_referral, student_tutor, tutor_management, identity_access domains; all Stripe/business-logic writes go through FastAPI) |

---

## Domain Map

| Domain | File | Owner Project | Tables | One-Line Purpose |
|---|---|---|---|---|
| identity_access | domains/identity_access.md | tauka-flutter | user_profiles, admin_users, devices | Who is in the system and from which device |
| student_tutor | domains/student_tutor.md | tauka-flutter (+tauka-python writes) | student, tutor, student_course, tutor_course, tutor_assignment | The learner/teacher identity layer and enrollment graph |
| messaging | domains/messaging.md | tauka-flutter | classes, subclasses, class_members, conversations, conversation_participants, messages, message_read_receipts, notifications, notification_receipts, rate_limits | Real-time and async messaging within and across classes |
| language_registry | domains/language_registry.md | tauka-flutter | languages, user_languages | Reference table of supported languages and per-user language profile |
| course_content | domains/course_content.md | tauka-flutter | course, unit, unit_progress, progress, dictionary, anki_deck, anki_deck_assignment, course_correction, content_versions, program, contemporary | Structured lesson curriculum, flashcard decks, and the dictionary |
| social_media | domains/social_media.md | tauka-flutter | yt_playlist, yt_video, lyrics, langexchange, social, tk_tokens, tk_video | YouTube Explore feed, lyrics, and legacy social tables |
| video | domains/video.md | tauka-flutter | video_vendor_config, video_session, video_session_participant, video_session_rating, video_session_note, session_recordings, tutor_sessions | Scheduled live video sessions and associated artefacts |
| ai | domains/ai.md | tauka-flutter | ai_conversation, ai_message, ai_practice_session, ai_practice_turn | AI tutor chat sessions and AI drill/pronunciation practice |
| exercises_progress | domains/exercises_progress.md | tauka-flutter | exercise_result, handwriting_session, flashcard_reviews, voice_recordings, progress_snapshots | Student exercise outcomes, flashcard spaced-repetition state, and voice recordings |
| test_referral | domains/test_referral.md | tauka-python (+tauka-flutter reads) | test_questions, test_sessions, test_share_events, test_referrals, test_supporters, milestone_notifications, gift_subscriptions, testimonial_requests | Public placement test, supporter gift subscriptions, and milestone notifications |
| tutor_management | domains/tutor_management.md | tauka-python (+tauka-flutter reads) | tutor_availability, tutor_payout_settings, tutor_sessions, tutor_payroll, payroll_line_item, tutor_rates | Tutor scheduling, payout settings, earnings sessions, and payroll |
| cohort | domains/cohort.md | tauka-flutter | cohorts, cohort_memberships, cohort_payments, cohort_assignments | Admin-managed student cohorts, membership, payments, and assignments |
| broadcast | domains/broadcast.md | tauka-flutter | broadcast, broadcast_reaction, broadcast_comment, broadcast_reads, language_exchange_match | Tutor/admin broadcasts, reactions, comments, and language exchange matching |
| platform_config | domains/platform_config.md | tauka-python (+tauka-flutter reads) | feature_flag, tier_gating_rules, config, stripe_events, push_tokens, achievements, user_achievements | Feature flags, tier gating rules, app config, Stripe event log, push tokens, and XP achievements |
| content_governance | domains/content_governance.md | tauka-flutter | content_approval_requests, dictionary_contributions | Tutor-submitted content pending admin approval |

---

## Global Anti-Patterns

> These rules apply across ALL projects. Never remove entries; only append.

- Never store money as a float — all monetary amounts use integer cents (`amount_cents`) or `numeric(10,2)` for display-only values.
- Never create a generic key-value metadata table — use specific columns or a typed `jsonb` column instead.
- All timestamps are UTC, stored as `TIMESTAMPTZ` (PostgreSQL `timestamp with time zone`).
- Soft deletes use a `deleted_at TIMESTAMPTZ NULL` column. Never use a boolean `is_deleted` flag.
- All tables live in the `app` schema, never `public`. Access the schema explicitly: `app.<table>`.
- Never store sensitive credentials (Stripe keys, API tokens) in a DB column accessible by RLS-authenticated users. Use Supabase Vault or environment variables and expose only masked display values.
- Enum columns use `TEXT + CHECK` constraints, not PostgreSQL `ENUM` types. This allows `ALTER TABLE` without type migration.
- User identity is always `auth.uid()` from Supabase Auth — never trust a `user_id` supplied by the client without an RLS `USING (user_id = auth.uid())` guard.
- Never create `dictionary_entry` or `word_pair` tables — entries are stored inline as JSON arrays in `app.dictionary.dict` and `app.anki_deck.deck` respectively (see course_content domain).
- Never create `exercise_sessions` — the canonical exercise result table is `app.exercise_result` (singular).
- `subscription_tier` canonical values are: `free | learner | tutor | intensive`. Never use `tutor_tier`.
- The `app.user_profiles.subscription_tier` column is a denormalized cache kept in sync by `trg_sync_student_tier`. Never update it directly from Flutter — it is written by the trigger when `app.student.tier` changes.
- The Python backend (tauka-python) is the sole authority on `app.student.tier`, `stripe_customer_id`, and `stripe_subscription_id`. The Flutter client MUST NOT write these columns directly.
- The test/referral share flow supports exactly two channels: `whatsapp` and `copy_link`. The `email` channel was removed (see `partial_simplify_share.md`). `test_referrals.recipient_email` is kept in the schema but is no longer written.
- OTP email verification is used at the **mid-test email gate only** (purpose `'test_gate'`). The share/invite component and post-test share do NOT verify email via OTP — they rely on browser autofill only. The `email_verifications` table purpose column accepts only `'test_gate'`.

---

## Quick Lookup — All Tables Alphabetical

| Table | Domain | Owner | Read By | Write By |
|---|---|---|---|---|
| achievements | platform_config | tauka-python | tauka-flutter (reads) | tauka-python (seeds + updates) |
| email_verifications | test_referral | tauka-python | tauka-python only | tauka-python (OTP service) |
| admin_users | identity_access | tauka-flutter | tauka-flutter, tauka-python | tauka-flutter (admin dashboard) |
| ai_conversation | ai | tauka-flutter | tauka-flutter | tauka-flutter |
| ai_message | ai | tauka-flutter | tauka-flutter | tauka-flutter |
| ai_practice_session | ai | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| ai_practice_turn | ai | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| anki_deck | course_content | tauka-flutter | tauka-flutter | tauka-flutter |
| anki_deck_assignment | course_content | tauka-flutter | tauka-flutter | tauka-flutter (tutors) |
| broadcast | broadcast | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| broadcast_comment | broadcast | tauka-flutter | tauka-flutter | tauka-flutter |
| broadcast_reaction | broadcast | tauka-flutter | tauka-flutter | tauka-flutter |
| broadcast_reads | broadcast | tauka-flutter | tauka-flutter | tauka-flutter |
| class_members | messaging | tauka-flutter | tauka-flutter | tauka-flutter (teachers/admins) |
| classes | messaging | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| cohort_assignments | cohort | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| cohort_memberships | cohort | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| cohort_payments | cohort | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| cohorts | cohort | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| config | platform_config | tauka-python | tauka-flutter (reads) | tauka-python, tauka-flutter (admins) |
| contemporary | social_media | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| content_approval_requests | content_governance | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| content_versions | course_content | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| conversation_participants | messaging | tauka-flutter | tauka-flutter | tauka-flutter (via SECURITY DEFINER functions) |
| conversations | messaging | tauka-flutter | tauka-flutter | tauka-flutter |
| course | course_content | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| course_correction | course_content | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| devices | identity_access | tauka-flutter | tauka-flutter | tauka-flutter |
| dictionary | course_content | tauka-flutter | tauka-flutter (enrolled students + tutors) | tauka-flutter (admins) |
| dictionary_contributions | content_governance | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| exercise_result | exercises_progress | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| feature_flag | platform_config | tauka-python | tauka-flutter (reads) | tauka-python, tauka-flutter (admins) |
| flashcard_reviews | exercises_progress | tauka-flutter | tauka-flutter | tauka-flutter |
| gift_subscriptions | test_referral | tauka-python | tauka-flutter (reads), tauka-react-web (reads own rows via supabase-js) | tauka-python |
| handwriting_session | exercises_progress | tauka-flutter | tauka-flutter | tauka-flutter |
| language_exchange_match | broadcast | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| languages | language_registry | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| langexchange | social_media | tauka-flutter | tauka-flutter | tauka-flutter |
| lyrics | social_media | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| message_read_receipts | messaging | tauka-flutter | tauka-flutter | tauka-flutter |
| messages | messaging | tauka-flutter | tauka-flutter | tauka-flutter |
| milestone_notifications | test_referral | tauka-python | tauka-flutter (reads) | tauka-python |
| notification_receipts | messaging | tauka-flutter | tauka-flutter | tauka-flutter |
| notifications | messaging | tauka-flutter | tauka-flutter | tauka-flutter (teachers/admins) |
| payroll_line_item | tutor_management | tauka-python | tauka-flutter (tutors) | tauka-python (admins) |
| program | course_content | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| progress | course_content | tauka-flutter | tauka-flutter | tauka-flutter (legacy) |
| progress_snapshots | exercises_progress | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| push_tokens | platform_config | tauka-flutter | tauka-python (reads for push) | tauka-flutter |
| rate_limits | messaging | tauka-flutter | via SECURITY DEFINER only | tauka-flutter (via check_rate_limit()) |
| session_recordings | video | tauka-flutter | tauka-flutter (participants + tutors) | tauka-flutter (webhook) |
| social | social_media | tauka-flutter | tauka-flutter | tauka-flutter (unused) |
| stripe_events | platform_config | tauka-python | tauka-python | tauka-python |
| student | student_tutor | tauka-flutter + tauka-python | tauka-flutter, tauka-python, tauka-react-web (reads tier/active_gift_id/subscription_status/current_period_end via supabase-js) | tauka-flutter (basic), tauka-python (tier/stripe cols) |
| student_course | student_tutor | tauka-flutter | tauka-flutter | tauka-flutter (students) |
| subclasses | messaging | tauka-flutter | tauka-flutter | tauka-flutter (teachers/admins) |
| test_questions | test_referral | tauka-python | tauka-python | tauka-python |
| test_referrals | test_referral | tauka-python | tauka-flutter (own rows) | tauka-python |
| test_sessions | test_referral | tauka-python | tauka-python | tauka-python |
| test_share_events | test_referral | tauka-python | tauka-python | tauka-python |
| test_supporters | test_referral | tauka-python | tauka-flutter (own rows), tauka-react-web (reads own student's supporters via supabase-js) | tauka-python |
| testimonial_requests | test_referral | tauka-python | tauka-python, tauka-react-web (reads published rows for landing page via supabase-js) | tauka-python |
| tier_gating_rules | platform_config | tauka-python | tauka-flutter (reads) | tauka-python |
| tk_tokens | social_media | tauka-flutter | tauka-flutter (unused) | tauka-flutter (unused) |
| tk_video | social_media | tauka-flutter | tauka-flutter (unused) | tauka-flutter (unused) |
| tutor | student_tutor | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| tutor_assignment | student_tutor | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| tutor_availability | tutor_management | tauka-flutter | tauka-flutter, tauka-react-web (/tutor/schedule reads + writes own slots via supabase-js) | tauka-flutter (tutors), tauka-react-web (tutors manage own slots) |
| tutor_course | student_tutor | tauka-flutter | tauka-flutter | tauka-flutter (tutors) |
| tutor_payout_settings | tutor_management | tauka-python | tauka-flutter (masked display) | tauka-python |
| tutor_payroll | tutor_management | tauka-python | tauka-flutter (tutors) | tauka-python (admins) |
| tutor_rates | tutor_management | tauka-python | tauka-flutter (tutors) | tauka-python (admins) |
| tutor_sessions | tutor_management | tauka-python | tauka-flutter (tutors) | tauka-python |
| unit | course_content | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| unit_progress | course_content | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| user_achievements | platform_config | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| user_languages | language_registry | tauka-flutter | tauka-flutter | tauka-flutter |
| user_profiles | identity_access | tauka-flutter | tauka-flutter, tauka-python | tauka-flutter (own row), tauka-python (trigger-written tier) |
| video_session | video | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| video_session_note | video | tauka-flutter | tauka-flutter | tauka-flutter (tutors) |
| video_session_participant | video | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
| video_session_rating | video | tauka-flutter | tauka-flutter | tauka-flutter (students) |
| video_vendor_config | video | tauka-flutter | tauka-flutter (reads active vendor) | service_role (Edge Functions) |
| voice_recordings | exercises_progress | tauka-flutter | tauka-flutter, tutors | tauka-flutter |
| yt_playlist | social_media | tauka-flutter | tauka-flutter | tauka-flutter (tutors/admins) |
| yt_video | social_media | tauka-flutter | tauka-flutter | tauka-flutter (admins) |
