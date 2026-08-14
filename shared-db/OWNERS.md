# Ownership Map
> Each project fills in its own rows. Never modify another project's rows.
> Last updated by: tauka-react-web on 2026-05-26

## Ownership Rules

1. Only the owning project may `CREATE TABLE` or `ALTER TABLE` in its domain.
2. Other projects get READ access — query only, never DDL.
3. Shared-write tables are explicitly marked — all others are write-owner-only.
4. If you need a column on a table you don't own, add to `CONFLICT_LOG.md` as a `SCHEMA_REQUEST`.
5. If two projects both claim ownership of the same table, that is a CONFLICT — log it.
6. The `app` schema is used for all tables. `public` schema is not used.
   *One documented exception:* `web.interest` (tauka-react-web) — see below.
7. The Python backend (tauka-python) uses `supabase.schema("app").table(...)` for all table access.
8. The Flutter client (tauka-flutter) uses Supabase Dart SDK with `.schema('app')` qualifier.

---

## Table Ownership

| Table | Owner Project | Domain | Access Granted To |
|---|---|---|---|
| user_profiles | tauka-flutter | identity_access | tauka-python reads for user data; trigger writes subscription_tier via trg_sync_student_tier |
| admin_users | tauka-flutter | identity_access | tauka-python reads for admin check |
| devices | tauka-flutter | identity_access | tauka-flutter only |
| student | tauka-flutter (schema); tauka-python (tier/stripe cols) | student_tutor | SHARED WRITE — see Shared-Write Tables below |
| tutor | tauka-flutter | student_tutor | tauka-python reads for rate calculation |
| student_course | tauka-flutter | student_tutor | tauka-flutter only |
| tutor_course | tauka-flutter | student_tutor | tauka-flutter only |
| tutor_assignment | tauka-flutter | student_tutor | tauka-python reads for cohort tutor-student mapping |
| classes | tauka-flutter | messaging | tauka-flutter only |
| subclasses | tauka-flutter | messaging | tauka-flutter only |
| class_members | tauka-flutter | messaging | tauka-flutter only |
| conversations | tauka-flutter | messaging | tauka-flutter only |
| conversation_participants | tauka-flutter | messaging | tauka-flutter only (via SECURITY DEFINER functions) |
| messages | tauka-flutter | messaging | tauka-flutter only |
| message_read_receipts | tauka-flutter | messaging | tauka-flutter only |
| notifications | tauka-flutter | messaging | tauka-flutter only |
| notification_receipts | tauka-flutter | messaging | tauka-flutter only |
| rate_limits | tauka-flutter | messaging | accessed only via check_rate_limit() SECURITY DEFINER function |
| languages | tauka-flutter | language_registry | tauka-flutter only |
| user_languages | tauka-flutter | language_registry | tauka-flutter only |
| course | tauka-flutter | course_content | tauka-flutter only |
| unit | tauka-flutter | course_content | tauka-flutter only |
| unit_progress | tauka-flutter | course_content | tauka-flutter only |
| progress | tauka-flutter | course_content | tauka-flutter only (legacy) |
| dictionary | tauka-flutter | course_content | tauka-flutter (enrolled students + tutors read) |
| anki_deck | tauka-flutter | course_content | tauka-flutter only |
| anki_deck_assignment | tauka-flutter | course_content | tauka-flutter only |
| course_correction | tauka-flutter | course_content | tauka-flutter only |
| content_versions | tauka-flutter | course_content | tauka-flutter only |
| program | tauka-flutter | course_content | tauka-flutter only |
| contemporary | tauka-flutter | course_content | tauka-flutter only |
| yt_playlist | tauka-flutter | social_media | tauka-flutter only |
| yt_video | tauka-flutter | social_media | tauka-flutter only |
| lyrics | tauka-flutter | social_media | tauka-flutter only |
| langexchange | tauka-flutter | social_media | tauka-flutter only |
| social | tauka-flutter | social_media | tauka-flutter only (unused) |
| tk_tokens | tauka-flutter | social_media | tauka-flutter only (unused) |
| tk_video | tauka-flutter | social_media | tauka-flutter only (unused) |
| video_vendor_config | tauka-flutter | video | write: service_role (Edge Functions); read: authenticated users |
| video_session | tauka-flutter | video | tauka-python reads for earnings calculation |
| video_session_participant | tauka-flutter | video | tauka-flutter only |
| video_session_rating | tauka-flutter | video | tauka-flutter only |
| video_session_note | tauka-flutter | video | tauka-flutter only |
| session_recordings | tauka-flutter | video | tauka-flutter only |
| tutor_sessions | tauka-python | tutor_management | tauka-flutter reads (tutors see own sessions) |
| ai_conversation | tauka-flutter | ai | tauka-flutter only |
| ai_message | tauka-flutter | ai | tauka-flutter only |
| ai_practice_session | tauka-flutter | ai | tauka-flutter only |
| ai_practice_turn | tauka-flutter | ai | tauka-flutter only |
| exercise_result | tauka-flutter | exercises_progress | tauka-flutter only |
| handwriting_session | tauka-flutter | exercises_progress | tauka-flutter only |
| flashcard_reviews | tauka-flutter | exercises_progress | tauka-flutter only |
| voice_recordings | tauka-flutter | exercises_progress | tauka-flutter only |
| progress_snapshots | tauka-flutter | exercises_progress | tauka-flutter only |
| test_questions | tauka-python | test_referral | tauka-python only |
| test_sessions | tauka-python | test_referral | tauka-python only |
| test_share_events | tauka-python | test_referral | tauka-python only |
| test_referrals | tauka-python | test_referral | tauka-flutter reads own rows (sender_student_id = auth.uid()) |
| test_supporters | tauka-python | test_referral | tauka-flutter reads own rows (student_id = auth.uid()) |
| milestone_notifications | tauka-python | test_referral | tauka-flutter reads own rows (student_id = auth.uid()) |
| gift_subscriptions | tauka-python | test_referral | tauka-flutter reads own rows (student_id = auth.uid()) |
| testimonial_requests | tauka-python | test_referral | tauka-python only (admin writes); tauka-react-web reads published rows via supabase-js (public RLS: status='published') |
| email_verifications | tauka-python | test_referral | tauka-python only (OTP service writes/reads via service role); no direct Flutter or web client access |
| tutor_availability | tauka-flutter | tutor_management | tauka-flutter (tutors manage own slots); tauka-react-web (tutors read + upsert own slots via supabase-js on /tutor/schedule) |
| tutor_payout_settings | tauka-python | tutor_management | tauka-flutter reads masked display (details_last4 only) |
| tutor_payroll | tauka-python | tutor_management | tauka-flutter reads (tutors see own rows) |
| payroll_line_item | tauka-python | tutor_management | tauka-flutter reads (via tutor_payroll join) |
| tutor_rates | tauka-python | tutor_management | tauka-flutter reads own rows |
| cohorts | tauka-flutter | cohort | tauka-flutter only |
| cohort_memberships | tauka-flutter | cohort | tauka-flutter only |
| cohort_payments | tauka-flutter | cohort | tauka-flutter only |
| cohort_assignments | tauka-flutter | cohort | tauka-flutter only |
| broadcast | tauka-flutter | broadcast | tauka-flutter only |
| broadcast_reaction | tauka-flutter | broadcast | tauka-flutter only |
| broadcast_comment | tauka-flutter | broadcast | tauka-flutter only |
| broadcast_reads | tauka-flutter | broadcast | tauka-flutter only |
| language_exchange_match | tauka-flutter | broadcast | tauka-flutter only |
| feature_flag | tauka-python | platform_config | tauka-flutter reads all flags; admins write |
| tier_gating_rules | tauka-python | platform_config | tauka-flutter reads all rules |
| config | tauka-python | platform_config | tauka-flutter reads all config |
| stripe_events | tauka-python | platform_config | tauka-python only (service role) |
| push_tokens | tauka-flutter | platform_config | tauka-python reads for push dispatch |
| achievements | tauka-python | platform_config | tauka-flutter reads; tauka-python seeds |
| user_achievements | tauka-flutter | platform_config | tauka-flutter only |
| content_approval_requests | tauka-flutter | content_governance | tauka-flutter only |
| dictionary_contributions | tauka-flutter | content_governance | tauka-flutter only |
| **web.interest** | **tauka-react-web** | marketing_capture | tauka-react-web only. NOT in the `app` schema — see the note below |

