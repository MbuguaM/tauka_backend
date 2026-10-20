"""Phase 0 and the learner's enrolment state (enrolment plan 1.3, 1.6; §98).

tauka-python is the only writer of `app.enrolment_state`. The app reads its own
row through `GET /enrolment/me` (it could read it under RLS, but the sync below
has to run first, and only the server may write).

THE SYNC. Every read reconciles the row with facts other tables already hold,
so no event has to be delivered for the state to be right:

  1. **Paid** (phase0 → paid): `app.student.tier` is not `free_tier`. §2c puts
     payment before the seat; the alphabet is a paid lesson anyway.
  2. **Phase 0 finished** (1.6, settled by the developer 2026-10-18): the
     `app.unit_progress` row for Phase 0's SECOND unit (the alphabet, the second
     preliminary unit by `unit_order`) has `status = 'completed'`. That is the
     same verdict the app's progress tab reports (`unit_completion.dart`), so the
     learner and this service cannot disagree about whether they finished.
     `phase0_completed_at` takes that row's `completed_at`, which the app never
     moves once set. A paid learner then goes paid → seat_confirmed.
  3. **Retarget**: a learner whose target cutoff has passed without finishing
     rolls forward to the next intake they can still make. No penalty — the
     playbook's rule. **Rehearsal intakes are never targeted** (§99): Hidar 2019
     is a simulated pod run by staff, not a formation a learner can join.

A heritage **test-out** (§99, `test_out_service`) is the other way to finish
Phase 0: a pass sets `phase0_completed_at` with `phase0_completed_via =
'test_out'`, and the same sync then confirms the seat.

`app.unit_progress` is tauka-flutter's table and is read here, never written.
It is written by the learner's own client (`app.upsert_unit_progress`), so
"finished" is self-reported in the same way the progress tab is; a tutor sees
the same record.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# The shortest realistic Phase 0 (playbook: two to three weeks). A new learner
# is aimed at the earliest intake whose lesson deadline is at least this far
# from the day they started.
PHASE0_MIN_DAYS = 14

PACES = (5, 10, 20)
BANDS = ("americas", "europe", "ea_gulf", "asia")

# States from which the sync may move a learner. Later states belong to the
# matcher and to admins.
_PRE_SEAT = ("phase0", "paid")


def _t(name: str):
    return db.schema("app").table(name)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _intakes() -> list[dict]:
    return (
        _t("intake")
        .select("id, ethiopic_month, ec_year, formation_date, cutoff_opens, cutoff_closes, tier, status, mode, season_id, band")
        .eq("status", "scheduled")
        .order("formation_date")
        .execute()
    ).data or []


def choose_target(intakes: list[dict], *, started: date, today: date, completed: date | None,
                  band: str | None = None) -> dict | None:
    """The intake a learner should aim at. Pure, so it can be tested alone.

    Finished Phase 0: the first intake whose deadline is on or after the day they
    finished (finish by 5 Nov → Hidar; on 6 Nov → Tahsas). Not finished: the
    first whose deadline is both still ahead and at least PHASE0_MIN_DAYS from
    when they started.
    """
    if completed is not None:
        floor = completed
    else:
        floor = max(today, started + timedelta(days=PHASE0_MIN_DAYS))
    for row in sorted(intakes, key=lambda r: _d(r["formation_date"])):
        if row.get("mode", "live") != "live":
            continue
        # Track 10 (§140): a band-only intake is for that band's learners only.
        if row.get("band") and row["band"] != band:
            continue
        if row.get("status", "scheduled") == "scheduled" and _d(row["cutoff_closes"]) >= floor:
            return row
    return None


def _alphabet_completed_at(student_id: str, course_id: str) -> date | None:
    """`completed_at` of Phase 0 unit 2, or None. See the module docstring."""
    # app.unit (tauka-flutter): is_preliminary marks Phase 0 units; unit_order
    # orders them (introduction 1, alphabet 2). We read ids only.
    prelim = (
        _t("unit").select("id, unit_order")
        .eq("course_id", course_id).eq("is_preliminary", True)
        .order("unit_order").execute()
    ).data or []
    if len(prelim) < 2:
        return None
    # app.unit_progress (tauka-flutter): one row per (unit, user); status is
    # not_started | in_progress | completed, completed_at set once.
    rows = (
        _t("unit_progress").select("status, completed_at")
        .eq("unit_id", prelim[1]["id"]).eq("user_id", student_id).limit(1).execute()
    ).data or []
    if not rows or rows[0].get("status") != "completed" or not rows[0].get("completed_at"):
        return None
    return datetime.fromisoformat(str(rows[0]["completed_at"]).replace("Z", "+00:00"))


def _is_paid(student_id: str) -> bool:
    rows = (_t("student").select("tier").eq("id", student_id).limit(1).execute()).data or []
    return bool(rows) and rows[0].get("tier") not in (None, "free_tier")


def _row(student_id: str, course_id: str) -> dict | None:
    rows = (
        _t("enrolment_state").select("*")
        .eq("student_id", student_id).eq("course_id", course_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _sync(row: dict) -> dict:
    """Apply the three rules; write only what changed. Returns the new row."""
    if row["state"] not in _PRE_SEAT:
        return row

    update: dict = {}
    state = row["state"]

    if state == "phase0" and _is_paid(row["student_id"]):
        state = "paid"

    completed_at = row.get("phase0_completed_at")
    if not completed_at:
        done = _alphabet_completed_at(row["student_id"], row["course_id"])
        if done is not None:
            completed_at = done.isoformat()
            update["phase0_completed_at"] = completed_at
            update["phase0_completed_via"] = "lessons"

    if state == "paid" and completed_at:
        state = "seat_confirmed"

    target = choose_target(
        _intakes(),
        started=_d(row.get("phase0_started_at") or row["created_at"]),
        today=_today(),
        completed=_d(completed_at) if completed_at else None,
        band=row.get("band"),
    )
    target_id = target["id"] if target else None
    if target_id != row.get("target_intake_id"):
        update["target_intake_id"] = target_id

    # Two hops (phase0 → paid → seat_confirmed) are written as two updates:
    # the guard trigger checks each transition, not the end-to-end jump.
    if state != row["state"]:
        if row["state"] == "phase0" and state == "seat_confirmed":
            _t("enrolment_state").update({"state": "paid"}).eq("id", row["id"]).execute()
        update["state"] = state

    if update:
        result = _t("enrolment_state").update(update).eq("id", row["id"]).execute()
        row = (result.data or [None])[0] or {**row, **update}
    return row


def _present(row: dict | None) -> dict:
    if row is None:
        return {"enrolled": False}
    target = None
    if row.get("target_intake_id"):
        found = (
            _t("intake")
            .select("ethiopic_month, ec_year, formation_date, cutoff_opens, cutoff_closes, tier, season:season_id(display_name)")
            .eq("id", row["target_intake_id"]).limit(1).execute()
        ).data or []
        if found:
            t = found[0]
            target = {
                "ethiopic_month": t["ethiopic_month"],
                "ec_year": t["ec_year"],
                "formation_date": str(t["formation_date"])[:10],
                "cutoff_opens": str(t["cutoff_opens"])[:10],
                "cutoff_closes": str(t["cutoff_closes"])[:10],
                "tier": t["tier"],
                "season": (t.get("season") or {}).get("display_name"),
            }
    return {
        "enrolled": True,
        "state": row["state"],
        "pace_pledge_hours": row.get("pace_pledge_hours"),
        "band": row.get("band"),
        "phase0_started_at": row.get("phase0_started_at"),
        "phase0_completed_at": row.get("phase0_completed_at"),
        "phase0_completed_via": row.get("phase0_completed_via"),
        "target_intake": target,
        "test_out": _open_test_out(row["student_id"], row["course_id"]),
        "slot_count": _slot_count(row["student_id"]),
        "alphabet_unit_id": _alphabet_unit_id(row["course_id"]),
    }


def _alphabet_unit_id(course_id: str) -> str | None:
    """Phase 0 unit 2 — the alphabet — so the app can open its Fidel readings
    (plan 1.4). Same rule as _alphabet_completed_at."""
    try:
        prelim = (_t("unit").select("id, unit_order").eq("course_id", course_id)
                  .eq("is_preliminary", True).order("unit_order").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("alphabet unit read failed for %s: %s", course_id, exc)
        return None
    return prelim[1]["id"] if len(prelim) >= 2 else None


def _slot_count(student_id: str) -> int:
    """How many catalogue slots the learner chose (plan 3.1, §104); 3+ is the
    matching guarantee."""
    try:
        return len((_t("student_slot_choice").select("slot_id").eq("student_id", student_id)
                    .execute()).data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot count read failed for %s: %s", student_id, exc)
        return 0


def _open_test_out(student_id: str, course_id: str) -> dict | None:
    """The learner's latest test-out request, so the app can show it. Never
    names who requested it; the assessor is the learner's own tutor."""
    try:
        rows = (
            _t("test_out_request")
            .select("id, status, assessor_tutor_id, gap_codes, created_at, result_at")
            .eq("student_id", student_id).eq("course_id", course_id)
            .order("created_at", desc=True).limit(1).execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("test-out read failed for %s: %s", student_id, exc)
        return None
    return rows[0] if rows else None


def status(student_id: str, course_id: str) -> dict:
    row = _row(student_id, course_id)
    return _present(_sync(row) if row else None)


def start(student_id: str, course_id: str, *, pace_pledge_hours: int | None, band: str | None) -> dict:
    """Begin Phase 0, or update the pledge and band. Idempotent."""
    row = _row(student_id, course_id)
    if row is None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            _t("enrolment_state").insert({
                "student_id": student_id,
                "course_id": course_id,
                "state": "phase0",
                "phase0_started_at": now,
                "pace_pledge_hours": pace_pledge_hours,
                "band": band,
            }).execute()
        except Exception:  # noqa: BLE001
            # A concurrent start won the UNIQUE (student_id, course_id) race.
            pass
        row = _row(student_id, course_id)
    else:
        update = {k: v for k, v in (("pace_pledge_hours", pace_pledge_hours), ("band", band)) if v is not None}
        if update:
            _t("enrolment_state").update(update).eq("id", row["id"]).execute()
            row = {**row, **update}
    if row is None:
        raise RuntimeError("enrolment_state row missing after start")
    return _present(_sync(row))


def advance(student_id: str, from_states: tuple[str, ...], to_state: str) -> int:
    """Move a learner's enrolment row(s) on a matching event (§102, plan 3.4).

    Called by the matcher and the offer flow, best effort: matching must never
    fail because the enrolment record couldn't follow it. Only rows currently in
    one of `from_states` move, and the database trigger refuses any pair not in
    `app.enrolment_transition_ok`, so a stray call can't teleport anyone.

    The events: offer made -> matched; offer accepted -> in_pod; no seat at the
    end of a cycle (declined, expired, deferred, or the pod dissolved) ->
    wait_pool. A learner with no enrolment row (one who never started Phase 0)
    is simply not touched.
    """
    moved = 0
    try:
        rows = (
            _t("enrolment_state").select("id, state")
            .eq("student_id", student_id).in_("state", list(from_states)).execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("enrolment read failed for %s: %s", student_id, exc)
        return 0
    for r in rows:
        if r["state"] == to_state:
            continue
        try:
            _t("enrolment_state").update({"state": to_state}).eq("id", r["id"]).execute()
            moved += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrolment %s -> %s refused for %s: %s", r["state"], to_state, student_id, exc)
    return moved

