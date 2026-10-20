"""Cover requests (enrolment plan §2h, migration §123; CONFLICT-058).

A tutor who can't lead a pod session or a make-up asks for cover. The session
keeps its time; only the tutor changes.

  * REQUEST: at least 24 hours ahead, with a reason code (the learners are
    never told why). Inside 24 hours it is the LATE path: still offered, ops
    alerted at once, and a `late_drop` reliability mark.
  * OFFER: to every tutor who is in EVERY learner's pool (active
    `student_tutor_link`), is active, is available then (their weekly windows)
    and has no clash. The pod's guest tutors first; never the requester. By
    email (push is suspended) and in the app's offers list.
  * ACCEPT: first to accept wins (a conditional update on the open request).
    The session's lead (`cohort_session.lead_tutor_id` / `lead_role`, or the
    make-up's `tutor_id`) and its video room's host change; the other offers
    expire; the learners are told who is teaching; `cover_given` and
    `cover_taken` are recorded. Pay follows `lead_tutor_id` already.
  * OPS: alerted at once on every request, and again on the first DAILY run
    within 30 hours of an uncovered session; can assign any tutor still in
    every learner's pool.
  * NO TUTOR AT START: the next daily run cancels the session as our failure
    (`tutor_issue`) and credits each learner (§92 for make-ups; one credit per
    member referencing the pod session).
  * WITHDRAW: until someone accepts, the tutor can keep the session.

NO-SHOW (plan 8.6, §125): a learner in the session, or ops, reports "my tutor
isn't here" from the start until 15 minutes after. That opens a request with
reason `no_show` (requested_by = the absent lead, reported_by = the reporter),
offered at once to tutors on STANDBY then (`standby_service`) first, then the
guests, then any free tutor in the pool; it can be taken until 20 minutes
after the start. The absent tutor's `no_show` mark is written only when it is
covered or fails; if they turn up, they (or ops) withdraw it. A request whose
session was in fact delivered (attendance marked) is withdrawn, not failed.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import email_service
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

NOTICE = timedelta(hours=24)
ALERT_WITHIN = timedelta(hours=30)
REASONS = ("ill", "emergency", "clash", "other")
NO_SHOW_REPORT = timedelta(minutes=15)
NO_SHOW_TAKE = timedelta(minutes=20)
TYPES = ("cohort_session", "makeup_session")


class CoverRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# ── the session ─────────────────────────────────────────────────────────────

def session(session_type: str, session_id: str) -> dict:
    """{type, id, starts_at, minutes, lead, learners, cohort_id, video_session_id, status}."""
    if session_type == "cohort_session":
        rows = (_t("cohort_session").select("id, cohort_id, scheduled_at, duration_minutes, status, lead_tutor_id, "
                                            "video_session_id").eq("id", session_id).limit(1).execute()).data or []
        if not rows:
            raise CoverRefused("no_such_session")
        s = rows[0]
        learners = [m["user_id"] for m in (_t("cohort_memberships").select("user_id").eq("cohort_id", s["cohort_id"])
                                           .eq("status", "active").execute()).data or []]
        return {"type": session_type, "id": s["id"], "starts_at": _ts(s["scheduled_at"]),
                "minutes": int(s.get("duration_minutes") or 50), "lead": s.get("lead_tutor_id"),
                "learners": learners, "cohort_id": s["cohort_id"], "video_session_id": s.get("video_session_id"),
                "status": s.get("status")}
    if session_type == "makeup_session":
        rows = (_t("makeup_session").select("id, scheduled_at, duration_minutes, status, tutor_id, video_session_id")
                .eq("id", session_id).limit(1).execute()).data or []
        if not rows:
            raise CoverRefused("no_such_session")
        s = rows[0]
        learners = [p["student_id"] for p in (_t("makeup_session_participant").select("student_id, status")
                                              .eq("makeup_session_id", session_id).execute()).data or []
                    if p.get("status") != "withdrawn"]
        return {"type": session_type, "id": s["id"], "starts_at": _ts(s.get("scheduled_at")),
                "minutes": int(s.get("duration_minutes") or 30), "lead": s.get("tutor_id"),
                "learners": learners, "cohort_id": None, "video_session_id": s.get("video_session_id"),
                "status": s.get("status")}
    raise CoverRefused("bad_type")


def _pools(learners: list[str]) -> tuple[set[str], set[str]]:
    """(tutors in EVERY learner's pool, the guests among them)."""
    if not learners:
        return set(), set()
    links = (_t("student_tutor_link").select("student_id, tutor_id, role").in_("student_id", learners)
             .eq("state", "active").execute()).data or []
    per: dict[str, set[str]] = {sid: set() for sid in learners}
    guests: set[str] = set()
    for l in links:
        per.setdefault(l["student_id"], set()).add(l["tutor_id"])
        if l.get("role") == "guest":
            guests.add(l["tutor_id"])
    common = set.intersection(*per.values()) if per else set()
    return common, guests & common


def _free(tutor_id: str, start: datetime, minutes: int, standby: set[str] = frozenset()) -> bool:
    from app.services.makeup_service import load_busy
    from app.services.schedule_service import tutor_covers
    if tutor_id not in standby and not tutor_covers(tutor_id, start.weekday(), start.time(), "UTC", start.date()):
        return False
    end = start + timedelta(minutes=minutes)
    return not any(s < end and start < e for s, e in load_busy([tutor_id], start - timedelta(hours=3)).get(tutor_id, []))


def eligible(s: dict, exclude: set[str], check_free: bool = True) -> list[str]:
    """Tutors who may take it: on standby then (8.6) first, then guests.
    `check_free=False` is ops' wider choice: anyone still in every learner's
    pool. A standby window counts as availability."""
    common, guests = _pools(s["learners"])
    common -= exclude
    if not common:
        return []
    active = {t["id"] for t in (_t("tutor").select("id, status, deleted_at").in_("id", sorted(common))
                                .execute()).data or [] if t.get("status") == "active" and not t.get("deleted_at")}
    # Plan 8.3: a tutor on an approved holiday that day isn't asked.
    if active and s.get("starts_at"):
        from app.services.tutor_ops_service import on_holiday
        active -= on_holiday(sorted(active), s["starts_at"].date())
    from app.services.standby_service import on_standby
    standby = on_standby(sorted(active), s["starts_at"]) if active and s.get("starts_at") else set()
    out = [t for t in sorted(common)
           if t in active and (not check_free or _free(t, s["starts_at"], s["minutes"], standby))]
    return sorted(out, key=lambda t: (t not in standby, t not in guests, t))


def _takeable(req: dict, now: datetime) -> bool:
    """Before the start, or a no-show inside its take window."""
    starts = _ts(req["session_starts_at"])
    return starts > now or (req.get("reason_code") == "no_show" and now <= starts + NO_SHOW_TAKE)


# ── the tutor's side ────────────────────────────────────────────────────────

async def request(tutor_id: str, session_type: str, session_id: str, reason: str,
                  now: datetime | None = None) -> dict:
    now = now or _now()
    if session_type not in TYPES or reason not in REASONS:
        raise CoverRefused("bad_request")
    s = session(session_type, session_id)
    if s["lead"] != tutor_id:
        raise CoverRefused("not_your_session")
    if s["status"] != "scheduled" or not s["starts_at"] or s["starts_at"] <= now:
        raise CoverRefused("not_upcoming")
    late = s["starts_at"] - now < NOTICE
    try:
        req = (_t("session_cover_request").insert({
            "session_type": session_type, "session_id": session_id, "session_starts_at": s["starts_at"].isoformat(),
            "requested_by": tutor_id, "reason_code": reason, "late": late, "created_at": now.isoformat(),
        }).execute()).data[0]
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise CoverRefused("already_requested") from exc
        raise
    tutors = eligible(s, {tutor_id})
    for rank, t in enumerate(tutors, start=1):
        _t("session_cover_offer").insert({"request_id": req["id"], "tutor_id": t, "rank": rank}).execute()
    if late:
        _reliability(tutor_id, "late_drop", s, req["id"])
    await _mail_offers(tutors, s)
    await _alert_ops(req, s, len(tutors), "new")
    _t("session_cover_request").update({"alerted_at": now.isoformat()}).eq("id", req["id"]).eq("status", "open").execute()
    return {**req, "offered": len(tutors)}


def withdraw(tutor_id: str, request_id: str) -> dict:
    """The requester keeps the session (for a no-show: "I'm here"); ops may too."""
    q = _t("session_cover_request").update({"status": "withdrawn", "resolved_at": _now().isoformat()}) \
        .eq("id", request_id).eq("status", "open")
    if not hs.admin_can(tutor_id, "ops"):
        q = q.eq("requested_by", tutor_id)
    got = q.execute().data or []
    if not got:
        raise CoverRefused("not_open")
    _expire_offers(request_id)
    return got[0]


def mine(tutor_id: str, now: datetime | None = None, days: int = 14) -> list[dict]:
    """The tutor's own upcoming sessions (pod sessions they lead, scheduled
    make-ups) in the next `days`, each with its open or covered request."""
    now = now or _now()
    until = (now + timedelta(days=days)).isoformat()
    since = (now - NO_SHOW_TAKE).isoformat()     # a no-show against them shows until it can't be taken
    rows = []
    for s in (_t("cohort_session").select("id, scheduled_at, duration_minutes, status").eq("lead_tutor_id", tutor_id)
              .eq("status", "scheduled").execute()).data or []:
        if since < str(s["scheduled_at"]) <= until:
            rows.append(("cohort_session", s["id"], s["scheduled_at"], int(s.get("duration_minutes") or 50)))
    for s in (_t("makeup_session").select("id, scheduled_at, duration_minutes, status").eq("tutor_id", tutor_id)
              .eq("status", "scheduled").execute()).data or []:
        if s.get("scheduled_at") and since < str(s["scheduled_at"]) <= until:
            rows.append(("makeup_session", s["id"], s["scheduled_at"], int(s.get("duration_minutes") or 30)))
    reqs = {}
    if rows:
        for r in (_t("session_cover_request").select("id, session_id, status, late, requested_by, reason_code")
                  .in_("session_id", [r[1] for r in rows]).in_("status", ["open"]).execute()).data or []:
            reqs[r["session_id"]] = r
    out = []
    for t, i, at, m in rows:
        r = reqs.get(i)
        if _ts(at) <= now and not (r and r.get("reason_code") == "no_show"):
            continue      # started: shown only while a no-show is open against it
        out.append({"session_type": t, "session_id": i, "starts_at": at, "minutes": m,
                    "request": ({"id": r["id"], "status": r["status"], "late": r["late"],
                                 "no_show": r.get("reason_code") == "no_show"} if r else None),
                    "late_if_now": _ts(at) - now < NOTICE})
    return sorted(out, key=lambda x: x["starts_at"])


def offers_for(tutor_id: str) -> list[dict]:
    """Pending offers on open requests, soonest first — what this tutor can take."""
    from app.services.makeup_service import _names
    offers = (_t("session_cover_offer").select("request_id, rank").eq("tutor_id", tutor_id)
              .eq("response", "pending").execute()).data or []
    if not offers:
        return []
    reqs = {r["id"]: r for r in (_t("session_cover_request").select("*").in_("id", [o["request_id"] for o in offers])
                                 .eq("status", "open").execute()).data or []}
    out = []
    for o in offers:
        r = reqs.get(o["request_id"])
        if not r or not _takeable(r, _now()):
            continue
        s = session(r["session_type"], r["session_id"])
        out.append({"request_id": r["id"], "session_type": r["session_type"], "starts_at": r["session_starts_at"],
                    "minutes": s["minutes"], "learners": len(s["learners"]), "late": r["late"],
                    "no_show": r.get("reason_code") == "no_show",
                    "requested_by": _names([r["requested_by"]]).get(r["requested_by"])})
    return sorted(out, key=lambda x: x["starts_at"])


async def accept(tutor_id: str, request_id: str) -> dict:
    offer = (_t("session_cover_offer").select("response").eq("request_id", request_id).eq("tutor_id", tutor_id)
             .limit(1).execute()).data or []
    if not offer or offer[0]["response"] != "pending":
        raise CoverRefused("no_offer")
    return await _cover(request_id, tutor_id, actor=None)


def decline(tutor_id: str, request_id: str) -> None:
    _t("session_cover_offer").update({"response": "declined", "responded_at": _now().isoformat()}) \
        .eq("request_id", request_id).eq("tutor_id", tutor_id).eq("response", "pending").execute()


# ── covering (accept or assign) ─────────────────────────────────────────────

async def _cover(request_id: str, tutor_id: str, actor: str | None) -> dict:
    now = _now()
    rows = (_t("session_cover_request").select("*").eq("id", request_id).limit(1).execute()).data or []
    if not rows:
        raise CoverRefused("no_such_request")
    req = rows[0]
    if req["status"] != "open":
        raise CoverRefused("not_open")
    if not _takeable(req, now):
        raise CoverRefused("started")
    # First wins: the conditional update is the lock.
    got = (_t("session_cover_request").update({"status": "covered", "covered_by": tutor_id, "assigned_by": actor,
                                               "resolved_at": now.isoformat()})
           .eq("id", request_id).eq("status", "open").execute()).data or []
    if not got:
        raise CoverRefused("not_open")
    s = session(req["session_type"], req["session_id"])
    if s["type"] == "cohort_session":
        pod = (_t("cohorts").select("tutor_id").eq("id", s["cohort_id"]).limit(1).execute()).data or [{}]
        role = "home" if pod[0].get("tutor_id") == tutor_id else "guest"
        # CONFLICT-058: the lead of this one future session.
        _t("cohort_session").update({"lead_tutor_id": tutor_id, "lead_role": role}) \
            .eq("id", s["id"]).in_("status", ["scheduled", "in_progress"]).execute()
    else:
        _t("makeup_session").update({"tutor_id": tutor_id}).eq("id", s["id"]) \
            .in_("status", ["scheduled", "in_progress"]).execute()
    if s.get("video_session_id"):
        # CONFLICT-058: the room's host follows the lead.
        _t("video_session").update({"tutor_id": tutor_id}).eq("id", s["video_session_id"]).execute()
    _t("session_cover_offer").update({"response": "accepted", "responded_at": now.isoformat()}) \
        .eq("request_id", request_id).eq("tutor_id", tutor_id).execute()
    _expire_offers(request_id)
    _reliability(req["requested_by"], "no_show" if req.get("reason_code") == "no_show" else "cover_given",
                 s, request_id)
    _reliability(tutor_id, "cover_taken", s, request_id)
    await _mail_covered(s, tutor_id, req["requested_by"])
    return got[0]


def _expire_offers(request_id: str) -> None:
    _t("session_cover_offer").update({"response": "expired", "responded_at": _now().isoformat()}) \
        .eq("request_id", request_id).eq("response", "pending").execute()


def _reliability(tutor_id: str, kind: str, s: dict, request_id: str | None) -> None:
    try:
        _t("tutor_reliability_event").insert({"tutor_id": tutor_id, "kind": kind, "session_type": s["type"],
                                              "session_id": s["id"], "request_id": request_id}).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("reliability %s for %s failed: %s", kind, tutor_id, exc)


# ── no-shows (plan 8.6, §125) ───────────────────────────────────────────────

async def report_no_show(reporter: str, session_type: str, session_id: str, now: datetime | None = None) -> dict:
    """A learner in the session (or ops): the tutor hasn't come."""
    now = now or _now()
    if session_type not in TYPES:
        raise CoverRefused("bad_request")
    s = session(session_type, session_id)
    if reporter not in s["learners"] and not hs.admin_can(reporter, "ops"):
        raise CoverRefused("not_your_session")
    if s["status"] not in ("scheduled", "in_progress") or not s["starts_at"] \
            or not s["starts_at"] <= now <= s["starts_at"] + NO_SHOW_REPORT:
        raise CoverRefused("not_now")
    if not s["lead"]:
        raise CoverRefused("no_lead")
    open_ = (_t("session_cover_request").select("*").eq("session_type", session_type).eq("session_id", session_id)
             .eq("status", "open").limit(1).execute()).data or []
    if open_:
        return {**open_[0], "already": True}
    try:
        req = (_t("session_cover_request").insert({
            "session_type": session_type, "session_id": session_id, "session_starts_at": s["starts_at"].isoformat(),
            "requested_by": s["lead"], "reason_code": "no_show", "late": True, "reported_by": reporter,
            "created_at": now.isoformat(),
        }).execute()).data[0]
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return {"already": True}
        raise
    tutors = eligible(s, {s["lead"]})
    for rank, t in enumerate(tutors, start=1):
        _t("session_cover_offer").insert({"request_id": req["id"], "tutor_id": t, "rank": rank}).execute()
    await _mail_offers(tutors, s, urgent=True)
    await _mail_absent_lead(s)
    await _alert_ops(req, s, len(tutors), "no_show")
    _t("session_cover_request").update({"alerted_at": now.isoformat()}).eq("id", req["id"]).eq("status", "open").execute()
    return {**req, "offered": len(tutors)}


def _delivered(s: dict) -> bool:
    """Did the session happen after all (the tutor came)? Attendance says so."""
    if s["type"] == "cohort_session":
        # app.cohort_session_attendance (tauka-flutter): marked by the tutor.
        rows = (_t("cohort_session_attendance").select("status").eq("cohort_session_id", s["id"])
                .eq("status", "attended").limit(1).execute()).data or []
    else:
        rows = (_t("makeup_session_participant").select("status").eq("makeup_session_id", s["id"])
                .eq("status", "attended").limit(1).execute()).data or []
    return bool(rows) or s.get("status") == "completed"


# ── ops ──────────────────────────────────────────────────────────────────────

def _require_ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise CoverRefused("not_allowed")


def open_requests(actor: str) -> list[dict]:
    _require_ops(actor)
    from app.services.makeup_service import _names
    reqs = (_t("session_cover_request").select("*").eq("status", "open").execute()).data or []
    out = []
    for r in sorted(reqs, key=lambda r: r["session_starts_at"]):
        s = session(r["session_type"], r["session_id"])
        offers = (_t("session_cover_offer").select("tutor_id, response").eq("request_id", r["id"]).execute()).data or []
        candidates = eligible(s, {r["requested_by"]}, check_free=False)
        names = _names([r["requested_by"]] + [o["tutor_id"] for o in offers] + candidates)
        out.append({**r, "learners": len(s["learners"]),
                    "requested_by_name": names.get(r["requested_by"]),
                    "offers": [{"tutor_id": o["tutor_id"], "name": names.get(o["tutor_id"]), "response": o["response"]}
                               for o in offers],
                    "can_assign": [{"tutor_id": t, "name": names.get(t)} for t in candidates]})
    return out


async def assign(actor: str, request_id: str, tutor_id: str) -> dict:
    """Ops: any tutor still in every learner's pool (the pool rule; availability
    is ops' call)."""
    _require_ops(actor)
    rows = (_t("session_cover_request").select("*").eq("id", request_id).limit(1).execute()).data or []
    if not rows:
        raise CoverRefused("no_such_request")
    s = session(rows[0]["session_type"], rows[0]["session_id"])
    if tutor_id not in eligible(s, {rows[0]["requested_by"]}, check_free=False):
        raise CoverRefused("not_in_pool")
    return await _cover(request_id, tutor_id, actor)


# ── the daily run ───────────────────────────────────────────────────────────

async def daily(now: datetime | None = None) -> dict:
    """Alert ops for uncovered sessions within 30 hours (once more, close to
    the start); fail requests whose session started uncovered: cancel it as
    our failure and credit the learners."""
    now = now or _now()
    out = {"alerted": 0, "failed": 0}
    for r in (_t("session_cover_request").select("*").eq("status", "open").execute()).data or []:
        starts = _ts(r["session_starts_at"])
        try:
            if starts <= now and _takeable(r, now):
                continue      # a no-show still inside its take window
            if starts <= now:
                await _fail(r, now)
                out["failed"] += 1
            elif starts - now <= ALERT_WITHIN and (not r.get("alerted_at")
                                                    or starts - _ts(r["alerted_at"]) > ALERT_WITHIN):
                s = session(r["session_type"], r["session_id"])
                await _alert_ops(r, s, None, "soon")
                _t("session_cover_request").update({"alerted_at": now.isoformat()}).eq("id", r["id"]) \
                    .eq("status", "open").execute()
                out["alerted"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("cover request %s: %s", r.get("id"), exc)
    return out


async def _fail(r: dict, now: datetime) -> None:
    s = session(r["session_type"], r["session_id"])
    if _delivered(s):
        # The tutor came after all and nobody withdrew the request: not a failure.
        _t("session_cover_request").update({"status": "withdrawn", "resolved_at": now.isoformat()}) \
            .eq("id", r["id"]).eq("status", "open").execute()
        _expire_offers(r["id"])
        return
    got = (_t("session_cover_request").update({"status": "failed", "resolved_at": now.isoformat()})
           .eq("id", r["id"]).eq("status", "open").execute()).data or []
    if not got:
        return
    _expire_offers(r["id"])
    if r.get("reason_code") == "no_show":
        _reliability(r["requested_by"], "no_show", s, r["id"])
    if s["type"] == "cohort_session":
        _t("cohort_session").update({"status": "cancelled", "cancelled_reason": "tutor_issue: no cover (plan §2h)"}) \
            .eq("id", s["id"]).in_("status", ["scheduled", "in_progress"]).execute()
        for sid in s["learners"]:
            try:
                _t("session_credit").insert({"student_id": sid, "direction": "grant", "reason": "tutor_issue",
                                             "reference_type": "cohort_session", "reference_id": s["id"]}).execute()
            except Exception as exc:  # noqa: BLE001
                if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                    logger.error("credit for %s failed: %s", sid, exc)
    else:
        # §92: cancelling a make-up for tutor_issue credits its month-funded seats.
        _t("makeup_session").update({"status": "cancelled", "cancelled_reason": "tutor_issue"}) \
            .eq("id", s["id"]).in_("status", ["scheduled", "in_progress"]).execute()
    await _mail_cancelled(s)


# ── email ────────────────────────────────────────────────────────────────────

async def _send(to: str | None, data: dict, tags: list[str]) -> None:
    if not to:
        return
    try:
        await email_service.send_template_email(to, "cover", {**data, "web_base_url": settings.web_base_url}, tags=tags)
    except Exception as exc:  # noqa: BLE001
        logger.error("cover email failed: %s", exc)


async def _mail_offers(tutors: list[str], s: dict, urgent: bool = False) -> None:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    zones = _zones(tutors) if tutors else {}
    if urgent:
        # Plan 6.1/6.2: a no-show can't wait for email — push / WhatsApp when on.
        from app.services.notify_service import notify
        for t in tutors:
            when = local_time(s["starts_at"], zones.get(t, "UTC"))
            await notify(t, "cover_urgent", s["id"], "Learners are waiting for a tutor now", when, "/tutor-dashboard",
                         whatsapp=("cover_urgent", [when]))
    for t in tutors:
        await _send(_email_of(t), {"subject": "Urgent: learners are waiting for a tutor now" if urgent
                                   else "Can you cover a session?", "kind": "urgent" if urgent else "offer",
                                   "when": local_time(s["starts_at"], zones.get(t, "UTC")), "minutes": s["minutes"],
                                   "learners": len(s["learners"])}, ["cover", "offer"])


async def _mail_covered(s: dict, tutor_id: str, requester: str) -> None:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    from app.services.makeup_service import _names
    first = (_names([tutor_id]).get(tutor_id) or "a tutor").split(" ")[0]
    people = s["learners"] + [requester, tutor_id]
    zones = _zones(people)
    for uid in s["learners"]:
        await _send(_email_of(uid), {"subject": f"Your session is with {first}", "kind": "covered_learner",
                                     "when": local_time(s["starts_at"], zones.get(uid, "UTC")), "tutor": first},
                    ["cover", "covered"])
    for uid, kind in ((requester, "covered_requester"), (tutor_id, "covered_taker")):
        await _send(_email_of(uid), {"subject": "Cover confirmed", "kind": kind, "tutor": first,
                                     "when": local_time(s["starts_at"], zones.get(uid, "UTC"))}, ["cover", "covered"])


async def _mail_absent_lead(s: dict) -> None:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    zones = _zones([s["lead"]])
    await _send(_email_of(s["lead"]), {"subject": "Your learners are waiting", "kind": "absent_lead",
                                       "when": local_time(s["starts_at"], zones.get(s["lead"], "UTC"))},
                ["cover", "no_show"])


async def _mail_cancelled(s: dict) -> None:
    from app.services.makeup_delivery import _email_of, _zones, local_time
    zones = _zones(s["learners"]) if s["learners"] else {}
    for uid in s["learners"]:
        await _send(_email_of(uid), {"subject": "Your session couldn't go ahead", "kind": "cancelled",
                                     "when": local_time(s["starts_at"], zones.get(uid, "UTC"))}, ["cover", "cancelled"])


async def _alert_ops(r: dict, s: dict, offered: int | None, why: str) -> None:
    # Plan 6.4: the ops Telegram group too (times and counts only, no names).
    try:
        from app.services.telegram_service import post
        label = {"new": "Cover requested", "no_show": "NO-SHOW reported", "soon": "Still uncovered"}.get(why, why)
        await post(f"{'LATE ' if r.get('late') and why == 'new' else ''}{label}: {s['type'].replace('_', ' ')} at "
                   f"{s['starts_at'].strftime('%a %d %b %H:%M UTC')}"
                   + ("" if offered is None else f" · offered to {offered}"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram cover alert failed: %s", exc)
    if not settings.admin_email:
        return
    try:
        await email_service.send_email(
            settings.admin_email,
            ("NO-SHOW " if why == "no_show" else "LATE " if r.get("late") else "") +
            ("Cover requested" if why == "new" else "Tutor not here" if why == "no_show" else "Still uncovered") +
            f": {s['type'].replace('_', ' ')} at {s['starts_at'].strftime('%a %d %b %H:%M UTC')}",
            f"<p>{'A tutor asked for cover' + (' with less than 24 hours notice' if r.get('late') else '') if why == 'new' else 'A learner reports the tutor has not come; standby tutors have been asked.' if why == 'no_show' else 'This session still has no cover.'}"
            f"{'' if offered is None else f' Offered to {offered} eligible tutor(s).'}</p>"
            f"<p><a href=\"{settings.web_base_url}/admin/cover\">Open cover requests</a></p>",
            tags=["cover", why])
    except Exception as exc:  # noqa: BLE001
        logger.error("cover alert failed: %s", exc)
