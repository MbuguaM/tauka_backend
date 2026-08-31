"""The cohort matcher — plan `tutor-cohort-matching-plan.md` §3, Phases 3-4.

**This module touches no database.** The whole pipeline is a pure function

    (intake pool, tutor set, config) -> (cohorts, offers, deferrals)

and persisting the result is a separate step in `intake_service`. That is what
makes the dry run in plan §4 free rather than a feature: the same code that
places real students runs against a synthetic pool with nothing to undo.

WHY IT IS SET-PACKING AND NOT A GREEDY LOOP OVER PAIR SCORES.

The spec scored tutor-student *pairs*, but three of its constraints are
properties of a *set* — "within ±2 hours of each other" holds among students,
"within 5 lessons of each other" is a property of the group, and the cohort
completion bonus depends on who has already been placed. So a pair's score
changes with iteration order, the outcome is non-deterministic, and the students
processed last are reliably stranded: every compatible group is either full or
too small to open. Plan §3 keeps every number the spec chose — 30/20/15/10/25,
±2h, ≤5 lessons, 3-4 members, top 2-3 options — and changes the *unit* they
apply to.

DETERMINISM IS A REQUIREMENT, NOT A NICETY. A rerun must produce the same
answer or nothing is debuggable and nothing is explicable to a student. Every
tie in this module breaks on an explicit total order; nothing iterates a set,
and no float comparison decides an ordering without an id as its last key.

WHAT THIS MODULE DOES NOT DECIDE. The rules that already exist are called, not
reimplemented — `matching_service` owns the referral hard preference, the
commitment lock, the concentration cap and the guest-pool rules, and O-16 says
they "will be called unchanged". They enter here as already-resolved fields on
`StudentIn` (`locked_tutor_id`, `referral_tutor_id`, `priority`), because a pure
function cannot query, and leaving them as inputs is what keeps one copy of each
rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# 0 = Monday .. 6 = Sunday, matching `app.tutor_availability.day_of_week` as the
# web portal writes it (`tutor-schedule.jsx` keys its grid Mon..Sun) and
# `app.student_time_preference.day_of_week`. Python's `weekday()` uses the same
# convention, which is why no translation table appears anywhere below.
MONDAY = 0

# Candidate slot starts are laid on this grid inside a tutor's availability
# window. Finer would multiply candidates for no gain — nobody schedules a
# lesson at 14:07 — and coarser would lose the half-hour starts people use.
SLOT_GRANULARITY_MINUTES = 30

# The four levels, from a CEFR placement result. B1+ and everything above it is
# level 4: the product has four levels of fifteen lessons and no fifth, so a C1
# student is placed at the top rather than refused for being too good.
LEVEL_FROM_CEFR = {
    "A1": 1, "A2": 2, "B1": 3, "B1+": 4, "B2": 4, "C1": 4, "C2": 4,
}

# Tier -> cadence class. Plan §1b.2: class is a HARD constraint, because mixing
# classes is what produced a member absent from two meetings in three.
CADENCE_CLASS_FOR_TIER = {
    "learner_tier": "A",
    "tutor_tier": "B",
    "intensive_tier": "B",
}

SESSIONS_PER_MONTH = {"A": 1, "B": 3}

# Plan §3 Stage 6, extended by migration §86.3. Every unplaced student gets one
# of these plus the diagnostic that would resolve it.
REASON_NO_TIMEZONE = "NO_TIMEZONE"
REASON_TOO_FEW_PREFERENCES = "TOO_FEW_PREFERENCES"
REASON_NO_LEVEL = "NO_LEVEL"
REASON_NO_TUTOR_FOR_LEVEL = "NO_TUTOR_FOR_LEVEL"
REASON_NO_SLOT_OVERLAP = "NO_SLOT_OVERLAP"
REASON_TZ_ISOLATE = "TZ_ISOLATE"
REASON_TOO_FEW_PEERS = "TOO_FEW_PEERS_AT_LEVEL"
REASON_TUTORS_AT_CAPACITY = "TUTORS_AT_CAPACITY"


# ── Configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MatchConfig:
    """Every band and weight the run used.

    Snapshotted verbatim into `app.intake_cycle.config` (plan §3 Stage 3):
    "a past cycle's result is inexplicable without knowing what it ran under".
    Frozen so no stage can quietly retune mid-run.
    """

    tz_band_hours: float = 2.0
    lesson_band: int = 5
    civil_start_minute: int = 7 * 60
    civil_end_minute: int = 22 * 60
    cohort_min: int = 3
    cohort_max: int = 4
    min_preferences: int = 3
    session_minutes: int = 50
    offer_ranks: int = 3
    offer_ttl_hours: int = 48
    overbook_slack: int = 2
    relax_after_cycles: int = 2
    tz_band_hours_relaxed: float = 3.0
    lesson_band_relaxed: int = 8
    experience_years: int = 3
    referral_preference: str = "hard"
    weights: dict[str, float] = field(default_factory=lambda: {
        "goal": 30.0, "style": 20.0, "l1": 15.0, "experience": 10.0,
        "size_4": 25.0, "size_3": 15.0, "tightness_lesson": 10.0,
        "tightness_tz": 5.0, "slot_pref": 10.0, "tier_cohesion": 20.0,
    })

    def as_dict(self) -> dict:
        return {
            "tz_band_hours": self.tz_band_hours,
            "lesson_band": self.lesson_band,
            "civil_start_minute": self.civil_start_minute,
            "civil_end_minute": self.civil_end_minute,
            "cohort_min": self.cohort_min,
            "cohort_max": self.cohort_max,
            "min_preferences": self.min_preferences,
            "session_minutes": self.session_minutes,
            "offer_ranks": self.offer_ranks,
            "offer_ttl_hours": self.offer_ttl_hours,
            "overbook_slack": self.overbook_slack,
            "relax_after_cycles": self.relax_after_cycles,
            "tz_band_hours_relaxed": self.tz_band_hours_relaxed,
            "lesson_band_relaxed": self.lesson_band_relaxed,
            "experience_years": self.experience_years,
            "referral_preference": self.referral_preference,
            "weights": dict(self.weights),
        }

    def bands_for(self, cycles_waited: int) -> tuple[float, int]:
        """Cold-start ladder Stage 2 (plan §4b): after N failed cycles the bands
        widen, automatically and system-side, before any human remedy.

        Returned per student rather than set globally, because a student on
        their third cycle is owed the looser band and a student on their first
        is not — and a cohort that mixes them is flagged as relaxed by the
        loosest member, which is the honest label.
        """
        if cycles_waited >= self.relax_after_cycles:
            return self.tz_band_hours_relaxed, self.lesson_band_relaxed
        return self.tz_band_hours, self.lesson_band


# ── Inputs ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimeWindow:
    """A weekly availability window, in the owner's own local time."""

    day_of_week: int
    start_minute: int
    end_minute: int
    rank: int = 1
    # Tutor rows carry their own zone (a tutor may enter 09:00 Nairobi and 09:00
    # London); student rows do not, and inherit the profile zone. See §85.1.
    tz: str | None = None


