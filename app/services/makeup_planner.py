"""Make-up grouping, tutor and time — enrolment plan §2e/§2g, migrations §92/§93.

A PURE function, like the cohort matcher: everything it needs is passed in, so
it can be tested without a database and dry-run without side effects. Loading
and persisting live in `makeup_service`.

WHAT IT PROPOSES (an admin confirms; nothing is booked here)
  * A group of 1–3 learners of one course (15 / 20 / 30 minutes by head-count).
  * A TUTOR FROM EVERY MEMBER'S OWN POOL — the home tutor they are locked to or
    one of their guest (backup) tutors (`student_tutor_link`, active). A
    learner is never proposed a stranger.
  * A TIME that is inside the tutor's weekly availability, inside every
    member's preferred windows, clear of the tutor's other sessions, at least
    `LEAD` from now and finishing before the earliest member's deadline.

HOW IT CHOOSES
  * Learners join an existing proposed group first, when its tutor is in their
    pool and its time works for them at the longer length.
  * Otherwise, learner by learner in deadline order: for each tutor in their
    pool, build the largest group (≤3) that tutor can take at a common time.
    Prefer the bigger group, then the learner's home tutor, then the time that
    best honours everyone's ranked preferences, then the earliest.
  * A learner with no pool, or no time that works, still gets a group — with
    no proposal and a reason (`no_tutor_pool` / `no_common_time`) for the admin.
  * Each proposal reserves that tutor's time for the rest of the run, so two
    groups are never proposed the same tutor at the same moment.

TIME
  Every window is a weekly rule (day_of_week 0=Mon..6=Sun, start, end, IANA
  zone). Occurrences are resolved to real instants with zoneinfo, never a fixed
  offset, so a DST change between now and the session cannot move it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_GROUP = 3
MINUTES = {1: 15, 2: 20, 3: 30}
STEP = timedelta(minutes=15)
LEAD = timedelta(hours=24)          # never propose a session for tomorrow morning at 2am notice
NO_WINDOWS_RANK = 5                 # a learner with no preferences fits anywhere, scored mid-table


@dataclass(frozen=True)
class Window:
    day_of_week: int        # 0 = Monday
    start_min: int
    end_min: int            # may be 1440 (24:00)
    tz: str
    rank: int = 1


@dataclass(frozen=True)
class Waiting:
    student_id: str
    course_id: str | None
    funded_by: str                    # 'month' | 'credit'
    period_start: datetime | None
    due_by: datetime
    pool: tuple[str, ...] = ()        # home tutor first, then guests
    windows: tuple[Window, ...] = ()


@dataclass(frozen=True)
class OpenGroup:
    session_id: str
    course_id: str | None
    live: int
    due_by: datetime
    tutor_id: str | None = None       # the current proposal, if any
    starts_at: datetime | None = None
    members: tuple[Waiting, ...] = ()


@dataclass(frozen=True)
class Proposal:
    tutor_id: str | None
    starts_at: datetime | None
    note: str | None = None           # 'no_tutor_pool' | 'no_common_time'


@dataclass
class Plan:
    joins: list[tuple[str, Waiting, Proposal]] = field(default_factory=list)
    new_groups: list[tuple[list[Waiting], Proposal]] = field(default_factory=list)


def group_sizes(n: int) -> list[int]:
    """Fewest groups of at most MAX_GROUP, as even as possible (kept for the
    no-tutor fallback and for callers that only need sizes)."""
    if n <= 0:
        return []
    k = math.ceil(n / MAX_GROUP)
    base, extra = divmod(n, k)
    return [base + 1] * extra + [base] * (k - extra)


# ── time ────────────────────────────────────────────────────────────────────

def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def occurrences(w: Window, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Every concrete (start, end) instant of a weekly window between two instants."""
    zone = _zone(w.tz)
    out = []
    day: date = start.astimezone(zone).date() - timedelta(days=1)
    last: date = end.astimezone(zone).date()
    while day <= last:
        if day.weekday() == w.day_of_week:
            midnight = datetime.combine(day, time(0), tzinfo=zone)
            a = (midnight + timedelta(minutes=w.start_min)).astimezone(timezone.utc)
            b = (midnight + timedelta(minutes=w.end_min)).astimezone(timezone.utc)
            if b > start and a < end:
                out.append((a, b))
        day += timedelta(days=1)
    return out


def _learner_rank(l: Waiting, a: datetime, b: datetime) -> int | None:
    """The best rank of a learner window containing [a, b), or None."""
    if not l.windows:
        return NO_WINDOWS_RANK
    best = None
    for w in l.windows:
        for s, e in occurrences(w, a - timedelta(days=1), b + timedelta(days=1)):
            if s <= a and b <= e:
                best = w.rank if best is None else min(best, w.rank)
    return best