---

## The `web` schema — the one documented exception to Rule 6

`web.interest` is the sole table outside `app`. It is **marketing-site signup
capture** (`email`, `country`, `phone`, `languages[]`, `category`,
`pioneer_number`), written by the public landing page before a user account
exists — so it sits deliberately outside the authenticated `app` surface.

- **Owner: tauka-react-web.** Only that project may `CREATE`/`ALTER` it.
- **No API role has USAGE on the `web` schema** (`anon`, `authenticated`,
  `service_role` all lack it), so it is not reachable over PostgREST despite
  carrying table-level grants. Those grants are vestigial.
- **RLS is enabled** with a single `INSERT` policy (`"public Insert"`). The
  former `"Enable read access for all users"` SELECT policy was dropped in §52 —
  it was `USING (true)` to PUBLIC and would have exposed every signup's email
  and phone the moment anyone granted schema USAGE.
- Ownership assigned 2026-08-09 (CONFLICT-016). tauka-flutter applied the §52
  security fix before ownership existed; further changes belong to
  tauka-react-web.
- Not a precedent. Rule 6 stands: new tables go in `app`.

---

## Shared-Write Tables

> Tables where multiple projects may INSERT or UPDATE rows.

| Table | Schema Owner | Projects That Write | Rules |
|---|---|---|---|
| app.student | tauka-flutter | tauka-flutter (creates row on sign-up, updates tutor_id), tauka-python (writes tier, stripe_customer_id, stripe_subscription_id, subscription_status, current_period_end, active_gift_id via service role) | tauka-flutter MUST NOT write tier/stripe columns directly. tauka-python MUST NOT create/delete student rows. The trigger trg_sync_student_tier keeps user_profiles.subscription_tier in sync automatically — do not duplicate this write. |
| app.user_profiles | tauka-flutter | tauka-flutter (own-row updates), trigger trg_sync_student_tier (subscription_tier, tier_updated_at written automatically when student.tier changes) | tauka-flutter MUST NOT update subscription_tier or user_type directly from client code. The §48 RLS policy blocks self-promotion of these columns. |
| app.push_tokens | tauka-flutter | tauka-flutter (INSERT/DELETE own tokens), tauka-python (reads token list for FCM dispatch — read only) | append-only from Flutter; tauka-python never writes |

