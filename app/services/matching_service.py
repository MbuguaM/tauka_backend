"""The home / guest tutor relation — plan Phase 2a, spec §1.2 / §2.1 / §3.2.

`app.student_tutor_link` (§73) replaced `app.student.tutor_id`, which said a
student has exactly one tutor. They have one **home** tutor and a **guest pool**,
with different rights and different lifetimes.

WHAT THIS MODULE IS, AND IS NOT.

It is **not the cohort matcher**. `tutor-cohort-matching-plan.md` designs that —
`intake_cycle`, `match_offer`, bucketed set-packing with repair — and none of it
exists yet: its Phases 1–4 have not been built, and none of its tables are in the
database. This plan's D-01 folded home/guest *into* that matcher, so the parts of
Phase 2a that read "Stage 1 of the matcher" have no Stage 1 to hook into.

What is buildable, and what this module is, are the **rules the matcher will have
to obey** — expressed as functions that can be enforced today by an admin
assigning links by hand, and called unchanged by the matcher when it lands:

- the referral hard preference (§1.1) — a `referral_signup` row decides the home
  tutor, subject to capacity, rather than adding a score;
- the concentration cap (§1.4) — checked in real time **on every assignment**,
  which the spec asks for explicitly and distinguishes from a periodic audit;
- the guest-pool rotation check (§1.2) — the rule that stops one guest being
  picked repeatedly, which is what makes a guest pool dilution rather than
  decoration;
- the minimum guest pool (§1.2).

THE SUPPLY PROBLEM, STATED ONCE.

**The platform has one tutor.** "At least two guests" cannot be satisfied by one
tutor, and a 60% concentration cap reads 100% on day one. Enforced hard, either
rule blocks every assignment that will ever be attempted. Plan §8 names this for
the cap; it is equally true of the guest pool. Both are therefore **advisory
below `match.min_tutor_supply` active tutors and binding above it** — reported
either way, so nothing is silently skipped.
"""

import logging
from datetime import datetime, timedelta, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# Defaults used when a config row is missing or unparseable. A bad config value
# must degrade to a sane rule, never to an exception on an assignment path.
_DEFAULTS = {
    "match.home_guest_ratio": "3:1",
    "match.min_guest_pool": "2",
    "match.guest_rotation_window_days": "90",
    "match.concentration_cap_pct": "60",
    "match.min_tutor_supply": "4",
}

LINK_ROLES = ("home", "guest")
LINK_SOURCES = ("referral", "matched", "admin", "handoff", "addon")   # addon: plan 2.6, E-15 (§129)


# ── Config ────────────────────────────────────────────────────────────────────