def find_slot(tutor_id: str, learners: list[Waiting], tutor_windows: dict[str, list[Window]],
              busy: dict[str, list[tuple[datetime, datetime]]], now: datetime
              ) -> tuple[int, datetime] | None:
    """(score, start) of the best time for this tutor and these learners."""
    minutes = MINUTES.get(len(learners))
    if not minutes:
        return None
    length = timedelta(minutes=minutes)
    earliest = now + LEAD
    latest_end = min(l.due_by for l in learners)
    best = None
    for w in tutor_windows.get(tutor_id, []):
        for ws, we in occurrences(w, earliest, latest_end):
            a = max(ws, _ceil_step(earliest))
            while a + length <= min(we, latest_end):
                b = a + length
                if not any(s < b and a < e for s, e in busy.get(tutor_id, [])):
                    ranks = [_learner_rank(l, a, b) for l in learners]
                    if all(r is not None for r in ranks):
                        cand = (sum(ranks), a)
                        if best is None or cand < best:
                            best = cand
                a += STEP
    return best


def _ceil_step(t: datetime) -> datetime:
    over = (t.minute % 15) * 60 + t.second
    return t if over == 0 and t.microsecond == 0 else \
        t.replace(second=0, microsecond=0) + timedelta(minutes=15 - t.minute % 15)


# ── grouping ────────────────────────────────────────────────────────────────

def plan_makeups(waiting: list[Waiting], open_groups: list[OpenGroup],
                 tutor_windows: dict[str, list[Window]] | None = None,
                 busy: dict[str, list[tuple[datetime, datetime]]] | None = None,
                 now: datetime | None = None) -> Plan:
    tutor_windows = tutor_windows or {}
    busy = {k: list(v) for k, v in (busy or {}).items()}
    now = now or datetime.now(timezone.utc)
    plan = Plan()

    # De-duplicate: one seat per learner per run.
    learners: list[Waiting] = []
    seen: set[str] = set()
    for w in sorted(waiting, key=lambda w: (w.due_by, w.student_id)):
        if w.student_id not in seen:
            seen.add(w.student_id)
            learners.append(w)
    placed: set[str] = set()

    # 1. Join existing proposed groups that have a tutor and time.
    groups = [g for g in sorted(open_groups, key=lambda g: g.due_by)
              if g.live < MAX_GROUP and g.tutor_id and g.starts_at]
    members = {g.session_id: list(g.members) for g in groups}
    current = {g.session_id: (g.tutor_id, g.starts_at, g.live) for g in groups}
    for l in learners:
        for g in groups:
            tutor, start, live = current[g.session_id]
            if live >= MAX_GROUP or g.course_id != l.course_id or tutor not in l.pool:
                continue
            new_len = timedelta(minutes=MINUTES[live + 1])
            end = start + new_len
            if end > min(l.due_by, g.due_by):
                continue
            # The tutor's own reservation for this group is not a clash.
            old_end = start + timedelta(minutes=MINUTES[live])
            others = [(s, e) for s, e in busy.get(tutor, []) if not (s == start and e == old_end)]
            if any(s < end and start < e for s, e in others):
                continue
            if any(_learner_rank(m, start, end) is None for m in members[g.session_id] + [l]):
                continue
            plan.joins.append((g.session_id, l, Proposal(tutor, start)))
            busy[tutor] = others + [(start, end)]
            current[g.session_id] = (tutor, start, live + 1)
            members[g.session_id].append(l)
            placed.add(l.student_id)
            break

    # 2. New groups, learner by learner, in deadline order.
    for l in learners:
        if l.student_id in placed:
            continue
        best = None   # (key, group, tutor, start)
        for tutor in l.pool:
            first = find_slot(tutor, [l], tutor_windows, busy, now)
            if first is None:
                continue
            group, slot = [l], first
            for m in learners:
                if len(group) == MAX_GROUP:
                    break
                if (m.student_id in placed or m is l or m.course_id != l.course_id
                        or tutor not in m.pool):
                    continue
                s = find_slot(tutor, group + [m], tutor_windows, busy, now)
                if s is not None:
                    group, slot = group + [m], s
            key = (len(group), tutor == l.pool[0], -slot[0], -slot[1].timestamp())
            if best is None or key > best[0]:
                best = (key, group, tutor, slot[1])

        if best is None:
            note = "no_tutor_pool" if not l.pool else "no_common_time"
            plan.new_groups.append(([l], Proposal(None, None, note)))
            placed.add(l.student_id)
            continue
        _, group, tutor, start = best
        busy.setdefault(tutor, []).append((start, start + timedelta(minutes=MINUTES[len(group)])))
        plan.new_groups.append((group, Proposal(tutor, start)))
        placed.update(m.student_id for m in group)

    return plan