---

## Cross-Project Dependencies

> What breaks if a table changes.

| If This Table Changes... | These Projects Are Affected | Specifically... |
|---|---|---|
| app.student (columns tier, stripe_customer_id, stripe_subscription_id) | tauka-python | Python billing service reads and writes these exact column names. Renaming any of them breaks Stripe webhook processing and tier assignment. |
| app.user_profiles (columns id, subscription_tier, user_type, streak_days, xp_total) | tauka-flutter | AuthStateManager._fetchAndSetRole() reads subscription_tier and user_type. TierGatingService subscribes to Realtime on this table. Changing column names breaks both. |
| app.user_profiles (id column type or FK to auth.users) | tauka-python | Python uses user_profiles.id as the foreign key anchor for all tutor/student lookups. |
| app.video_session (id, tutor_id, scheduled_at, status) | tauka-python | Python reads these for monthly earnings calculation and no-show detection. |
| app.tutor_sessions (tutor_id, started_at, per_session_rate_cents join via tutor) | tauka-python | get_tutor_monthly_earnings() RPC depends on this exact structure. |
| app.student (id, tutor_id, tutor_ended_at) | tauka-python | Python uses tutor_id and tutor_ended_at to determine active tutor–student assignments. |
| app.ai_message (conversation_id column name) | tauka-flutter | aiChatStateManager.dart queries ai_message by conversation_id. This MUST remain `conversation_id` — do NOT rename to `ai_conversation_id` (divergence note D1). |
| app.exercise_result (table name — singular) | tauka-flutter | supabaseService.dart references this table by name. Table must remain singular `exercise_result`. |
| app.student (columns tier, active_gift_id, subscription_status, current_period_end, tier_source) | tauka-react-web | /account page reads these columns directly via supabase-js to render the student portal dashboard and subscription management UI. Renaming any of these breaks the account portal. |
| app.test_supporters (columns supporter_email, supporter_name, student_id, status, student_visible, gift_nudge_shown) | tauka-react-web | /account/supporters page reads the student's supporter list via supabase-js. Columns supporter_email, supporter_name, and student_visible must remain unchanged. |
| app.gift_subscriptions (columns student_id, tier, duration_months, amount_cents, status, expires_at, anonymous, activated_at) | tauka-react-web | /account/supporters page shows active and past gifts. All listed columns are rendered in the UI. |
| app.testimonial_requests (columns quote_text, display_name, display_preference, status, published_at) | tauka-react-web | Landing page (/) reads published testimonials via supabase-js with no auth. status='published' RLS must remain. |
| app.tutor_availability (all columns) | tauka-react-web | /tutor/schedule reads and upserts availability slots. The RLS policy `tutors_manage_own_availability` must remain (tutor_id = auth.uid()). |
