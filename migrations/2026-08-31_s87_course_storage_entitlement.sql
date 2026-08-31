-- s87 — Entitlement-scope reads of the Tauka_Courses storage bucket.
-- Owner: tauka-flutter (course_content domain owns this bucket's policies).
-- Raised by: desktop local-storage security review, 2026-08-31.
--
-- WHY
-- The bucket is private, which only ever meant "you need an account". The one
-- SELECT policy on it was:
--
--     "allow all users to fetch files 1bje9mf_0"
--     USING (bucket_id = 'Tauka_Courses')  TO authenticated
--
-- No enrolment check, no tier check. Any account — and signup is free and
-- self-serve on the website — could list and download every object in the
-- bucket: all course units, all unit audio, the three Amharic dictionaries
-- (66 MB) and the course audio packs (34 MB). `SupabaseService` already
-- exposes `storage.from(bucket).list(path:)`, so enumeration needs no
-- guesswork. This is reachable without the app, so no amount of client-side
-- encryption addresses it.
--
-- WHAT THIS DOES
-- Replaces that policy with one that asks whether the caller is entitled to
-- the specific course the object belongs to.
--
-- Three deliberate exemptions, each because gating them breaks something a
-- person needs BEFORE they are entitled:
--   * course_cover/**        — the course picker renders covers pre-enrolment
--                              (courses.dart:629,771; detail_course.dart:59).
--   * **/table_of_contents.* — the syllabus preview. Metadata, not payload.
--   * explore/**             — gated on tier instead of enrolment, matching
--                              tier_gating_rules.explore_download = learner_tier.
--
-- SLUG MAPPING
-- Object paths are `<prefix>/<language-slug>/...` while app.course keys on a
-- uuid, and there is no slug column. The mapping is derived from the course's
-- OWN recorded storage paths (toc_url, dict_url) with lower(title) as a
-- fallback — deliberately not from the title alone, so renaming a course does
-- not silently revoke access to its content. Adding app.course.slug is the
-- proper fix and is logged as a SCHEMA_REQUEST.
--
-- ROLLBACK
--   drop policy "course_objects_read" on storage.objects;
--   create policy "allow all users to fetch files 1bje9mf_0"
--     on storage.objects for select to authenticated
--     using (bucket_id = 'Tauka_Courses');

create or replace function app.course_object_slug_matches(
  p_slug text,
  p_course app.course
) returns boolean
language sql
immutable
as $function$
  select p_slug = lower(coalesce(p_course.title, ''))
      or p_slug = lower(split_part(coalesce(p_course.toc_url, ''), '/', 2))
      or exists (
           select 1
           from unnest(coalesce(p_course.dict_url, array[]::text[])) as d
           where lower(split_part(d, '/', 2)) = p_slug
         );
$function$;

comment on function app.course_object_slug_matches(text, app.course) is
  'Does a Tauka_Courses path segment name this course? Derived from the '
  'course''s own stored object paths so a title change cannot revoke content.';

create or replace function app.can_read_course_object(p_name text)
returns boolean
language sql
stable
security definer
set search_path to 'app', 'public'
as $function$
  select case split_part(p_name, '/', 1)
    -- Shop window: shown before anyone is entitled to anything.
    when 'course_cover' then true

    -- Paid surface, gated on tier rather than course enrolment.
    when 'explore' then
      app.is_tutor_or_admin()
      or exists (
           select 1 from app.student s
           where s.id = auth.uid()
             and s.tier in ('learner_tier', 'tutor_tier', 'intensive_tier')
         )

    -- Course payload: units, unit audio, dictionaries, decks.
    else
      -- The syllabus is a preview, not the goods.
      split_part(p_name, '/', 3) like 'table\_of\_contents%'
      or exists (
           select 1 from app.course c
           where c.deleted_at is null
             and app.course_object_slug_matches(
                   lower(split_part(p_name, '/', 2)), c)
             and app.is_enrolled_in_course(c.id)
         )
  end;
$function$;

comment on function app.can_read_course_object(text) is
  'Storage read gate for the Tauka_Courses bucket. A table''s RLS does not '
  'protect a file — this is the file-side twin of app.is_enrolled_in_course().';

grant execute on function app.can_read_course_object(text) to authenticated;
grant execute on function app.course_object_slug_matches(text, app.course) to authenticated;

drop policy if exists "allow all users to fetch files 1bje9mf_0" on storage.objects;

create policy "course_objects_read"
  on storage.objects
  for select
  to authenticated
  using (
    bucket_id = 'Tauka_Courses'
    and app.can_read_course_object(name)
  );
