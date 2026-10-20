-- ============================================================================
-- §140 (2026-10-19)  Track 10 pilots and the plan's last remainders
-- ============================================================================
--
-- tauka-python writes everything here.
--
-- 1.4 remainder (enrolment): fidel_approval gains a SECOND review for a 20%
--     sample of rung-1 reviews (calibration). The second verdict never changes
--     the learner's; agreement informs the rung-1 reviewer's promotion (E-10).
-- Holiday mini-lessons in Explore (enrolment): short lessons tied to Ethiopian
--     holidays (Enkutatash, Meskel, Genna, Timket, Fasika), shown in the two
--     weeks before each and three days after; dates computed, not stored.
-- City meetups and season gatherings (enrolment): ops schedules; learners RSVP
--     (capacity); the host tutor is paid a gathering work item when it's done.
-- Twice-monthly formation per band (enrolment): intake.band — NULL is every
--     band; a band-only mid-month intake (Ethiopic day 15) forms pods from that
--     band only, opened by ops once a band has the volume.
-- Two-tutor clinics (pilot): clinic.co_tutor_id; both are paid for it.
-- Heritage-beginner mentoring (pilot): peer_optin.mentor; a monthly mentoring
--     peer session pairs a heritage learner (level 3+) with a beginner.

ALTER TABLE app.fidel_approval ADD COLUMN IF NOT EXISTS second_reviewer_id uuid;
ALTER TABLE app.fidel_approval ADD COLUMN IF NOT EXISTS second_status text
  CHECK (second_status IS NULL OR second_status IN ('pending','agree','disagree'));
ALTER TABLE app.fidel_approval ADD COLUMN IF NOT EXISTS second_reviewed_at timestamptz;

CREATE TABLE IF NOT EXISTS app.holiday_lesson (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  holiday     text NOT NULL CHECK (holiday IN ('enkutatash','meskel','genna','timket','fasika')),
  title       text NOT NULL CHECK (length(title) BETWEEN 3 AND 120),
  body        text NOT NULL CHECK (length(body) BETWEEN 10 AND 4000),
  active      boolean NOT NULL DEFAULT true,
  created_by  uuid,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.gathering (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL CHECK (kind IN ('meetup','gathering')),
  season_id     uuid,
  title         text NOT NULL CHECK (length(title) BETWEEN 3 AND 120),
  city          text NOT NULL CHECK (length(city) BETWEEN 2 AND 80),
  starts_at     timestamptz NOT NULL,
  capacity      int  NOT NULL CHECK (capacity BETWEEN 2 AND 500),
  host_tutor_id uuid,
  status        text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','done','cancelled')),
  created_by    uuid,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.gathering_rsvp (
  gathering_id uuid NOT NULL REFERENCES app.gathering(id) ON DELETE CASCADE,
  student_id   uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  status       text NOT NULL DEFAULT 'going' CHECK (status IN ('going','cancelled')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (gathering_id, student_id)
);

ALTER TABLE app.intake ADD COLUMN IF NOT EXISTS band text
  CHECK (band IS NULL OR band IN ('americas','europe','ea_gulf','asia'));
COMMENT ON COLUMN app.intake.band IS 'NULL = every band. A band-only intake (twice-monthly formation, Track 10) forms pods from that band only.';

ALTER TABLE app.clinic ADD COLUMN IF NOT EXISTS co_tutor_id uuid;
COMMENT ON COLUMN app.clinic.co_tutor_id IS 'Two-tutor clinic pilot (Track 10): the second tutor, paid for the clinic too.';

ALTER TABLE app.peer_optin ADD COLUMN IF NOT EXISTS mentor boolean NOT NULL DEFAULT false;
ALTER TABLE app.peer_session DROP CONSTRAINT IF EXISTS peer_session_kind_check;
ALTER TABLE app.peer_session ADD CONSTRAINT peer_session_kind_check CHECK (kind IN ('scheduled','instant','mentoring'));

ALTER TABLE app.holiday_lesson ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.gathering ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.gathering_rsvp ENABLE ROW LEVEL SECURITY;
CREATE POLICY holiday_lesson_read ON app.holiday_lesson FOR SELECT TO authenticated USING (active OR app.is_admin());
CREATE POLICY gathering_read ON app.gathering FOR SELECT TO authenticated USING (true);
CREATE POLICY gathering_rsvp_read ON app.gathering_rsvp FOR SELECT TO authenticated USING (student_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.holiday_lesson, app.gathering, app.gathering_rsvp FROM anon, authenticated;
GRANT SELECT ON app.holiday_lesson, app.gathering, app.gathering_rsvp TO authenticated;
GRANT ALL ON app.holiday_lesson, app.gathering, app.gathering_rsvp TO service_role;
COMMENT ON TABLE app.holiday_lesson IS 'Holiday mini-lessons for Explore (Track 10). tauka-python writes.';
COMMENT ON TABLE app.gathering IS 'City meetups and season gatherings (Track 10). tauka-python writes.';
