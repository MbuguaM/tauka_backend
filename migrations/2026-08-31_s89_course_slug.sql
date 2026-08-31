-- ============================================================
-- §s89 (2026-08-31) — app.course.slug
-- Closes SCHEMA_REQUEST-050. Owner: tauka-flutter (course_content).
--
-- Storage paths in Tauka_Courses are `<prefix>/<language-slug>/...` while
-- app.course keys on a uuid. s87's storage read policy had to derive the slug
-- from the course's own toc_url/dict_url with lower(title) as a fallback,
-- because there was no column to ask. That worked, but coupled content access
-- to a display string and to a column the domain doc already marks DEPRECATED
-- (dict_url) — and a course renamed to e.g. 'Amharic (Beginner)' would have
-- fallen through to a slug nothing matches, revoking content for every
-- enrolled student from an innocuous edit.
--
-- `speaker_for` could not serve. The domain doc describes it as "the language
-- this course teaches (e.g. 'amharic')" but the live row holds 'English' — the
-- language taught FROM. See CONFLICT-052.
-- ============================================================

ALTER TABLE app.course ADD COLUMN IF NOT EXISTS slug text;

COMMENT ON COLUMN app.course.slug IS
  'Storage path segment for this course''s objects in the Tauka_Courses bucket: dictionaries/<slug>/..., course_audio/<slug>/... Read by app.course_object_slug_matches() for the storage read policy. Immutable once objects exist - renaming it orphans them.';

-- Backfill from the course's own recorded storage paths first: those are ground
-- truth for where its objects actually live. Title only as a last resort.
UPDATE app.course
   SET slug = lower(split_part(toc_url, '/', 2))
 WHERE slug IS NULL
   AND toc_url IS NOT NULL
   AND split_part(toc_url, '/', 2) <> '';

UPDATE app.course
   SET slug = lower(d.seg)
  FROM (
    SELECT c.id, lower(split_part(u, '/', 2)) AS seg
      FROM app.course c, unnest(coalesce(c.dict_url, array[]::text[])) AS u
     WHERE split_part(u, '/', 2) <> ''
  ) d
 WHERE app.course.id = d.id
   AND app.course.slug IS NULL;

UPDATE app.course
   SET slug = lower(title)
 WHERE slug IS NULL AND title IS NOT NULL AND title <> '';

-- Partial: a soft-deleted course may keep its slug without blocking a new
-- course that reuses the folder.
CREATE UNIQUE INDEX IF NOT EXISTS course_slug_active_key
  ON app.course (slug)
  WHERE deleted_at IS NULL;

-- Point the storage gate at the column. One fallback kept: a row with no slug
-- still matches on lower(title), so an insert that forgets the slug degrades to
-- the old behaviour rather than making its content unreachable. That branch
-- should stay dead - SupabaseService.createCourse writes the slug.
CREATE OR REPLACE FUNCTION app.course_object_slug_matches(
  p_slug text,
  p_course app.course
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT CASE
    WHEN p_course.slug IS NOT NULL AND p_course.slug <> ''
      THEN p_slug = lower(p_course.slug)
    ELSE p_slug = lower(coalesce(p_course.title, ''))
  END;
$function$;

-- Verified after applying: enrolled student 40/92 readable, tutor 91/92,
-- unenrolled stranger 4/92 — identical to the s87 derivation.
