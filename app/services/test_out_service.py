"""Heritage test-out of Phase 0 (enrolment plan 1.6 / 1.7, §2k, migration §99).

A learner who already reads Fidel can skip Phase 0 by passing a short live check
with a tutor. Decided 2026-10-18 (developer):

- **Who may send a test-out request:** the learner themselves, a rung-4 (senior /
  anchor) tutor, an `owner` or an `ops` admin.
- **Who assesses:** one of the learner's OWN tutors (active `student_tutor_link`)
  at rung 2 or above, or an `ops` admin.
- **Assigning an assessor who isn't linked yet** creates a guest link, and only
  `ops` / `owner` may do that (it widens who can read the learner's data). A
  rung-4 tutor may assign only a tutor the learner already has.
- **The guest link made for a test-out is ended when the request closes**, so the
  access lasts as long as the job.

No recording is stored: the check is live (L-004 — recording is still blocked).
The result is codes only, never free text.

A pass sets `enrolment_state.phase0_completed_at` with `phase0_completed_via =
'test_out'`; `phase0_service`'s sync then confirms the seat if the learner has
paid, exactly as finishing the alphabet would.
"""

import logging
from datetime import datetime, timezone

from app.config import settings
from app.services import email_service
from app.services import hierarchy_service as hs
from app.services import matching_service, phase0_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

GAP_CODES = ("fidel_recognition", "vowel_orders", "ejectives", "gemination", "reading_fluency")
RESULTS = ("passed", "not_yet")
OPEN = ("requested", "assigned")
ASSESSOR_MIN_RUNG = 2
SENIOR_RUNG = 4


