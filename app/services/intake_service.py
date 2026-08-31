"""Loading the intake pool, running a cycle, and persisting the result.

`cohort_matcher` is pure and knows nothing about the database. This module is
the other half: it reads the pool, calls the matcher, and — **only in live
mode** — writes the cohorts, offers and deferrals it produced.

THE SPLIT IS WHAT MAKES THE DRY RUN FREE. Plan §4 and Phase 3: the job must be
runnable against synthetic or replayed pools, producing the proposed cohorts
*without writing them*, so the bands and weights can be tuned against realistic
distributions before a single real student is placed. A dry run here writes one
`app.intake_cycle` row carrying its proposals in `result` and touches nothing
else, so a bad configuration costs a row instead of an apology.

THE POOL IS FROZEN AT THE START OF A RUN (plan §3 Stage 7). A student signing up
mid-run lands in the *next* cycle, or the pipeline is no longer a pure function
of its inputs and results stop being reproducible. `load_pool` is called once.

WHAT THIS MODULE DOES NOT DECIDE. `matching_service` already owns the referral
hard preference, the commitment lock and the concentration cap, and O-16 records
that they "will be called unchanged". They are read here into `StudentIn`
fields, never re-derived.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone

from app.services import matching_service
from app.services.cohort_matcher import (
    CADENCE_CLASS_FOR_TIER,
    LEVEL_FROM_CEFR,
    SESSIONS_PER_MONTH,
    Candidate,
    MatchConfig,
    MatchResult,
    StudentIn,
    TimeWindow,
    TutorIn,
    run as run_matcher,
    summarise,
)
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

PAID_TIERS = tuple(CADENCE_CLASS_FOR_TIER)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")

# Cohort statuses that occupy a seat and a tutor. A `dissolved` or `completed`
# cohort holds neither, which is why the list is enumerated rather than
# expressed as "not dissolved".
LIVE_COHORT_STATUSES = ("forming", "active", "paused", "merging")


# ── Config ───────────────────────────────────────────────────────────────────

def _rows_to_config(rows: list[dict]) -> MatchConfig:
    raw = {r["key"]: r["value"] for r in rows if r.get("value") not in (None, "")}
    default = MatchConfig()

    def num(key: str, fallback, cast):
        try:
            return cast(raw[f"match.{key}"])
        except (KeyError, TypeError, ValueError):
            return fallback

    civil_start, civil_end = default.civil_start_minute, default.civil_end_minute
    try:
        lo, hi = raw["match.civil_hours"].split("-", 1)
        civil_start = int(lo.split(":")[0]) * 60 + int(lo.split(":")[1])
        civil_end = int(hi.split(":")[0]) * 60 + int(hi.split(":")[1])
    except (KeyError, ValueError, IndexError, AttributeError):
        pass

    weights = dict(default.weights)
    try:
        parsed = json.loads(raw["match.weights"])
        # Merge rather than replace: a config row that names three weights must
        # not silently zero the other seven.
        weights.update({k: float(v) for k, v in parsed.items() if k in weights})
    except (KeyError, ValueError, TypeError):
        pass

    return MatchConfig(
        tz_band_hours=num("tz_band_hours", default.tz_band_hours, float),
        lesson_band=num("lesson_band", default.lesson_band, int),
        civil_start_minute=civil_start,
        civil_end_minute=civil_end,
        cohort_min=num("cohort_min", default.cohort_min, int),
        cohort_max=num("cohort_max", default.cohort_max, int),
        min_preferences=num("min_preferences", default.min_preferences, int),
        session_minutes=num("session_minutes", default.session_minutes, int),
        offer_ranks=num("offer_ranks", default.offer_ranks, int),
        offer_ttl_hours=num("offer_ttl_hours", default.offer_ttl_hours, int),
        overbook_slack=num("overbook_slack", default.overbook_slack, int),
        relax_after_cycles=num("relax_after_cycles", default.relax_after_cycles, int),
        tz_band_hours_relaxed=num("tz_band_hours_relaxed",
                                  default.tz_band_hours_relaxed, float),
        lesson_band_relaxed=num("lesson_band_relaxed", default.lesson_band_relaxed, int),
        experience_years=num("experience_years", default.experience_years, int),
        referral_preference=raw.get("match.referral_preference",
                                    default.referral_preference),
        weights=weights,
    )


def load_config() -> MatchConfig:
    """Read every `match.*` key. A missing or unparseable value degrades to the
    documented default rather than raising — a bad config row must not be able
    to stop a cycle, only to make it run on the numbers the plan chose.
    """
    try:
        result = db.schema("app").table("config").select("key, value").like(
            "key", "match.%").execute()
        return _rows_to_config(result.data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("match config read failed, using defaults: %s", exc)
        return MatchConfig()


def is_intake_day(today: date | None = None) -> bool:
    """`match.intake_days` (default "1,15"), the same cadence-guard shape as
    `payout.cadence_day` — one daily cron, many jobs behind their own clocks."""
    today = today or datetime.now(timezone.utc).date()
    try:
        result = db.schema("app").table("config").select("value").eq(
            "key", "match.intake_days").limit(1).execute()
        raw = (result.data or [{}])[0].get("value") or "1,15"
    except Exception as exc:  # noqa: BLE001
        logger.warning("intake day config read failed: %s", exc)
        raw = "1,15"
    try:
        days = {int(p.strip()) for p in raw.split(",") if p.strip()}
    except ValueError:
        days = {1, 15}
    return today.day in days


# ── Loading the pool ─────────────────────────────────────────────────────────

def _native_language(known: object) -> str | None:
    """The student's strongest declared language, from `user_profiles
    .known_languages` — a jsonb list of `{name, proficiency}`.

    There is no `native_language` column on the student side, so the highest
    self-declared proficiency stands in for L1. It is a proxy and it is only
    ever used for a soft, decaying bonus (+15 x beginnerness), which is the
    right blast radius for an inference.
    """
    if not isinstance(known, list) or not known:
        return None
    best = None
    for entry in known:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        prof = entry.get("proficiency")
        prof = prof if isinstance(prof, (int, float)) else 0
        if best is None or prof > best[0]:
            best = (prof, entry["name"])
    return best[1] if best else None


def _current_lesson_map(student_ids: list[str]) -> dict[str, int]:
    """How far through the curriculum each student is.

    Reads `app.unit_progress`, the existing source of truth, and NOT a copy on
    the membership — two sources of truth for "which lesson is this student on"
    is how the streak columns went wrong (CONFLICT-032).

    `unit_order` is NOT unique: production carries preliminary units at orders 1
    and 2, colliding with content units 1 and 2. Preliminary units are excluded
    by `unit.level IS NOT NULL`, which §85.4 set for content units only.
    """
    out = {sid: 1 for sid in student_ids}
    if not student_ids:
        return out
    try:
        rows = db.schema("app").table("unit_progress").select(
            "user_id, unit_id, status"
        ).in_("user_id", student_ids).eq("status", "completed").execute()
        progress = rows.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("unit progress read failed: %s", exc)
        return out
    if not progress:
        return out

    unit_ids = sorted({r["unit_id"] for r in progress if r.get("unit_id")})
    try:
        units = db.schema("app").table("unit").select(
            "id, unit_order, level").in_("id", unit_ids).not_.is_(
            "level", "null").execute()
        order_of = {u["id"]: u["unit_order"] for u in (units.data or [])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("unit read failed: %s", exc)
        return out

    for r in progress:
        order = order_of.get(r.get("unit_id"))
        if order is None:
            continue
        # "Currently on" is the next unit after the furthest completed one.
        out[r["user_id"]] = max(out.get(r["user_id"], 1), order + 1)
    return out


def _cycles_waited_map(student_ids: list[str]) -> dict[str, int]:
    """A FOLD OVER `deferred_student`, NEVER A COUNTER COLUMN.

    The same reasoning the money ledgers use: a stored count drifts, and this
    one feeds the tie-break that decides who waits another fortnight. Counted
    since the student last joined a cohort, so a placed-then-dropped student
    starts again rather than carrying a grievance forward for ever.
    """
    out = {sid: 0 for sid in student_ids}
    if not student_ids:
        return out
    joined_since: dict[str, str] = {}
    try:
        rows = db.schema("app").table("cohort_memberships").select(
            "user_id, joined_at").in_("user_id", student_ids).execute()
        for r in rows.data or []:
            prev = joined_since.get(r["user_id"])
            if r.get("joined_at") and (prev is None or r["joined_at"] > prev):
                joined_since[r["user_id"]] = r["joined_at"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("membership read failed: %s", exc)

    try:
        rows = db.schema("app").table("deferred_student").select(
            "student_id, created_at").in_("student_id", student_ids).execute()
        for r in rows.data or []:
            cutoff = joined_since.get(r["student_id"])
            if cutoff and r["created_at"] <= cutoff:
                continue
            out[r["student_id"]] = out.get(r["student_id"], 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("deferral history read failed: %s", exc)
    return out


def load_pool() -> list[StudentIn]:
    """Every student who has bought live sessions and is not already in one.

    A student already holding a seat in a live cohort is NOT in the pool: this
    is intake, not reshuffling. Moving someone between cohorts is the drift
    monitor's job (plan §8) and it offers, it never reassigns.
    """
    try:
        students = db.schema("app").table("student").select(
            "id, tier, assessed_level"
        ).in_("tier", list(PAID_TIERS)).is_("deleted_at", "null").execute()
        rows = students.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("intake pool read failed: %s", exc)
        return []
    if not rows:
        return []

    ids = sorted(r["id"] for r in rows)

    try:
        seated = db.schema("app").table("cohort_memberships").select(
            "user_id, cohort_id, status").in_("user_id", ids).eq(
            "status", "active").execute()
        seated_ids = {r["user_id"] for r in seated.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("seated membership read failed: %s", exc)
        seated_ids = set()

    try:
        profiles = db.schema("app").table("user_profiles").select(
            "id, timezone, learning_goal, study_method, known_languages"
        ).in_("id", ids).execute()
        profile_of = {p["id"]: p for p in profiles.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile read failed: %s", exc)
        profile_of = {}

    try:
        prefs = db.schema("app").table("student_time_preference").select(
            "student_id, day_of_week, start_time, end_time, preference_rank"
        ).in_("student_id", ids).eq("active", True).execute()
        pref_rows = prefs.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("time preference read failed: %s", exc)
        pref_rows = []

    windows: dict[str, list[TimeWindow]] = {}
    for r in pref_rows:
        windows.setdefault(r["student_id"], []).append(TimeWindow(
            day_of_week=r["day_of_week"],
            start_minute=_minutes(r["start_time"]),
            end_minute=_minutes(r["end_time"]),
            rank=r.get("preference_rank") or 1,
        ))

    lessons = _current_lesson_map(ids)
    waited = _cycles_waited_map(ids)
    priority_ids = _priority_ids(ids)

    pool: list[StudentIn] = []
    for r in sorted(rows, key=lambda x: x["id"]):
        sid = r["id"]
        if sid in seated_ids:
            continue
        profile = profile_of.get(sid, {})
        pool.append(StudentIn(
            id=sid,
            tier=r["tier"],
            level=LEVEL_FROM_CEFR.get((r.get("assessed_level") or "").upper()),
            current_lesson=lessons.get(sid, 1),
            tz=profile.get("timezone"),
            windows=tuple(sorted(
                windows.get(sid, []),
                key=lambda w: (w.day_of_week, w.start_minute))),
            goal=profile.get("learning_goal"),
            style=profile.get("study_method"),
            native_language=_native_language(profile.get("known_languages")),
            priority=(r["tier"] == "intensive_tier") or (sid in priority_ids),
            cycles_waited=waited.get(sid, 0),
            locked_tutor_id=matching_service.locked_by_commitment(sid),
            referral_tutor_id=matching_service.preferred_home_tutor(sid),
        ))
    return pool


def _priority_ids(student_ids: list[str]) -> set[str]:
    """`enrollment_commitment.priority_matching` on an active term — the same
    promise the Intensive tier sells, bought by the term instead."""
    if not student_ids:
        return set()
    try:
        rows = db.schema("app").table("enrollment_commitment").select(
            "student_id, priority_matching, status"
        ).in_("student_id", student_ids).eq("status", "active").execute()
        return {r["student_id"] for r in rows.data or [] if r.get("priority_matching")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("commitment priority read failed: %s", exc)
        return set()


def _minutes(raw: str | None) -> int:
    """"HH:MM:SS" -> minutes past midnight."""
    if not raw:
        return 0
    parts = str(raw).split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def load_tutors() -> list[TutorIn]:
    """Active tutors with a teaching profile and availability.

    NO PROFILE MEANS NOT CERTIFIED FOR COHORT TEACHING, and that is the honest
    default for a tutor who has only ever taught 1-on-1 — inventing a level-1
    certification for them would put a student in front of someone nobody
    assessed.
    """
    try:
        tutors = db.schema("app").table("tutor").select(
            "id, status").eq("status", "active").is_("deleted_at", "null").execute()
        rows = tutors.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("tutor read failed: %s", exc)
        return []
    if not rows:
        return []

    ids = sorted(r["id"] for r in rows)
    try:
        profiles = db.schema("app").table("tutor_teaching_profile").select("*").in_(
            "tutor_id", ids).execute()
        profile_of = {p["tutor_id"]: p for p in profiles.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("teaching profile read failed: %s", exc)
        profile_of = {}

    try:
        avail = db.schema("app").table("tutor_availability").select(
            "tutor_id, day_of_week, start_time, end_time, timezone"
        ).in_("tutor_id", ids).eq("active", True).execute()
        avail_rows = avail.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("availability read failed: %s", exc)
        avail_rows = []

    windows: dict[str, list[TimeWindow]] = {}
    for r in avail_rows:
        windows.setdefault(r["tutor_id"], []).append(TimeWindow(
            day_of_week=r["day_of_week"],
            start_minute=_minutes(r["start_time"]),
            end_minute=_minutes(r["end_time"]),
            tz=r.get("timezone"),
        ))

    try:
        cohorts = db.schema("app").table("cohorts").select(
            "tutor_id, status").in_("tutor_id", ids).in_(
            "status", list(LIVE_COHORT_STATUSES)).execute()
        load_of: dict[str, int] = {}
        for r in cohorts.data or []:
            load_of[r["tutor_id"]] = load_of.get(r["tutor_id"], 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor load read failed: %s", exc)
        load_of = {}

    out: list[TutorIn] = []
    for tid in ids:
        profile = profile_of.get(tid)
        if not profile:
            continue
        out.append(TutorIn(
            id=tid,
            min_level=profile.get("min_level") or 1,
            max_level=profile.get("max_level") or 1,
            max_concurrent_cohorts=profile.get("max_concurrent_cohorts") or 0,
            current_cohorts=load_of.get(tid, 0),
            availability=tuple(sorted(
                windows.get(tid, []),
                key=lambda w: (w.day_of_week, w.start_minute))),
            specialisations=tuple(profile.get("specialisations") or ()),
            teaching_style=profile.get("teaching_style"),
            native_language=profile.get("native_language"),
            years_experience=profile.get("years_experience"),
        ))
    return out


# ── Running a cycle ──────────────────────────────────────────────────────────

def _serialise(result: MatchResult) -> dict:
    """The dry run's whole output, as jsonb. Ids and numbers only — a proposal
    is not the place for a name."""
    return {
        "stats": result.stats,
        "cohorts": [{
            "students": list(c.student_ids),
            "tutor_id": c.slot.tutor_id,
            "level": c.level,
            "cadence_class": c.cadence_class,
            "slot_day_of_week": c.slot.local_day_of_week,
            "slot_minute": c.slot.local_start_minute,
            "slot_timezone": c.slot.tz,
            "score": c.score,
            "breakdown": c.breakdown,
            "band": [c.lesson_low, c.lesson_high],
            "bands_relaxed": c.bands_relaxed,
        } for c in result.cohorts],
        "offers": {
            sid: [{"tutor_id": c.slot.tutor_id, "score": c.score, "rank": i + 1}
                  for i, c in enumerate(cands)]
            for sid, cands in sorted(result.offers.items())
        },
        "deferrals": [{
            "student_id": d.student_id, "reason": d.reason,
            "detail": d.detail, "cycles_waited": d.cycles_waited,
        } for d in result.deferrals],
    }


def _cohort_name(c: Candidate) -> str:
    hh, mm = divmod(c.slot.local_start_minute, 60)
    return (f"Level {c.level} · {DAY_NAMES[c.slot.local_day_of_week]} "
            f"{hh:02d}:{mm:02d} ({c.slot.tz})")


def _course_for(student_ids: list[str]) -> str | None:
    """The course the cohort studies — the one its members are enrolled on.

    Modal rather than first: on a one-course platform they agree, and when they
    do not, the group's course is the one most of them are actually taking.
    """
    try:
        rows = db.schema("app").table("student_course").select(
            "student_id, course_id").in_("student_id", student_ids).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("student course read failed: %s", exc)
        return None
    counts: dict[str, int] = {}
    for r in rows.data or []:
        if r.get("course_id"):
            counts[r["course_id"]] = counts.get(r["course_id"], 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


async def run_cycle(
    mode: str = "dry_run",
    cycle_date: date | None = None,
    notes: str | None = None,
) -> dict:
    """One run of the whole pipeline.

    A LIVE RUN WRITES COHORTS AND OFFERS BUT NO MEMBERSHIPS. A cohort is created
    `forming` with zero members; membership is what *accepting* an offer creates
    (`offer_service.accept`). Writing memberships here would place people who
    have not been asked, which is the difference between an offer and an
    assignment.
    """
    cfg = load_config()
    cycle_date = cycle_date or datetime.now(timezone.utc).date()
    pool = load_pool()
    tutors = load_tutors()
    result = run_matcher(pool, tutors, cfg, cycle_date)

    if mode not in ("dry_run", "live"):
        raise ValueError("mode must be 'dry_run' or 'live'")

    row = {
        "cycle_date": cycle_date.isoformat(),
        "mode": mode,
        "status": "running",
        "config": cfg.as_dict(),
        "pool_size": result.stats["pool_size"],
        "students_placed": 0,
        "students_deferred": 0,
        "cohorts_formed": 0,
        "notes": notes,
    }
    if mode == "dry_run":
        row["result"] = _serialise(result)
        row["status"] = "closed"
        row["students_deferred"] = len(result.deferrals)
        row["closed_at"] = datetime.now(timezone.utc).isoformat()

    try:
        created = db.schema("app").table("intake_cycle").insert(row).execute()
        cycle = (created.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        # A live cycle already exists for this date — the partial unique index
        # doing its job. Plan §3 Stage 7: the pool is frozen per run, and a
        # second run on the same date would re-freeze a pool the first has
        # already placed people out of.
        logger.error("intake cycle insert failed: %s", exc)
        return {"ok": False, "error": str(exc), "stats": result.stats}

    if mode == "dry_run":
        return {"ok": True, "cycle_id": cycle["id"], "mode": mode,
                "stats": result.stats, "proposals": _serialise(result)}

    return _persist_live(cycle["id"], cycle_date, cfg, result, pool)


def _persist_live(
    cycle_id: str,
    cycle_date: date,
    cfg: MatchConfig,
    result: MatchResult,
    pool: list[StudentIn],
) -> dict:
    """Write the cohorts, the offers and the deferrals.

    Not one transaction, because the Supabase REST client cannot open one. The
    order is chosen so a failure part-way leaves something recoverable rather
    than something wrong: cohorts first (a `forming` cohort with no offers is
    swept at close), offers second, deferrals last (a missing deferral row costs
    an explanation, not a placement).
    """
    cohort_ids: dict[tuple, str] = {}
    formed = 0

    for c in result.cohorts:
        hh, mm = divmod(c.slot.local_start_minute, 60)
        payload = {
            "name": _cohort_name(c),
            "tutor_id": c.slot.tutor_id,
            "course_id": _course_for(list(c.student_ids)),
            "level": c.level,
            "cadence_class": c.cadence_class,
            "sessions_per_month": SESSIONS_PER_MONTH[c.cadence_class],
            "band_lesson_low": c.lesson_low,
            "band_lesson_high": c.lesson_high,
            "slot_day_of_week": c.slot.local_day_of_week,
            "slot_time": f"{hh:02d}:{mm:02d}:00",
            "slot_timezone": c.slot.tz,
            "status": "forming",
            "max_students": cfg.cohort_max,
            "intake_cycle": cycle_date.isoformat(),
            "start_date": cycle_date.isoformat(),
        }
        try:
            created = db.schema("app").table("cohorts").insert(payload).execute()
            cohort_ids[(c.slot.tutor_id, c.slot.start_utc.isoformat(),
                        c.student_ids)] = (created.data or [{}])[0].get("id")
            formed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("cohort insert failed: %s", exc)

    expires = datetime.now(timezone.utc) + timedelta(hours=cfg.offer_ttl_hours)
    # The pool the matcher ran on, not a fresh read. Re-reading here would break
    # the freeze (plan §3 Stage 7) for the sake of one boolean, and the boolean
    # would then describe a student the run never saw.
    pool_by_id = {s.id: s for s in pool}
    offers_written = 0

    for sid, candidates in sorted(result.offers.items()):
        student = pool_by_id.get(sid)
        for rank, c in enumerate(candidates, start=1):
            cohort_id = cohort_ids.get(
                (c.slot.tutor_id, c.slot.start_utc.isoformat(), c.student_ids))
            if not cohort_id:
                continue
            try:
                db.schema("app").table("match_offer").insert({
                    "cycle_id": cycle_id,
                    "student_id": sid,
                    "cohort_id": cohort_id,
                    "tutor_id": c.slot.tutor_id,
                    "rank": rank,
                    "score": c.score,
                    "score_breakdown": {**c.breakdown, "summary": summarise(c)},
                    "priority": bool(student and student.priority),
                    "status": "offered",
                    "expires_at": expires.isoformat(),
                }).execute()
                offers_written += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("offer insert failed for %s: %s", sid, exc)

    deferred = 0
    for d in result.deferrals:
        try:
            db.schema("app").table("deferred_student").insert({
                "cycle_id": cycle_id,
                "student_id": d.student_id,
                "reason": d.reason,
                "detail": d.detail,
                "cycles_waited": d.cycles_waited,
                "bands_relaxed": d.bands_relaxed,
            }).execute()
            deferred += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("deferral insert failed for %s: %s", d.student_id, exc)

    try:
        db.schema("app").table("intake_cycle").update({
            "status": "offered",
            "cohorts_formed": formed,
            "students_placed": len(result.placed_ids),
            "students_deferred": deferred,
        }).eq("id", cycle_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("intake cycle update failed: %s", exc)

    logger.info(
        "Intake cycle %s: pool=%s placed=%s cohorts=%s offers=%s deferred=%s",
        cycle_id, result.stats["pool_size"], len(result.placed_ids),
        formed, offers_written, deferred,
    )
    return {
        "ok": True, "cycle_id": cycle_id, "mode": "live",
        "stats": result.stats, "cohorts_formed": formed,
        "offers": offers_written, "deferred": deferred,
    }


# ── Readiness and reporting ──────────────────────────────────────────────────

def readiness(student_id: str) -> dict:
    """Is this student matchable, and if not, what is missing?

    THE POINT IS THAT THE STUDENT CAN FIX MOST OF IT THEMSELVES. Three of the
    eleven deferral reasons are a form they have not filled in — no timezone,
    fewer than three windows, no placement result — and telling someone "we
    could not place you" when the answer is "you have not told us where you
    are" is the failure plan §4b Stage 1 exists to prevent. Checked live rather
    than read from the last cycle, because the last cycle is up to a fortnight
    old and they may have fixed it this morning.
    """
    cfg = load_config()
    out = {
        "matchable": False,
        "missing": [],
        "timezone": None,
        "preferences": 0,
        "min_preferences": cfg.min_preferences,
        "level": None,
        "cadence_class": None,
        "in_cohort": False,
    }
    try:
        rows = db.schema("app").table("student").select(
            "id, tier, assessed_level").eq("id", student_id).limit(1).execute()
        student = (rows.data or [None])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness student read failed: %s", exc)
        return out
    if not student:
        return out

    out["cadence_class"] = CADENCE_CLASS_FOR_TIER.get(student.get("tier"))
    out["level"] = LEVEL_FROM_CEFR.get((student.get("assessed_level") or "").upper())

    try:
        profile = db.schema("app").table("user_profiles").select(
            "timezone").eq("id", student_id).limit(1).execute()
        out["timezone"] = (profile.data or [{}])[0].get("timezone")
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness profile read failed: %s", exc)

    try:
        prefs = db.schema("app").table("student_time_preference").select(
            "id").eq("student_id", student_id).eq("active", True).execute()
        out["preferences"] = len(prefs.data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness preference read failed: %s", exc)

    try:
        seat = db.schema("app").table("cohort_memberships").select(
            "cohort_id").eq("user_id", student_id).eq(
            "status", "active").limit(1).execute()
        out["in_cohort"] = bool(seat.data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness membership read failed: %s", exc)

    if out["cadence_class"] is None:
        # Not a fault and not a missing field: a free-tier student has not
        # bought live sessions, so there is nothing for them to complete.
        out["missing"].append("paid_tier")
    if not out["timezone"]:
        out["missing"].append("timezone")
    if out["preferences"] < cfg.min_preferences:
        out["missing"].append("time_preferences")
    if out["level"] is None:
        out["missing"].append("placement_level")

    out["matchable"] = not out["missing"] and not out["in_cohort"]
    return out


def deferral_summary(limit: int = 200) -> dict:
    """The recruitment brief, grouped.

    Plan §4b: "the deferral table is the supply plan". The counts are what a
    hiring decision reads; the rows underneath are what an individual student is
    owed, and they stay separate so nobody mistakes an aggregate for a person.
    """
    try:
        rows = db.schema("app").table("deferred_student").select(
            "student_id, reason, detail, cycles_waited, bands_relaxed, created_at"
        ).order("created_at", desc=True).limit(limit).execute()
        data = rows.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("deferral summary read failed: %s", exc)
        return {"by_reason": {}, "rows": [], "longest_waiting": []}

    by_reason: dict[str, int] = {}
    for r in data:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1

    # Plan §4: "track median cycles_waited PER TIER, not just overall, or this
    # stays invisible behind a healthy-looking average" — the prediction being
    # that the tier paying most waits longest.
    longest = sorted(data, key=lambda r: (-(r.get("cycles_waited") or 0),
                                          r["student_id"]))[:10]
    return {
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "rows": data,
        "longest_waiting": longest,
    }
