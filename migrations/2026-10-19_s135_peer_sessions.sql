-- ============================================================================
-- §135 (2026-10-19)  Peer sessions (enrolment plan Track 5: 5.1-5.8)
-- ============================================================================
--
-- New tables in the cohort_matching domain; tauka-python writes all of them.
-- Behind the 'peer_sessions' flag (§131): until it is on, nothing pairs and
-- every endpoint refuses. "Not before Tir" (plan).
--
-- 5.1 peer_task: task cards per lesson (information gap: A and B each see half;
--     role-play; question set), authored by rung >= 3 tutors or ops.
-- 5.2 weekly pairing across pods in one season, same level and band, at a
--     catalogue slot both learners chose, not clashing with their pod; gender
--     requirements honoured when that flag is on. Never two from the same pod,
--     never a blocked pair, not the same pair two weeks running.
-- 5.3 20 minutes, audio-only (Daily room with video off), room made on first join.
-- 5.4 report (to safeguarding, as a concern report) and block in every session;
--     contact swapping is already caught at send by the message scanner (D-12).
-- 5.5 each side says whether the partner came; marked absent twice in 30 days
--     pauses pairing for 14 days (E-16: peer attendance is an allowed input).
-- 5.6 nothing is recorded (L-004).
-- 5.7 pilot: "practise now" pairs two waiting learners at once, one band only.
-- 5.8 peer attendance is readable for the milestone guarantee (Track 10).

CREATE TABLE IF NOT EXISTS app.peer_task (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  course_id      uuid,
  lesson_number  int  NOT NULL CHECK (lesson_number BETWEEN 0 AND 200),
  kind           text NOT NULL CHECK (kind IN ('information_gap','role_play','question_set')),
  title          text NOT NULL CHECK (length(title) BETWEEN 3 AND 120),
  prompt_a       text NOT NULL CHECK (length(prompt_a) BETWEEN 3 AND 2000),
  prompt_b       text CHECK (prompt_b IS NULL OR length(prompt_b) <= 2000),
  active         boolean NOT NULL DEFAULT true,
  created_by     uuid,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT peer_task_gap_has_b CHECK (kind <> 'information_gap' OR prompt_b IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS app.peer_optin (
  student_id   uuid PRIMARY KEY REFERENCES app.student(id) ON DELETE CASCADE,
  opted_in     boolean NOT NULL DEFAULT true,
  paused_until timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.peer_session (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL DEFAULT 'scheduled' CHECK (kind IN ('scheduled','instant')),
  starts_at   timestamptz NOT NULL,
  minutes     int  NOT NULL DEFAULT 20 CHECK (minutes BETWEEN 15 AND 20),
  level       int,
  band        text,
  season_id   uuid,
  task_id     uuid REFERENCES app.peer_task(id) ON DELETE SET NULL,
  room_url    text,
  status      text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','completed','cancelled')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_peer_session_starts ON app.peer_session (starts_at);

CREATE TABLE IF NOT EXISTS app.peer_session_participant (
  session_id     uuid NOT NULL REFERENCES app.peer_session(id) ON DELETE CASCADE,
  student_id     uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  role           text NOT NULL DEFAULT 'a' CHECK (role IN ('a','b','c')),
  -- What THIS learner said about the others: did they come?
  partner_came   boolean,
  marked_at      timestamptz,
  PRIMARY KEY (session_id, student_id)
);
CREATE INDEX IF NOT EXISTS idx_peer_participant_student ON app.peer_session_participant (student_id);

CREATE TABLE IF NOT EXISTS app.peer_block (
  blocker_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  blocked_id uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (blocker_id, blocked_id),
  CONSTRAINT peer_block_not_self CHECK (blocker_id <> blocked_id)
);

CREATE TABLE IF NOT EXISTS app.peer_queue (
  student_id  uuid PRIMARY KEY REFERENCES app.student(id) ON DELETE CASCADE,
  level       int,
  band        text,
  joined_at   timestamptz NOT NULL DEFAULT now(),
  session_id  uuid REFERENCES app.peer_session(id) ON DELETE SET NULL
);

-- 5.4: a concern report can point at a peer session.
ALTER TABLE app.concern_report DROP CONSTRAINT IF EXISTS concern_report_session_type_check;
ALTER TABLE app.concern_report ADD CONSTRAINT concern_report_session_type_check CHECK (session_type IS NULL OR
  session_type IN ('cohort_session','makeup_session','clinic','one_to_one_session','peer_session'));

ALTER TABLE app.peer_task ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.peer_optin ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.peer_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.peer_session_participant ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.peer_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.peer_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY peer_optin_read ON app.peer_optin FOR SELECT TO authenticated USING (student_id = auth.uid() OR app.is_admin());
CREATE POLICY peer_block_read ON app.peer_block FOR SELECT TO authenticated USING (blocker_id = auth.uid() OR app.is_admin());
CREATE POLICY peer_participant_read ON app.peer_session_participant FOR SELECT TO authenticated
  USING (student_id = auth.uid() OR app.is_admin());
-- Tasks and sessions are served through tauka-python (each side sees only its half).
REVOKE ALL ON app.peer_task, app.peer_optin, app.peer_session, app.peer_session_participant, app.peer_block,
              app.peer_queue FROM anon, authenticated;
GRANT SELECT ON app.peer_optin, app.peer_block, app.peer_session_participant TO authenticated;
GRANT ALL ON app.peer_task, app.peer_optin, app.peer_session, app.peer_session_participant, app.peer_block,
              app.peer_queue TO service_role;
COMMENT ON TABLE app.peer_session IS 'Peer sessions between pods (Track 5): 20 min, audio-only, never recorded. tauka-python writes.';
COMMENT ON TABLE app.peer_task IS 'Task cards per lesson for peer sessions (plan 5.1). tauka-python writes (rung >= 3 or ops author).';