class TestOutRefused(Exception):
    __test__ = False  # not a pytest class, despite the name

    def __init__(self, reason: str, detail: list | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or []


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def requester_role(actor: str, student_id: str) -> str | None:
    """The capacity in which `actor` may send a request for `student_id`."""
    if actor == student_id:
        return "learner"
    if hs.admin_can(actor, "owner"):
        return "owner"
    if hs.admin_can(actor, "ops"):
        return "ops"
    if hs.rung_of(actor) == SENIOR_RUNG:
        return "senior_tutor"
    return None


def _get(request_id: str) -> dict:
    rows = (_t("test_out_request").select("*").eq("id", request_id).limit(1).execute()).data or []
    if not rows:
        raise TestOutRefused("no_such_request")
    return rows[0]


def _active_tutor(tutor_id: str) -> bool:
    rows = (
        _t("tutor").select("id, status, deleted_at").eq("id", tutor_id).limit(1).execute()
    ).data or []
    return bool(rows) and rows[0].get("status") == "active" and not rows[0].get("deleted_at")


def _end_link(link_id: str | None) -> None:
    if not link_id:
        return
    now = _now()
    try:
        _t("student_tutor_link").update({"state": "ended", "ended_at": now, "updated_at": now}) \
            .eq("id", link_id).eq("state", "active").execute()
    except Exception as exc:  # noqa: BLE001
        # Best effort: a link left active is visible on the roster and an admin
        # can end it. Failing the result over it would lose the verdict.
        logger.error("could not end test-out guest link %s: %s", link_id, exc)


def _ensure_phase0(student_id: str, course_id: str) -> None:
    """A test-out is a way through Phase 0, so the learner must be in it (or
    about to start it) and not already past it."""
    rows = (
        _t("enrolment_state").select("state, phase0_completed_at")
        .eq("student_id", student_id).eq("course_id", course_id).limit(1).execute()
    ).data or []
    if not rows:
        phase0_service.start(student_id, course_id, pace_pledge_hours=None, band=None)
        return
    if rows[0].get("phase0_completed_at") or rows[0]["state"] not in ("waitlisted", "phase0", "paid"):
        raise TestOutRefused("phase0_already_done")


def _course_of(student_id: str) -> str:
    """The learner's course, when a staff member didn't name one. One course is
    the normal case; more than one is ambiguous, and we say so."""
    # app.student_course (tauka-flutter): one row per enrolment, course_id.
    rows = (_t("student_course").select("course_id").eq("student_id", student_id).execute()).data or []
    ids = sorted({r["course_id"] for r in rows})
    if len(ids) != 1:
        raise TestOutRefused("course_required")
    return ids[0]


async def create(actor: str, student_id: str, course_id: str | None,
                 assessor_tutor_id: str | None = None) -> dict:
    role = requester_role(actor, student_id)
    if role is None:
        raise TestOutRefused("not_allowed")
    if not (_t("student").select("id").eq("id", student_id).limit(1).execute()).data:
        raise TestOutRefused("no_such_student")
    course_id = course_id or _course_of(student_id)
    _ensure_phase0(student_id, course_id)
    try:
        created = _t("test_out_request").insert({
            "student_id": student_id, "course_id": course_id,
            "requested_by": actor, "requested_as": role,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise TestOutRefused("already_open") from exc
        raise
    row = (created.data or [None])[0]
    if assessor_tutor_id and row:
        row = await assign(actor, row["id"], assessor_tutor_id)
    return row


async def assign(actor: str, request_id: str, tutor_id: str) -> dict:
    req = _get(request_id)
    if req["status"] not in OPEN:
        raise TestOutRefused("closed")
    may_link = hs.admin_can(actor, "ops")          # owner implies ops
    is_senior = hs.rung_of(actor) == SENIOR_RUNG
    if not (may_link or is_senior):
        raise TestOutRefused("not_allowed")
    if (hs.rung_of(tutor_id) or 0) < ASSESSOR_MIN_RUNG:
        raise TestOutRefused("assessor_rung")
    if not _active_tutor(tutor_id):
        raise TestOutRefused("assessor_inactive")

    new_link_id = None
    if tutor_id not in hs.linked_tutor_ids(req["student_id"]):
        if not may_link:
            # A guest link widens who can read the learner's data: ops decides.
            raise TestOutRefused("ops_required_for_new_link")
        made = await matching_service.create_link(req["student_id"], tutor_id, "guest", "admin")
        if not made.get("created"):
            raise TestOutRefused("link_refused", made.get("blocking"))
        new_link_id = (made.get("link") or {}).get("id")

    # Re-assigning: the previous assessor's test-out link goes.
    if req.get("assessor_link_id") and req.get("assessor_tutor_id") != tutor_id:
        _end_link(req["assessor_link_id"])
        keep_link = new_link_id
    else:
        keep_link = new_link_id or req.get("assessor_link_id")

    result = _t("test_out_request").update({
        "status": "assigned", "assessor_tutor_id": tutor_id, "assessor_link_id": keep_link,
    }).eq("id", request_id).execute()
    return (result.data or [req])[0]


def record_result(actor: str, request_id: str, result: str, gap_codes: list[str]) -> dict:
    if result not in RESULTS:
        raise TestOutRefused("invalid_result")
    codes = sorted(set(gap_codes or []))
    if any(c not in GAP_CODES for c in codes):
        raise TestOutRefused("invalid_gap_code")
    req = _get(request_id)
    if req["status"] != "assigned":
        raise TestOutRefused("not_assigned")

    is_assessor = (
        actor == req["assessor_tutor_id"]
        and actor in hs.linked_tutor_ids(req["student_id"])
        and (hs.rung_of(actor) or 0) >= ASSESSOR_MIN_RUNG
    )
    if not (is_assessor or hs.admin_can(actor, "ops")):
        raise TestOutRefused("not_allowed")

    updated = _t("test_out_request").update({
        "status": result, "gap_codes": codes, "result_by": actor, "result_at": _now(),
    }).eq("id", request_id).execute()
    row = (updated.data or [req])[0]

    if result == "passed":
        _t("enrolment_state").update({
            "phase0_completed_at": _now(), "phase0_completed_via": "test_out",
        }).eq("student_id", req["student_id"]).eq("course_id", req["course_id"]) \
          .is_("phase0_completed_at", "null").execute()
        # Confirms the seat if they have paid, retargets by the finish date.
        phase0_service.status(req["student_id"], req["course_id"])

    _end_link(req.get("assessor_link_id"))
    return row


def cancel(actor: str, request_id: str) -> dict:
    req = _get(request_id)
    if req["status"] not in OPEN:
        raise TestOutRefused("closed")
    if actor not in (req["student_id"], req["requested_by"]) and not hs.admin_can(actor, "ops"):
        raise TestOutRefused("not_allowed")
    result = _t("test_out_request").update({"status": "cancelled"}).eq("id", request_id).execute()
    _end_link(req.get("assessor_link_id"))
    return (result.data or [req])[0]


def _names(ids) -> dict[str, str]:
    """First and last name per user, batched. Names only: the rest of a
    user_profiles row (phone, location) is not this screen's business."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = (_t("user_profiles").select("id, first_name, last_name").in_("id", ids).execute()).data or []
    return {r["id"]: " ".join(p for p in (r.get("first_name"), r.get("last_name")) if p) or "Unnamed"
            for r in rows}


def _with_names(rows: list[dict]) -> list[dict]:
    names = _names([r.get("student_id") for r in rows] + [r.get("assessor_tutor_id") for r in rows])
    return [{**r, "student_name": names.get(r.get("student_id")),
             "assessor_name": names.get(r.get("assessor_tutor_id"))} for r in rows]


def overview(actor: str) -> dict:
    """GET /test-outs: the caller's requests plus what they may do, so each
    client can decide which controls to show without a second round trip."""
    is_ops = hs.admin_can(actor, "ops")
    rung = hs.rung_of(actor)
    return {
        "requests": _with_names(visible_to(actor)),
        "my_rung": rung,
        "can_send": is_ops or rung == SENIOR_RUNG,
        "can_link": is_ops,
    }


def candidates(actor: str) -> list[dict]:
    """Learners a staff member could send a test-out to: in Phase 0, not finished,
    no open request. ops sees everyone; a rung-4 tutor sees their own learners."""
    is_ops = hs.admin_can(actor, "ops")
    if not (is_ops or hs.rung_of(actor) == SENIOR_RUNG):
        raise TestOutRefused("not_allowed")
    rows = (
        _t("enrolment_state").select("student_id, course_id, state, phase0_completed_at, target_intake_id")
        .in_("state", ["phase0", "paid"]).is_("phase0_completed_at", "null").execute()
    ).data or []
    if not is_ops:
        mine = {r["student_id"] for r in (
            _t("student_tutor_link").select("student_id").eq("tutor_id", actor).eq("state", "active").execute()
        ).data or []}
        rows = [r for r in rows if r["student_id"] in mine]
    open_ = {(r["student_id"], r["course_id"]) for r in (
        _t("test_out_request").select("student_id, course_id").in_("status", list(OPEN)).execute()
    ).data or []}
    rows = [r for r in rows if (r["student_id"], r["course_id"]) not in open_]
    names = _names(r["student_id"] for r in rows)
    return [{"student_id": r["student_id"], "course_id": r["course_id"], "state": r["state"],
             "student_name": names.get(r["student_id"])} for r in rows]


def assessors(actor: str, request_id: str) -> list[dict]:
    """Tutors who could assess this request: active, rung >= 2. ops sees all of
    them (an unlinked one gets a guest link); a rung-4 tutor sees only tutors the
    learner already has."""
    req = _get(request_id)
    is_ops = hs.admin_can(actor, "ops")
    if not (is_ops or hs.rung_of(actor) == SENIOR_RUNG):
        raise TestOutRefused("not_allowed")
    linked = hs.linked_tutor_ids(req["student_id"])
    tutors = (_t("tutor").select("id, status, deleted_at").eq("status", "active").execute()).data or []
    out = []
    for t in tutors:
        if t.get("deleted_at"):
            continue
        rung = hs.rung_of(t["id"]) or 0
        if rung < ASSESSOR_MIN_RUNG or (not is_ops and t["id"] not in linked):
            continue
        out.append({"tutor_id": t["id"], "rung": rung, "linked": t["id"] in linked})
    names = _names(o["tutor_id"] for o in out)
    for o in out:
        o["name"] = names.get(o["tutor_id"])
    return sorted(out, key=lambda o: (not o["linked"], -o["rung"], o["name"] or ""))


def _email_of(user_id: str) -> str | None:
    try:
        return db.auth.admin.get_user_by_id(user_id).user.email
    except Exception as exc:  # noqa: BLE001
        logger.warning("no email for %s: %s", user_id, exc)
        return None


async def notify_learner(row: dict) -> None:
    """Background: tell a learner that staff sent them a test-out. Best effort.
    Nothing is sent when the learner asked for it themselves."""
    if not row or row.get("requested_as") == "learner":
        return
    to = _email_of(row["student_id"])
    if to:
        await email_service.send_template_email(to, "test_out", {
            "subject": "You can test out of Phase 0", "audience": "learner",
            "web_base_url": settings.web_base_url,
        }, tags=["test_out"])


async def notify_assessor(row: dict) -> None:
    """Background: tell the assigned tutor there is a test-out to run."""
    if not row or not row.get("assessor_tutor_id"):
        return
    to = _email_of(row["assessor_tutor_id"])
    if to:
        await email_service.send_template_email(to, "test_out", {
            "subject": "A Phase 0 test-out for you to assess", "audience": "assessor",
            "student_name": _names([row["student_id"]]).get(row["student_id"]) or "a learner",
            "web_base_url": settings.web_base_url,
        }, tags=["test_out"])


def visible_to(actor: str) -> list[dict]:
    """What a caller's test-out list shows: an ops admin sees every open request;
    a tutor sees those assigned to them; a learner sees their own."""
    q = _t("test_out_request").select(
        "id, student_id, course_id, requested_as, assessor_tutor_id, status, gap_codes, created_at, result_at"
    )
    if hs.admin_can(actor, "ops"):
        rows = q.in_("status", list(OPEN)).order("created_at").execute().data or []
    else:
        mine = q.eq("assessor_tutor_id", actor).eq("status", "assigned").order("created_at").execute().data or []
        own = (
            _t("test_out_request").select(
                "id, student_id, course_id, requested_as, assessor_tutor_id, status, gap_codes, created_at, result_at"
            ).eq("student_id", actor).order("created_at", desc=True).execute().data or []
        )
        rows = mine + [r for r in own if r["id"] not in {m["id"] for m in mine}]
    return rows