@dataclass(frozen=True)
class StudentIn:
    id: str
    tier: str
    level: int | None
    current_lesson: int
    tz: str | None
    windows: tuple[TimeWindow, ...] = ()
    goal: str | None = None
    style: str | None = None
    native_language: str | None = None
    priority: bool = False
    cycles_waited: int = 0
    # Resolved by `matching_service` before the pool reaches here.
    locked_tutor_id: str | None = None
    referral_tutor_id: str | None = None

    @property
    def cadence_class(self) -> str | None:
        return CADENCE_CLASS_FOR_TIER.get(self.tier)


@dataclass(frozen=True)
class TutorIn:
    id: str
    min_level: int
    max_level: int
    max_concurrent_cohorts: int
    current_cohorts: int = 0
    availability: tuple[TimeWindow, ...] = ()
    specialisations: tuple[str, ...] = ()
    teaching_style: str | None = None
    native_language: str | None = None
    years_experience: int | None = None

    @property
    def free_cohort_seats(self) -> int:
        return max(0, self.max_concurrent_cohorts - self.current_cohorts)


@dataclass(frozen=True)
class TutorSlot:
    """A tutor plus one 50-minute UTC instant in the canonical week.

    Consumable exactly once: a tutor cannot hold two cohorts in the same slot.
    `start_utc` is a real instant — the first occurrence on or after the cycle
    date — rather than a (weekday, minute) pair, so every downstream conversion
    is exact rather than approximately exact. `local_*` is the same moment in
    the tutor's zone, which is what gets stored on the cohort.
    """

    tutor_id: str
    start_utc: datetime
    local_day_of_week: int
    local_start_minute: int
    tz: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.tutor_id, self.start_utc.isoformat())


@dataclass(frozen=True)
class Candidate:
    """A proposed cohort: a set of students at one level, in one class, on one
    TutorSlot. Candidates overlap heavily by design — one student appears in
    many — and Stage 4 does the choosing."""

    student_ids: tuple[str, ...]
    slot: TutorSlot
    cadence_class: str
    level: int
    score: int
    breakdown: dict[str, float]
    lesson_low: int
    lesson_high: int
    bands_relaxed: bool
    # The members' tie-break keys, sorted. Carried on the candidate because
    # Stage 4 chooses between candidates and therefore between *sets* of
    # claims — see `_candidate_order`.
    claim: tuple = ()

    @property
    def size(self) -> int:
        return len(self.student_ids)


@dataclass
class Deferral:
    student_id: str
    reason: str
    detail: dict
    cycles_waited: int
    bands_relaxed: bool = False


@dataclass
class MatchResult:
    cohorts: list[Candidate]
    # student_id -> ranked candidates, best first. Rank 1 is their cohort from
    # the packing; ranks 2-3 are their next-best still-open candidates.
    offers: dict[str, list[Candidate]]
    deferrals: list[Deferral]
    stats: dict

    @property
    def placed_ids(self) -> set[str]:
        return {s for c in self.cohorts for s in c.student_ids}


# ── Stage 0 — normalise to a canonical week ──────────────────────────────────

def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A bad zone name must defer one student, never raise and kill a cycle.
        logger.warning("unknown IANA zone %r — treating as missing", name)
        return None


