-- ============================================================================
-- §123 (2026-10-18)  Cover requests (enrolment plan §2h; CONFLICT-058)
-- ============================================================================
--
-- cohort_matching domain (session_cover_request, session_cover_offer) and
-- tutor_management (tutor_reliability_event); tauka-python writes all three.
--
-- §2h: a tutor who can't lead a pod session or a make-up asks for cover at
-- least 24 hours ahead; it is offered to tutors in EVERY learner's pool who
-- are free then (the pod's guests first; never the requester); the first to
-- accept takes it. Decided 2026-10-18 (developer):
--   * CONFLICT-058: on cover, tauka-python sets cohort_session.lead_tutor_id /
--     lead_role on that one future session, and the host (tutor_id) of its
--     video room. A make-up's tutor_id is already tauka-python's.
--   * The scheduler is DAILY: ops is alerted at once when a request is made,
--     and again on the first daily run within 30 hours of an uncovered
--     session; a session that started uncovered is cancelled as our failure
--     (credits) on the next daily run.
--   * The LATE PATH (< 24 hours) is built: the request is marked late, ops is
--     alerted at once, and the tutor gets a late_drop reliability mark. The
--     standby roster stays in plan 8.6.
--
-- No free text: a reason code only; learners are never told why.

CREATE TABLE IF NOT EXISTS app.session_cover_request (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_type     text NOT NULL CHECK (session_type IN ('cohort_session','makeup_session')),
  session_id       uuid NOT NULL,
  session_starts_at timestamptz NOT NULL,
  requested_by     uuid NOT NULL,
  reason_code      text NOT NULL CHECK (reason_code IN ('ill','emergency','clash','other')),
  late             boolean NOT NULL,
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open','covered','withdrawn','failed')),
  covered_by       uuid,
  assigned_by      uuid,
  alerted_at       timestamptz,
  resolved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT session_cover_request_late CHECK (late = (session_starts_at - created_at < interval '24 hours')),
  CONSTRAINT session_cover_request_covered CHECK ((status = 'covered') = (covered_by IS NOT NULL)),
  CONSTRAINT session_cover_request_resolved CHECK ((status = 'open') = (resolved_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_cover_request_one_open
  ON app.session_cover_request (session_type, session_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_session_cover_request_open ON app.session_cover_request (session_starts_at) WHERE status = 'open';

CREATE OR REPLACE FUNCTION app.session_cover_request_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'open' THEN
    RAISE EXCEPTION 'session_cover_request: % is final', OLD.status USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.session_type, NEW.session_id, NEW.session_starts_at, NEW.requested_by, NEW.reason_code, NEW.late, NEW.created_at)
     IS DISTINCT FROM (OLD.session_type, OLD.session_id, OLD.session_starts_at, OLD.requested_by, OLD.reason_code, OLD.late, OLD.created_at) THEN
    RAISE EXCEPTION 'session_cover_request: the request is not edited' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER trg_session_cover_request_guard BEFORE UPDATE ON app.session_cover_request
  FOR EACH ROW EXECUTE FUNCTION app.session_cover_request_guard();

CREATE TABLE IF NOT EXISTS app.session_cover_offer (
  request_id   uuid NOT NULL REFERENCES app.session_cover_request(id) ON DELETE CASCADE,
  tutor_id     uuid NOT NULL,
  rank         int  NOT NULL DEFAULT 1,
  response     text NOT NULL DEFAULT 'pending' CHECK (response IN ('pending','accepted','declined','expired')),
  offered_at   timestamptz NOT NULL DEFAULT now(),
  responded_at timestamptz,
  PRIMARY KEY (request_id, tutor_id)
);
CREATE INDEX IF NOT EXISTS idx_session_cover_offer_tutor ON app.session_cover_offer (tutor_id) WHERE response = 'pending';

CREATE TABLE IF NOT EXISTS app.tutor_reliability_event (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tutor_id     uuid NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('cover_given','cover_taken','late_drop','no_show')),
  session_type text CHECK (session_type IS NULL OR session_type IN ('cohort_session','makeup_session')),
  session_id   uuid,
  request_id   uuid REFERENCES app.session_cover_request(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tutor_reliability_event_tutor ON app.tutor_reliability_event (tutor_id, created_at);
CREATE OR REPLACE FUNCTION app.tutor_reliability_event_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'tutor_reliability_event is append-only' USING ERRCODE = 'check_violation';
END;
$$;
CREATE TRIGGER trg_tutor_reliability_event_append_only BEFORE UPDATE OR DELETE ON app.tutor_reliability_event
  FOR EACH ROW EXECUTE FUNCTION app.tutor_reliability_event_append_only();

ALTER TABLE app.session_cover_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.session_cover_offer ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tutor_reliability_event ENABLE ROW LEVEL SECURITY;
CREATE POLICY session_cover_request_read ON app.session_cover_request FOR SELECT TO authenticated
  USING (requested_by = auth.uid() OR covered_by = auth.uid() OR app.has_admin_capability('ops')
         OR EXISTS (SELECT 1 FROM app.session_cover_offer o WHERE o.request_id = session_cover_request.id AND o.tutor_id = auth.uid()));
CREATE POLICY session_cover_offer_read ON app.session_cover_offer FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.has_admin_capability('ops'));
CREATE POLICY tutor_reliability_event_read ON app.tutor_reliability_event FOR SELECT TO authenticated
  USING (tutor_id = auth.uid() OR app.is_admin());
REVOKE ALL ON app.session_cover_request, app.session_cover_offer, app.tutor_reliability_event FROM anon, authenticated;
GRANT SELECT ON app.session_cover_request, app.session_cover_offer, app.tutor_reliability_event TO authenticated;
GRANT ALL ON app.session_cover_request, app.session_cover_offer, app.tutor_reliability_event TO service_role;
COMMENT ON TABLE app.session_cover_request IS 'A tutor asks for cover on a pod session or make-up (plan §2h): 24 h+ ahead, or late. First eligible tutor to accept takes it (CONFLICT-058). tauka-python writes.';
COMMENT ON TABLE app.session_cover_offer IS 'Each eligible tutor offered a cover request, and their answer (plan §2h). tauka-python writes.';
COMMENT ON TABLE app.tutor_reliability_event IS 'Append-only reliability record (plan §2h, 8.6): cover given / taken, late drop, no-show. tauka-python writes; the tutor and admins read.';
-- Applied to production 2026-10-18 in three parts after a dry run: s123_cover_requests,
-- s123_cover_offers_reliability, s123_cover_rls.