def _config(keys: list[str]) -> dict[str, str]:
    """Read config keys, falling back to the defaults above for anything absent.

    One round trip for the whole set: these are read together on every
    assignment, and four separate selects on a hot path is four chances to fail.
    """
    values = dict(_DEFAULTS)
    try:
        result = db.schema("app").table("config").select("key, value").in_("key", keys).execute()
        for row in result.data or []:
            if row.get("value") not in (None, ""):
                values[row["key"]] = row["value"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("matching config read failed, using defaults: %s", exc)
    return values


def _as_int(value: str | None, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def parse_ratio(raw: str | None) -> tuple[int, int]:
    """`"3:1"` → `(3, 1)`. Home sessions per guest session in the cohort rota.

    Mirrors the parsing inside `app.generate_cohort_sessions` deliberately: the
    rota is applied in SQL at generation time, and this side only ever needs to
    *explain* it. If the two ever disagree, the SQL is the one that ran.
    """
    try:
        home, guest = str(raw or "3:1").split(":", 1)
        h, g = int(home), int(guest)
        if h < 1 or g < 0:
            raise ValueError
        return h, g
    except (ValueError, AttributeError):
        return 3, 1


# ── Supply ────────────────────────────────────────────────────────────────────

def active_tutor_count() -> int:
    try:
        result = db.schema("app").table("tutor").select("id", count="exact").eq(
            "status", "active"
        ).is_("deleted_at", "null").execute()
        return result.count or len(result.data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor supply count failed: %s", exc)
        # Zero, not a guess. A failed count must make the supply-dependent rules
        # advisory rather than accidentally binding on a number we do not have.
        return 0


# ── Concentration cap (§1.4) ──────────────────────────────────────────────────

def concentration(tutor_id: str) -> dict:
    """What share of actively-linked students this tutor holds.

    Counts **assignments**, not activity — which is why §3.5 permits this to be
    enforced hard while forbidding a telemetry number from gating anything.
    """
    try:
        total = db.schema("app").table("student_tutor_link").select(
            "student_id"
        ).eq("state", "active").execute()
        rows = total.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("concentration read failed: %s", exc)
        return {"tutor_students": 0, "total_students": 0, "pct": 0.0}

    all_students = {r["student_id"] for r in rows}
    # Re-read scoped to the tutor rather than filtering `rows`, because `rows`
    # carries only student_id.
    try:
        mine = db.schema("app").table("student_tutor_link").select(
            "student_id"
        ).eq("state", "active").eq("tutor_id", tutor_id).execute()
        my_students = {r["student_id"] for r in mine.data or []}
    except Exception as exc:  # noqa: BLE001
        logger.warning("concentration read failed: %s", exc)
        my_students = set()

    total_n = len(all_students)
    return {
        "tutor_students": len(my_students),
        "total_students": total_n,
        "pct": round(100.0 * len(my_students) / total_n, 1) if total_n else 0.0,
    }


# ── Guest rotation (§1.2) ─────────────────────────────────────────────────────

def guest_rotation(student_id: str, window_days: int) -> dict:
    """How evenly a student's guest sessions have been spread across their pool.

    §1.2 asks for a check that "flags/blocks a booking pattern where a guest
    tutor is repeatedly selected over others in the guest pool (defeats the
    dilution purpose)". Two sources count as a guest session: a cohort occurrence
    the rota gave to a guest, and a 1-on-1 booked with one.

    **Advisory, always.** Blocking a booking because a student liked one guest is
    a support incident; the value here is that an admin can see the pattern.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

    pool = []
    try:
        links = db.schema("app").table("student_tutor_link").select(
            "tutor_id"
        ).eq("student_id", student_id).eq("role", "guest").eq("state", "active").execute()
        pool = sorted({r["tutor_id"] for r in links.data or []})
    except Exception as exc:  # noqa: BLE001
        logger.warning("guest pool read failed for %s: %s", student_id, exc)

    counts = {t: 0 for t in pool}
    try:
        booked = db.schema("app").table("one_to_one_session").select(
            "tutor_id"
        ).eq("student_id", student_id).gte("scheduled_at", since).in_(
            "status", ["booked", "completed"]
        ).execute()
        for row in booked.data or []:
            if row["tutor_id"] in counts:
                counts[row["tutor_id"]] += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("guest rotation read failed for %s: %s", student_id, exc)

    total = sum(counts.values())
    top = max(counts.values()) if counts else 0
    return {
        "pool_size": len(pool),
        "window_days": window_days,
        "sessions": total,
        "per_tutor": counts,
        # The share held by the most-used guest. With an even spread across a
        # pool of N this is 1/N; at 1.0 one guest has taken everything.
        "top_share": round(top / total, 2) if total else 0.0,
        # Not "is this bad" — "is this lopsided enough to look at". A pool of one
        # is never lopsided, it is just small, and saying otherwise would flag
        # every student on a thin platform.
        "skewed": bool(len(pool) > 1 and total >= 3 and (top / total) > 0.75),
    }


# ── Reading a roster ──────────────────────────────────────────────────────────

def _tutor_display(tutor_ids: list[str]) -> dict[str, dict]:
    """Name and avatar for a set of tutors, batched.

    **Read here rather than by the client.** `app.user_profiles` has no policy
    letting a student read their tutor's row, and adding one would be the wrong
    fix: RLS is row-level, so a policy granting SELECT hands over the whole row —
    `phone_number`, `location`, `onboarding_location` included. A student is
    entitled to their tutor's name and face, not their phone number. Serving the
    roster from here is what keeps those two things separable.
    """
    if not tutor_ids:
        return {}
    out: dict[str, dict] = {}
    try:
        profiles = db.schema("app").table("user_profiles").select(
            "id, first_name, last_name, avatar_url"
        ).in_("id", tutor_ids).execute()
        for p in profiles.data or []:
            out[p["id"]] = {
                "first_name": p.get("first_name"),
                "last_name": p.get("last_name"),
                "avatar_url": p.get("avatar_url"),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor display lookup failed: %s", exc)

    try:
        tutors = db.schema("app").table("tutor").select(
            "id, bio, language, rating"
        ).in_("id", tutor_ids).execute()
        for t in tutors.data or []:
            out.setdefault(t["id"], {}).update({
                "bio": t.get("bio"),
                "language": t.get("language"),
                "rating": t.get("rating"),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor record lookup failed: %s", exc)

    return out


async def get_roster(student_id: str) -> dict:
    """The student's tutors — spec §2.1's screen, as data.

    Home and guests come back in separate lists rather than one list with a role
    field, because §2.1 asks for "visually distinct treatment (home tutor should
    read as primary/persistent, guest tutors as supporting)" and a client that
    has to partition the list itself will eventually forget to.

    **`handoff_pending` is included, and it was not before (Phase 6).** This
    query filtered `state = 'active'` alone, so the moment a handoff began the
    student's home tutor disappeared from their own tutor screen and the roster
    read as "no home tutor". That is precisely the silent swap §1.7 exists to
    prevent, produced by the screen rather than by the state machine. A tutor in
    `handoff_pending` is still teaching — `ended_at` is NULL and the CHECK
    constraint enforces it — so they belong on the roster, flagged.
    """
    try:
        links = db.schema("app").table("student_tutor_link").select(
            "id, tutor_id, role, source, state, started_at"
        ).eq("student_id", student_id).in_(
            "state", ["active", "handoff_pending"]).order("started_at").execute()
        rows = links.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("roster read failed for %s: %s", student_id, exc)
        rows = []

    display = _tutor_display([r["tutor_id"] for r in rows])

    def shape(row: dict) -> dict:
        d = display.get(row["tutor_id"], {})
        return {
            "link_id": row["id"],
            "tutor_id": row["tutor_id"],
            "role": row["role"],
            "state": row["state"],
            # The client renders "your tutor is changing" from this rather than
            # comparing state strings itself. One flag, one meaning.
            "handing_off": row["state"] == "handoff_pending",
            "since": row["started_at"],
            "name": " ".join(p for p in (d.get("first_name"), d.get("last_name")) if p) or None,
            "avatar_url": d.get("avatar_url"),
            "bio": d.get("bio"),
            "language": d.get("language"),
        }

    homes = [r for r in rows if r["role"] == "home"]
    # The active home wins over a pending one during the warm-intro overlap,
    # when the student briefly has both. `handoff.outgoing` still names the
    # tutor who is leaving, so nobody vanishes — they stop being the headline.
    home_row = next((r for r in homes if r["state"] == "active"), None) or \
        next(iter(homes), None)
    home = shape(home_row) if home_row else None
    guests = [shape(r) for r in rows if r["role"] == "guest"]

    cfg = _config(list(_DEFAULTS))
    supply = active_tutor_count()
    min_guests = _as_int(cfg["match.min_guest_pool"], 2)

    return {
        "home": home,
        "guests": guests,
        # Told plainly rather than hidden. A student with one tutor on a
        # one-tutor platform should not be shown an empty "your guest tutors"
        # shelf with no explanation.
        "guest_pool_complete": len(guests) >= min_guests,
        "min_guest_pool": min_guests,
        "supply_limited": supply < _as_int(cfg["match.min_tutor_supply"], 4),
        # §1.7 / §2.5. Set whenever any home link is mid-transition, so the
        # tutor screen can route to "meet your new tutor" without a second call.
        "handoff_in_progress": any(r["state"] == "handoff_pending" for r in homes),
    }


# ── Assignment rules ──────────────────────────────────────────────────────────

def preferred_home_tutor(student_id: str) -> str | None:
    """§1.1's guaranteed first match, as a hard preference rather than a score.

    "When a `referral_signups` row exists for a new student, the assignment
    algorithm must treat that tutor as a **hard preference for home-tutor role**,
    subject only to a capacity check — not just a scoring boost."

    Returns the referring tutor, or None. **Capacity is deliberately not checked
    here**: this answers "who should it be", and `evaluate_assignment` answers
    "may it be", so a caller cannot accidentally get a yes/no when it asked a
    who.
    """
    try:
        result = db.schema("app").table("referral_signup").select(
            "tutor_id"
        ).eq("student_id", student_id).limit(1).execute()
        row = (result.data or [None])[0]
        if not row:
            return None
        tutor = db.schema("app").table("tutor").select("id, status, deleted_at").eq(
            "id", row["tutor_id"]
        ).limit(1).execute()
        t = (tutor.data or [None])[0]
        # A referral to a tutor who has since left is not a preference, it is a
        # dangling pointer. Silently ignoring it is right: the student keeps
        # their attribution (and the tutor keeps their revenue share) while being
        # matched to someone who can actually teach them.
        if not t or t.get("deleted_at") or t.get("status") != "active":
            return None
        return row["tutor_id"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral preference lookup failed for %s: %s", student_id, exc)
        return None


def locked_by_commitment(student_id: str) -> str | None:
    """The tutor an active termly commitment protects, or None.

    §1.3: *"the matching engine must treat this as a non-negotiable constraint —
    student is never reassigned off their home tutor mid-term except for
    tutor-side unavailability (see 1.7 warm handoff)."*

    Imported lazily: `commitment_service` is billing and this module is matching,
    and a module-level import would tie the two together at load time for a rule
    that fires on a minority of students.
    """
    try:
        from app.services.commitment_service import locked_tutor_for
        return locked_tutor_for(student_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("commitment lookup failed for %s: %s", student_id, exc)
        return None


def evaluate_assignment(student_id: str, tutor_id: str, role: str) -> dict:
    """May this link be created? Checked **on every assignment**, in real time.

    §1.4 asks for the concentration cap as "a check that runs on every new
    match/assignment, not just periodic auditing, to prevent breaching the cap in
    real time". This is that check.

    Returns `{allowed, blocking, advisories, concentration, supply}`. `blocking`
    is empty when the assignment may proceed; `advisories` are always populated
    and are the reason a thin platform is legible rather than merely permissive.
    """
    cfg = _config(list(_DEFAULTS))
    supply = active_tutor_count()
    min_supply = _as_int(cfg["match.min_tutor_supply"], 4)
    binding = supply >= min_supply

    conc = concentration(tutor_id)
    cap = _as_int(cfg["match.concentration_cap_pct"], 60)

    blocking: list[str] = []
    advisories: list[str] = []

    if role not in LINK_ROLES:
        blocking.append(f"role must be one of {LINK_ROLES}")

    # §1.3's guarantee, enforced. A student who bought a term bought "your slot
    # with [tutor] is protected for the term" — so a HOME assignment to anyone
    # else is refused outright, not scored down. This is the one blocking rule
    # here that is not supply-dependent: it is a contract, and a thin platform is
    # no reason to break one.
    #
    # A GUEST assignment is untouched. The promise is about who their home tutor
    # is; adding guests around them is exactly what §1.2 wants.
    locked = locked_by_commitment(student_id)
    if role == "home" and locked and locked != tutor_id:
        blocking.append(
            "this student has an active termly commitment locking their home "
            "tutor — reassignment needs a §1.7 handoff, not a rematch"
        )

    # Projected, not current: a cap checked against the share *before* the
    # assignment always passes the assignment that breaches it.
    projected_total = conc["total_students"]
    projected_mine = conc["tutor_students"]
    if not _already_linked(student_id, tutor_id):
        if not _has_any_active_link(student_id):
            projected_total += 1
        projected_mine += 1
    projected_pct = round(100.0 * projected_mine / projected_total, 1) if projected_total else 0.0

    if projected_pct > cap:
        message = (
            f"tutor would hold {projected_pct}% of linked students, over the "
            f"{cap}% concentration cap"
        )
        if binding:
            blocking.append(message)
        else:
            advisories.append(
                f"{message} — advisory only: {supply} active tutor(s), below the "
                f"{min_supply} needed for the cap to bind"
            )

    if not binding:
        advisories.append(
            f"supply-dependent rules are advisory: {supply} active tutor(s) < "
            f"match.min_tutor_supply={min_supply}"
        )

    return {
        "allowed": not blocking,
        "blocking": blocking,
        "advisories": advisories,
        "concentration": {**conc, "projected_pct": projected_pct, "cap_pct": cap},
        "supply": {"active_tutors": supply, "min_for_binding": min_supply, "binding": binding},
    }


def _already_linked(student_id: str, tutor_id: str) -> bool:
    try:
        r = db.schema("app").table("student_tutor_link").select("id").eq(
            "student_id", student_id
        ).eq("tutor_id", tutor_id).eq("state", "active").limit(1).execute()
        return bool(r.data)
    except Exception:  # noqa: BLE001
        return False


def _has_any_active_link(student_id: str) -> bool:
    try:
        r = db.schema("app").table("student_tutor_link").select("id").eq(
            "student_id", student_id
        ).eq("state", "active").limit(1).execute()
        return bool(r.data)
    except Exception:  # noqa: BLE001
        return False


async def create_link(
    student_id: str,
    tutor_id: str,
    role: str,
    source: str = "admin",
) -> dict:
    """Create one link, after `evaluate_assignment` allows it.

    A refusal comes back as `{created: False, blocking: [...]}` rather than an
    exception: the caller is an admin screen or the matcher, and both want to
    show the reason next to the student rather than catch and re-render it.
    """
    if source not in LINK_SOURCES:
        return {"created": False, "blocking": [f"source must be one of {LINK_SOURCES}"]}

    verdict = evaluate_assignment(student_id, tutor_id, role)
    if not verdict["allowed"]:
        return {"created": False, **verdict}

    if _already_linked(student_id, tutor_id):
        return {"created": False, "blocking": ["already linked"], **verdict}

    try:
        result = db.schema("app").table("student_tutor_link").insert({
            "student_id": student_id,
            "tutor_id": tutor_id,
            "role": role,
            "source": source,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        # The partial unique index on (student_id) WHERE role='home' AND
        # state='active' is the real guard against a second home tutor. Reaching
        # it means two callers raced; saying so beats a 500.
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return {"created": False, "blocking": ["this student already has an active home tutor"], **verdict}
        raise

    return {"created": True, "link": (result.data or [None])[0], **verdict}


def validate_link_set(student_id: str) -> dict:
    """Does this student's roster satisfy §1.2 — one home and at least N guests?

    **Enforced here and not in the schema, on purpose.** A CHECK cannot count
    sibling rows, and a trigger that refuses the first insert of a pair makes the
    table unwritable in the only order a caller can write it. So the rule lives
    where it can be evaluated *after* a set is assembled rather than during it.
    """
    cfg = _config(list(_DEFAULTS))
    min_guests = _as_int(cfg["match.min_guest_pool"], 2)
    supply = active_tutor_count()
    min_supply = _as_int(cfg["match.min_tutor_supply"], 4)
    binding = supply >= min_supply

    try:
        links = db.schema("app").table("student_tutor_link").select(
            "tutor_id, role, state"
        ).eq("student_id", student_id).in_(
            "state", ["active", "handoff_pending"]).execute()
        rows = links.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("link set read failed for %s: %s", student_id, exc)
        rows = []

    homes = [r for r in rows if r["role"] == "home" and r["state"] == "active"]
    handing_off = [r for r in rows
                   if r["role"] == "home" and r["state"] == "handoff_pending"]
    guests = [r for r in rows if r["role"] == "guest" and r["state"] == "active"]

    problems: list[str] = []
    if len(homes) == 0:
        # **"Mid-handoff" is not "unassigned" (Phase 6).** Before this branch
        # existed, beginning a handoff made the roster report "no active home
        # tutor" — a problem an admin would try to fix by assigning a home
        # tutor, which is the one thing that must not happen while a handoff is
        # pending. The state is reported, not counted as a fault.
        if handing_off:
            problems.append("home tutor is mid-handoff — complete or cancel it")
        else:
            problems.append("no active home tutor")
    if len(guests) < min_guests:
        problems.append(f"guest pool is {len(guests)}, minimum {min_guests}")

    return {
        "valid": not problems,
        # The distinction that matters on a one-tutor platform: the set is
        # incomplete, and it is incomplete because there is nobody to complete it
        # with, not because someone forgot.
        "enforced": binding,
        "problems": problems,
        "home_count": len(homes),
        "handoff_pending_count": len(handing_off),
        "guest_count": len(guests),
        "min_guest_pool": min_guests,
        "supply": {"active_tutors": supply, "min_for_binding": min_supply},
        "rotation": guest_rotation(
            student_id, _as_int(cfg["match.guest_rotation_window_days"], 90)
        ),
    }