def first_occurrence_utc(
    zone: ZoneInfo, ref: date, day_of_week: int, minute_of_day: int
) -> datetime:
    """The first instant on or after `ref` at which it is `minute_of_day` on
    `day_of_week` in `zone`.

    CONVERTED WITH A REAL IANA ZONE, NEVER A FIXED OFFSET (plan §3 Stage 0).
    An intake on the 1st placing a cohort that starts after a DST change would
    otherwise slip by an hour for half its members. Resolving to a concrete
    instant here, rather than carrying (weekday, offset) around, is what makes
    every later comparison exact.
    """
    delta = (day_of_week - ref.weekday()) % 7
    local_date = ref + timedelta(days=delta)
    naive = datetime.combine(
        local_date, time(hour=minute_of_day // 60, minute=minute_of_day % 60)
    )
    # `fold=0` picks the first of an ambiguous repeated hour; on a spring-forward
    # gap Python normalises forward. Both are one-off, both are documented, and
    # both beat raising on a date nobody chose.
    return naive.replace(tzinfo=zone).astimezone(timezone.utc)


def _local_minutes(instant: datetime, zone: ZoneInfo) -> tuple[int, int]:
    """(day_of_week, minute_of_day) for an instant, seen from `zone`."""
    local = instant.astimezone(zone)
    return local.weekday(), local.hour * 60 + local.minute


def _offset_hours(instant: datetime, zone: ZoneInfo) -> float:
    off = zone.utcoffset(instant.astimezone(zone).replace(tzinfo=None))
    return (off.total_seconds() / 3600.0) if off else 0.0


def build_tutor_slots(
    tutors: list[TutorIn], cfg: MatchConfig, cycle_date: date
) -> list[TutorSlot]:
    """Stage 1's supply side: every slot where a tutor has >= session_minutes
    contiguous free time **and** has not reached `max_concurrent_cohorts`.

    Capacity is applied here rather than in the edge test, because a tutor at
    their limit has no slots at all — and a student whose only tutor is full is
    owed TUTORS_AT_CAPACITY, a supply-side answer, rather than NO_SLOT_OVERLAP,
    which would send someone to fix a rota that is not the problem.
    """
    slots: list[TutorSlot] = []
    for tutor in sorted(tutors, key=lambda t: t.id):
        if tutor.free_cohort_seats <= 0:
            continue
        for window in sorted(
            tutor.availability, key=lambda w: (w.day_of_week, w.start_minute)
        ):
            zone = _zone(window.tz)
            if zone is None:
                continue
            start = window.start_minute
            while start + cfg.session_minutes <= window.end_minute:
                instant = first_occurrence_utc(
                    zone, cycle_date, window.day_of_week, start
                )
                slots.append(TutorSlot(
                    tutor_id=tutor.id,
                    start_utc=instant,
                    local_day_of_week=window.day_of_week,
                    local_start_minute=start,
                    tz=window.tz or "UTC",
                ))
                start += SLOT_GRANULARITY_MINUTES
    # Deterministic and de-duplicated: two availability rows may overlap.
    unique = {s.key: s for s in slots}
    return [unique[k] for k in sorted(unique)]


# ── Stage 1 — hard constraints as a feasibility graph ────────────────────────

def _student_fits_slot(
    student: StudentIn, slot: TutorSlot, tutor: TutorIn, cfg: MatchConfig
) -> str | None:
    """None if the edge exists, else the reason it does not.

    Timezone PROXIMITY is deliberately not tested here — it is a property of the
    group and belongs to Stage 2. Testing it pairwise is exactly what made the
    spec's Pass 1 ill-defined.
    """
    zone = _zone(student.tz)
    if zone is None:
        return REASON_NO_TIMEZONE
    if student.level is None:
        return REASON_NO_LEVEL

    # A commitment locks the home tutor and it is non-negotiable (spec §1.3,
    # platform plan §4.4). Not a score: a student who bought a term bought this.
    if student.locked_tutor_id and student.locked_tutor_id != tutor.id:
        return REASON_NO_SLOT_OVERLAP

    if not (tutor.min_level <= student.level <= tutor.max_level):
        return REASON_NO_TUTOR_FOR_LEVEL

    day, minute = _local_minutes(slot.start_utc, zone)
    end_minute = minute + cfg.session_minutes
    # Civil hour, both ends. Plan §3 Stage 0: "±2 hours is a proxy, not the
    # goal" — the offset band alone will happily schedule three people at 06:00.
    if minute < cfg.civil_start_minute or end_minute > cfg.civil_end_minute:
        return REASON_NO_SLOT_OVERLAP

    # Schedule overlap: the whole session must sit inside one declared window.
    # Intersecting rather than containing would place a student who is free for
    # the first ten minutes of their own lesson.
    for w in student.windows:
        if w.day_of_week == day and w.start_minute <= minute and end_minute <= w.end_minute:
            return None
    return REASON_NO_SLOT_OVERLAP


def _preference_rank(student: StudentIn, slot: TutorSlot, cfg: MatchConfig) -> int:
    """Which of the student's windows this slot lands in. 1 is a first choice."""
    zone = _zone(student.tz)
    if zone is None:
        return 1
    day, minute = _local_minutes(slot.start_utc, zone)
    end_minute = minute + cfg.session_minutes
    ranks = [
        w.rank for w in student.windows
        if w.day_of_week == day and w.start_minute <= minute and end_minute <= w.end_minute
    ]
    return min(ranks) if ranks else 1


# ── Stage 3 — score a cohort against a tutor ─────────────────────────────────

def _beginnerness(level: int) -> float:
    """1.0 at level 1, 0.0 at level 4."""
    return (4 - level) / 3.0


def member_fit(student: StudentIn, tutor: TutorIn, cfg: MatchConfig) -> dict[str, float]:
    """Plan §3 Stage 3's per-member terms.

    THE LAST TWO IMPLEMENT WHAT THE SPEC SAYS RATHER THAN WHAT IT TABULATES.
    L1 explanation is "useful for beginners", so its value decays as the student
    advances; and the spec explicitly wants experienced tutors weighted *lower*
    for advanced students "who benefit more from conversation partners". A flat
    +15/+10 says neither. Scaling by beginnerness says both, with one number.
    """
    w = cfg.weights
    b = _beginnerness(student.level or 1)
    out = {"goal": 0.0, "style": 0.0, "l1": 0.0, "experience": 0.0}

    if student.goal and student.goal in tutor.specialisations:
        out["goal"] = w["goal"]
    if student.style and tutor.teaching_style and student.style == tutor.teaching_style:
        out["style"] = w["style"]
    if (student.native_language and tutor.native_language
            and student.native_language == tutor.native_language):
        out["l1"] = w["l1"] * b
    if tutor.years_experience is not None:
        if tutor.years_experience >= cfg.experience_years:
            out["experience"] = w["experience"] * b
        else:
            out["experience"] = w["experience"] * (1.0 - b)
    return out


def score_candidate(
    members: list[StudentIn],
    slot: TutorSlot,
    tutor: TutorIn,
    cfg: MatchConfig,
    lesson_band: int,
    tz_band: float,
) -> tuple[int, dict[str, float]]:
    """clamp(mean(member_fit) + size + tightness + slot_pref + tier_cohesion, 0, 100).

    TIER COHESION IS INCLUDED EVEN THOUGH THE PLAN'S ONE-LINE FORMULA OMITS IT.
    §3 Stage 3's table defines it, weights it at 20 and argues for it at length
    ("a retention argument, and retention is worth real points"); the formula
    line beneath predates that paragraph. Building the line rather than the
    table would leave a documented, weighted, argued-for term scoring nothing.

    Clamping at 100 means the top of the range is reachable by several different
    good cohorts, which is intended: above ~100 the differences stop being
    meaningful and the tie-break order is a fairer discriminator than a
    fourth decimal place of fit.
    """
    w = cfg.weights
    fits = [member_fit(s, tutor, cfg) for s in members]
    mean_fit = sum(sum(f.values()) for f in fits) / len(fits)

    size = w["size_4"] if len(members) >= 4 else w["size_3"]

    lessons = [s.current_lesson for s in members]
    lesson_spread = max(lessons) - min(lessons)
    offsets = [
        _offset_hours(slot.start_utc, _zone(s.tz) or ZoneInfo("UTC")) for s in members
    ]
    tz_spread = max(offsets) - min(offsets)
    tightness = (
        w["tightness_lesson"] * (1.0 - (lesson_spread / lesson_band if lesson_band else 0))
        + w["tightness_tz"] * (1.0 - (tz_spread / tz_band if tz_band else 0))
    )

    # Honouring first choices beats honouring third. A student who expressed a
    # single rank has nothing to distinguish, and scores full marks rather than
    # dividing by zero.
    prefs = []
    for s in members:
        ranks = {win.rank for win in s.windows} or {1}
        span = max(ranks) - 1
        r = _preference_rank(s, slot, cfg)
        prefs.append(1.0 if span == 0 else 1.0 - (r - 1) / span)
    slot_pref = w["slot_pref"] * (sum(prefs) / len(prefs))

    tiers = [s.tier for s in members]
    modal_share = max(tiers.count(t) for t in set(tiers)) / len(tiers)
    cohesion = w["tier_cohesion"] * modal_share

    breakdown = {
        "goal": round(sum(f["goal"] for f in fits) / len(fits), 2),
        "style": round(sum(f["style"] for f in fits) / len(fits), 2),
        "l1": round(sum(f["l1"] for f in fits) / len(fits), 2),
        "experience": round(sum(f["experience"] for f in fits) / len(fits), 2),
        "size": round(size, 2),
        "tightness": round(tightness, 2),
        "slot_preference": round(slot_pref, 2),
        "tier_cohesion": round(cohesion, 2),
    }
    total = mean_fit + size + tightness + slot_pref + cohesion
    return max(0, min(100, int(round(total)))), breakdown


# ── Stage 2 — enumerate candidate cohorts ────────────────────────────────────

def _tie_key(student: StudentIn) -> tuple:
    """`(priority DESC, cycles_waited DESC, student_id ASC)` — plan §3 Stage 4.

    `priority` carries Intensive's guarantee; `cycles_waited` prevents the same
    person being deferred repeatedly, which is the fairness failure that would
    otherwise be invisible.
    """
    return (not student.priority, -student.cycles_waited, student.id)


def enumerate_candidates(
    pool: list[StudentIn],
    tutors: dict[str, TutorIn],
    slots: list[TutorSlot],
    cfg: MatchConfig,
    edges: dict[str, set[str]],
) -> list[Candidate]:
    """Per slot, per cadence class, per level: sort by lesson and slide a window
    of 4, then 3.

    WHY A SLIDING WINDOW IS SUFFICIENT RATHER THAN A SUBSET SEARCH. The
    lesson-spread constraint is an *interval* constraint, so any valid cohort is
    contiguous in lesson order; sorting by lesson makes every valid group a
    window, and the timezone test then filters. O(n) per slot per class per level
    instead of O(n^4), and it cannot miss a group the lesson constraint alone
    would allow.
    """
    by_id = {s.id: s for s in pool}
    candidates: list[Candidate] = []

    for slot in slots:
        tutor = tutors[slot.tutor_id]
        eligible = [by_id[sid] for sid in sorted(edges.get(_edge_key(slot), ()))]
        if not eligible:
            continue
        for cadence in ("A", "B"):
            in_class = [s for s in eligible if s.cadence_class == cadence]
            if len(in_class) < cfg.cohort_min:
                continue
            levels = sorted({s.level for s in in_class if s.level is not None})
            for level in levels:
                at_level = sorted(
                    (s for s in in_class if s.level == level),
                    key=lambda s: (s.current_lesson, s.id),
                )
                for size in range(cfg.cohort_max, cfg.cohort_min - 1, -1):
                    for i in range(0, len(at_level) - size + 1):
                        window = at_level[i:i + size]
                        # The loosest member's bands govern: a student on their
                        # third cycle is owed the relaxed band, and a group is
                        # labelled relaxed if any member's bands were widened.
                        bands = [cfg.bands_for(s.cycles_waited) for s in window]
                        tz_band = max(b[0] for b in bands)
                        lesson_band = max(b[1] for b in bands)
                        relaxed = (tz_band > cfg.tz_band_hours
                                   or lesson_band > cfg.lesson_band)

                        lessons = [s.current_lesson for s in window]
                        if max(lessons) - min(lessons) > lesson_band:
                            continue
                        offsets = [
                            _offset_hours(slot.start_utc, _zone(s.tz) or ZoneInfo("UTC"))
                            for s in window
                        ]
                        if max(offsets) - min(offsets) > tz_band:
                            continue

                        score, breakdown = score_candidate(
                            window, slot, tutor, cfg, lesson_band, tz_band
                        )
                        candidates.append(Candidate(
                            student_ids=tuple(s.id for s in window),
                            slot=slot,
                            cadence_class=cadence,
                            level=level,
                            score=score,
                            breakdown=breakdown,
                            lesson_low=min(lessons),
                            lesson_high=max(lessons),
                            bands_relaxed=relaxed,
                            claim=_claim(window),
                        ))
    return candidates


def _claim(members: list[StudentIn]) -> tuple:
    return tuple(sorted(_tie_key(s) for s in members))


def _candidate_order(c: Candidate) -> tuple:
    """Descending score, then size, then **claim**, then a total order on identity.

    THE CLAIM KEY IS LOAD-BEARING AND WAS MISSING FROM THE FIRST DRAFT.
    Plan §3 Stage 4 says every tie breaks on `(priority DESC, cycles_waited
    DESC, student_id ASC)`, and a tie between two *candidates* is exactly where
    that has to bite: five equally-scoring students competing for four seats
    produce two identical-scoring windows, and without this key the winner was
    whichever contained the alphabetically-first ids. An Intensive student on
    their third cycle was being passed over for a first-timer called `aaa`,
    which is the fairness failure `cycles_waited` exists to make impossible.

    The remaining keys are not decoration either: without them two candidates
    with the same score and claim would be ordered by construction order, and
    construction order depends on dict iteration — the difference between a
    matcher you can rerun and one you cannot.
    """
    return (-c.score, -c.size, c.claim, c.slot.tutor_id,
            c.slot.start_utc.isoformat(), c.student_ids)


# ── Stage 4 — assignment: set packing, then repair ───────────────────────────

def pack(
    candidates: list[Candidate],
    pool: list[StudentIn],
    cfg: MatchConfig,
    edges: dict[str, set[str]],
    tutors: dict[str, TutorIn],
) -> list[Candidate]:
    """Weighted set packing: no student twice, no TutorSlot twice.

    The objective is **placement first, quality second** —
    `maximise a*(students placed) + sum(score)` with `a >> 100`. At Tauka's
    scale a slightly worse-fitting cohort that *exists* beats an excellent one
    that leaves three people waiting a fortnight. Exact optimisation is
    unnecessary; the greedy seed plus three bounded repair passes is fast and
    good, and the same candidate set would feed an ILP without redesign if the
    pool ever outgrew it.
    """
    by_id = {s.id: s for s in pool}
    ordered = sorted(candidates, key=_candidate_order)

    used_students: set[str] = set()
    used_slots: set[str] = set()
    chosen: list[Candidate] = []

    # 1 — seed greedily by score.
    for c in ordered:
        slot_key = _edge_key(c.slot)
        if slot_key in used_slots:
            continue
        if used_students & set(c.student_ids):
            continue
        chosen.append(c)
        used_students |= set(c.student_ids)
        used_slots.add(slot_key)

    unplaced = [s for s in sorted(pool, key=_tie_key) if s.id not in used_students]

    # 2 — repair: GROW. Admit an unplaced student into a surviving 3-member
    # cohort. Cheap, and directly converts a deferral into a placement.
    for idx, c in enumerate(chosen):
        if c.size >= cfg.cohort_max:
            continue
        for student in list(unplaced):
            grown = _try_add(c, student, by_id, tutors, cfg, edges)
            if grown is not None:
                chosen[idx] = grown
                used_students.add(student.id)
                unplaced.remove(student)
                break

    # 3 — repair: EJECT. For each still-unplaced student, look for a cohort they
    # could join if one current member moved elsewhere. Applied only if it
    # increases students placed, or keeps it equal and raises total score.
    #
    # AN EJECTION MAY NEVER DISPLACE A STUDENT OF HIGHER PRIORITY OR LONGER
    # WAITING than the one being placed. Without that floor the repair pass
    # would quietly undo the fairness the tie-break order exists to create.
    for student in list(unplaced):
        if _try_eject(student, chosen, by_id, tutors, cfg, edges, used_students):
            unplaced.remove(student)

    # 4 — repair: OPEN. Any group of >= cohort_min unplaced students who satisfy
    # the bands together on an unconsumed slot opens a new cohort. This is the
    # pass that stops the greedy seed monopolising slots — and it is the one that
    # only ever fires on the thin, lopsided pools an early real cycle produces,
    # which is exactly why the dry run must include adversarial pools.
    if unplaced:
        remaining = [
            c for c in ordered
            if _edge_key(c.slot) not in used_slots
            and not (set(c.student_ids) & used_students)
        ]
        for c in remaining:
            slot_key = _edge_key(c.slot)
            if slot_key in used_slots or (set(c.student_ids) & used_students):
                continue
            chosen.append(c)
            used_students |= set(c.student_ids)
            used_slots.add(slot_key)

    return sorted(chosen, key=_candidate_order)


def _edge_key(slot: TutorSlot) -> str:
    return slot.key[1] + "|" + slot.tutor_id


def _try_add(
    c: Candidate,
    student: StudentIn,
    by_id: dict[str, StudentIn],
    tutors: dict[str, TutorIn],
    cfg: MatchConfig,
    edges: dict[str, set[str]],
) -> Candidate | None:
    """The grown cohort if `student` fits, else None. Every band is re-checked
    against the enlarged set: a member who fits three people need not fit four.
    """
    if student.id not in edges.get(_edge_key(c.slot), set()):
        return None
    if student.cadence_class != c.cadence_class or student.level != c.level:
        return None

    members = [by_id[i] for i in c.student_ids] + [student]
    bands = [cfg.bands_for(s.cycles_waited) for s in members]
    tz_band = max(b[0] for b in bands)
    lesson_band = max(b[1] for b in bands)

    lessons = [s.current_lesson for s in members]
    if max(lessons) - min(lessons) > lesson_band:
        return None
    offsets = [
        _offset_hours(c.slot.start_utc, _zone(s.tz) or ZoneInfo("UTC")) for s in members
    ]
    if max(offsets) - min(offsets) > tz_band:
        return None

    score, breakdown = score_candidate(
        members, c.slot, tutors[c.slot.tutor_id], cfg, lesson_band, tz_band
    )
    return Candidate(
        student_ids=tuple(sorted(s.id for s in members)),
        slot=c.slot,
        cadence_class=c.cadence_class,
        level=c.level,
        score=score,
        breakdown=breakdown,
        lesson_low=min(lessons),
        lesson_high=max(lessons),
        bands_relaxed=(tz_band > cfg.tz_band_hours or lesson_band > cfg.lesson_band),
        claim=_claim(members),
    )


def _try_eject(
    student: StudentIn,
    chosen: list[Candidate],
    by_id: dict[str, StudentIn],
    tutors: dict[str, TutorIn],
    cfg: MatchConfig,
    edges: dict[str, set[str]],
    used_students: set[str],
) -> bool:
    """Swap one member out and `student` in, if the swap does not lose a
    placement and does not displace someone with a stronger claim.

    Bounded to depth 1 — the ejected member must have somewhere to go *in the
    already-chosen set*, not at the end of a chain of further ejections. Depth 2
    was permitted by the plan; it is not built, because an unbounded-in-practice
    chain on a pool this size buys placements that the OPEN pass gets anyway,
    and the failure mode of a subtly wrong chain is a student silently moved out
    of a cohort they were told about.
    """
    for idx, c in enumerate(chosen):
        if student.id not in edges.get(_edge_key(c.slot), set()):
            continue
        if student.cadence_class != c.cadence_class or student.level != c.level:
            continue
        for victim_id in c.student_ids:
            victim = by_id[victim_id]
            # The fairness floor. `_tie_key` sorts stronger claims first, so a
            # victim ordering before the student is a stronger claim.
            if _tie_key(victim) < _tie_key(student):
                continue
            swapped = _swap(c, victim, student, by_id, tutors, cfg, edges)
            if swapped is None:
                continue
            # The victim must land somewhere, or this is not a repair — it is a
            # deferral with extra steps.
            for jdx, other in enumerate(chosen):
                if jdx == idx or other.size >= cfg.cohort_max:
                    continue
                regrown = _try_add(other, victim, by_id, tutors, cfg, edges)
                if regrown is None:
                    continue
                gain = (swapped.score + regrown.score) - (c.score + other.score)
                if gain < 0:
                    continue
                chosen[idx] = swapped
                chosen[jdx] = regrown
                used_students.add(student.id)
                return True
    return False


def _swap(
    c: Candidate,
    victim: StudentIn,
    incoming: StudentIn,
    by_id: dict[str, StudentIn],
    tutors: dict[str, TutorIn],
    cfg: MatchConfig,
    edges: dict[str, set[str]],
) -> Candidate | None:
    members = [by_id[i] for i in c.student_ids if i != victim.id] + [incoming]
    bands = [cfg.bands_for(s.cycles_waited) for s in members]
    tz_band = max(b[0] for b in bands)
    lesson_band = max(b[1] for b in bands)

    lessons = [s.current_lesson for s in members]
    if max(lessons) - min(lessons) > lesson_band:
        return None
    offsets = [
        _offset_hours(c.slot.start_utc, _zone(s.tz) or ZoneInfo("UTC")) for s in members
    ]
    if max(offsets) - min(offsets) > tz_band:
        return None

    score, breakdown = score_candidate(
        members, c.slot, tutors[c.slot.tutor_id], cfg, lesson_band, tz_band
    )
    return Candidate(
        student_ids=tuple(sorted(s.id for s in members)),
        slot=c.slot,
        cadence_class=c.cadence_class,
        level=c.level,
        score=score,
        breakdown=breakdown,
        lesson_low=min(lessons),
        lesson_high=max(lessons),
        bands_relaxed=(tz_band > cfg.tz_band_hours or lesson_band > cfg.lesson_band),
        claim=_claim(members),
    )


# ── The pipeline ─────────────────────────────────────────────────────────────

def run(
    pool: list[StudentIn],
    tutors: list[TutorIn],
    cfg: MatchConfig,
    cycle_date: date,
) -> MatchResult:
    """Stages 0-6 as one pure function. Writes nothing, reads nothing."""
    tutor_map = {t.id: t for t in tutors}
    slots = build_tutor_slots(tutors, cfg, cycle_date)

    deferrals: list[Deferral] = []
    ready: list[StudentIn] = []

    # Intake readiness first, so a student missing a timezone is told that
    # rather than told there was no tutor for their level.
    for s in sorted(pool, key=_tie_key):
        if s.cadence_class is None:
            # free_tier and anything unrecognised buys no live sessions. Not a
            # deferral: they are not in the market, and filing them would make
            # every count in the cycle wrong.
            continue
        if _zone(s.tz) is None:
            deferrals.append(Deferral(s.id, REASON_NO_TIMEZONE,
                                      {"field": "user_profiles.timezone"}, s.cycles_waited))
            continue
        if s.level is None:
            deferrals.append(Deferral(s.id, REASON_NO_LEVEL,
                                      {"field": "student.assessed_level"}, s.cycles_waited))
            continue
        active_windows = len(s.windows)
        if active_windows < cfg.min_preferences:
            deferrals.append(Deferral(
                s.id, REASON_TOO_FEW_PREFERENCES,
                {"have": active_windows, "need": cfg.min_preferences}, s.cycles_waited))
            continue
        ready.append(s)

    # Stage 1 — the feasibility graph, plus the precise reason for every student
    # who ends up with no edges at all.
    edges: dict[str, set[str]] = {}
    no_edge_reason: dict[str, tuple[str, dict]] = {}
    for s in ready:
        reasons: dict[str, int] = {}
        found = False
        for slot in slots:
            why = _student_fits_slot(s, slot, tutor_map[slot.tutor_id], cfg)
            if why is None:
                edges.setdefault(_edge_key(slot), set()).add(s.id)
                found = True
            else:
                reasons[why] = reasons.get(why, 0) + 1
        if not found:
            if not slots:
                # Every tutor is at their cohort limit, or none has availability.
                at_capacity = any(t.free_cohort_seats <= 0 for t in tutors)
                no_edge_reason[s.id] = (
                    REASON_TUTORS_AT_CAPACITY if at_capacity else REASON_NO_TUTOR_FOR_LEVEL,
                    {"tutor_slots": 0},
                )
            else:
                # The most common obstruction is the actionable one: a level with
                # no certified tutor is a hiring brief, a rota gap is a rota gap.
                dominant = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                no_edge_reason[s.id] = (dominant, {"slots_examined": len(slots),
                                                   "by_reason": reasons})

    # Referral is a HARD preference subject to capacity (spec §1.1, platform
    # plan §3.1): if the referring tutor has any feasible slot for this student,
    # the student is matched only within that tutor's slots. If they have none,
    # the preference silently yields rather than deferring someone over an
    # attribution that still pays out either way.
    if cfg.referral_preference == "hard":
        for s in ready:
            if not s.referral_tutor_id:
                continue
            theirs = {
                k for k, ids in edges.items()
                if s.id in ids and k.endswith("|" + s.referral_tutor_id)
            }
            if not theirs:
                continue
            for k, ids in edges.items():
                if k not in theirs:
                    ids.discard(s.id)

    candidates = enumerate_candidates(ready, tutor_map, slots, cfg, edges)
    cohorts = pack(candidates, ready, cfg, edges, tutor_map)

    placed = {sid for c in cohorts for sid in c.student_ids}

    # Stage 5 — offers, and the oversubscription problem.
    #
    # ALTERNATIVES ARE DRAWN FROM THE CHOSEN COHORTS, NOT FROM THE CANDIDATE
    # SET. An offer has to name a cohort that will exist — `match_offer.cohort_id`
    # is a foreign key — and only the packing's output becomes a row. Offering
    # rank 2 from the raw candidate list would be offering a seat in a group
    # that was never formed, which is precisely the "no longer available" the
    # soft hold exists to prevent.
    #
    # A cohort accepts at most `capacity + overbook_slack` holds, counting the
    # members it was packed with. That is what makes overbooking controlled
    # rather than a queue of disappointments.
    by_ready = {s.id: s for s in ready}
    offers: dict[str, list[Candidate]] = {}
    holds: dict[str, int] = {}
    cap = cfg.cohort_max + cfg.overbook_slack

    def _cohort_key(c: Candidate) -> str:
        return _edge_key(c.slot) + "#" + ",".join(c.student_ids)

    for c in cohorts:
        holds[_cohort_key(c)] = c.size
        for sid in c.student_ids:
            offers.setdefault(sid, []).append(c)

    # Strongest claims choose first, so a scarce second option goes to the
    # student the tie-break order says has waited longest.
    for sid in sorted(offers, key=lambda i: _tie_key(by_ready[i])):
        student = by_ready[sid]
        alternatives = sorted(
            (c for c in cohorts if sid not in c.student_ids),
            key=_candidate_order,
        )
        for alt in alternatives:
            if len(offers[sid]) >= cfg.offer_ranks:
                break
            key = _cohort_key(alt)
            if holds.get(key, 0) >= cap:
                continue
            # Feasible for THEM, checked the same way a grow is: a second option
            # that would break the bands is not an option.
            if _try_add(alt, student, by_ready, tutor_map, cfg, edges) is None:
                continue
            offers[sid].append(alt)
            holds[key] = holds.get(key, 0) + 1

    # Stage 6 — deferral is an output, not a failure.
    for s in ready:
        if s.id in placed:
            continue
        if s.id in no_edge_reason:
            reason, detail = no_edge_reason[s.id]
        else:
            # They had edges but no group formed around them. Which of the two
            # it is matters: too few peers is a marketing problem, a stranded
            # offset is a band problem.
            peers = [
                o for o in ready
                if o.id != s.id and o.level == s.level and o.cadence_class == s.cadence_class
            ]
            if len(peers) < cfg.cohort_min - 1:
                reason = REASON_TOO_FEW_PEERS
                detail = {"peers_at_level": len(peers),
                          "needed": cfg.cohort_min - 1 - len(peers),
                          "level": s.level, "cadence_class": s.cadence_class}
            else:
                reason = REASON_TZ_ISOLATE
                detail = {"peers_at_level": len(peers),
                          "tz": s.tz,
                          "tz_band_hours": cfg.bands_for(s.cycles_waited)[0]}
        deferrals.append(Deferral(
            s.id, reason, detail, s.cycles_waited,
            bands_relaxed=s.cycles_waited >= cfg.relax_after_cycles,
        ))

    stats = {
        "pool_size": len(pool),
        "eligible": len(ready),
        "tutor_slots": len(slots),
        "candidates": len(candidates),
        "cohorts_formed": len(cohorts),
        "students_placed": len(placed),
        "students_deferred": len(deferrals),
        "median_cycles_waited_by_tier": _median_wait_by_tier(pool),
    }
    return MatchResult(cohorts=cohorts, offers=offers, deferrals=deferrals, stats=stats)


def _median_wait_by_tier(pool: list[StudentIn]) -> dict[str, float]:
    """Plan §4: "track median `cycles_waited` per tier, not just overall, or this
    stays invisible behind a healthy-looking average."

    The prediction is that the tier paying most waits longest — Intensive is the
    smallest pool — which is the opposite of what anyone wants and is worth
    instrumenting from the first cycle rather than discovering in month six.
    """
    out: dict[str, float] = {}
    for tier in sorted({s.tier for s in pool}):
        waits = sorted(s.cycles_waited for s in pool if s.tier == tier)
        if not waits:
            continue
        mid = len(waits) // 2
        out[tier] = (
            float(waits[mid]) if len(waits) % 2
            else (waits[mid - 1] + waits[mid]) / 2.0
        )
    return out


def summarise(candidate: Candidate) -> str:
    """The natural-language line shown with an offer.

    A TEMPLATE OVER THE SCORE BREAKDOWN, NOT AN LLM CALL (plan §3 Stage 5). It
    must be instant, stable, and incapable of confidently misstating someone's
    schedule — three properties a generated sentence cannot guarantee and does
    not need to provide.
    """
    phrases = {
        "goal": "a good fit for what you want to use the language for",
        "style": "matches how you like to learn",
        "l1": "shares your first language",
        "experience": "the right level of experience for where you are",
        "size": "a full group",
        "tightness": "everyone is at almost exactly your point in the course",
        "slot_preference": "at one of your preferred times",
        "tier_cohesion": "everyone is on the same plan as you",
    }
    top = sorted(
        ((k, v) for k, v in candidate.breakdown.items() if v > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )[:2]
    if not top:
        return "A group at your level, meeting at a time you said you were free."
    return "This group is " + ", and ".join(phrases[k] for k, _ in top) + "."
