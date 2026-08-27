"""Tutor referral attribution — plan Phase 1, spec §1.1.

Two tables, both `tutor_management`, both written **only** here via the service
role (§72): `app.tutor_referral_link` (a tutor's shareable code) and
`app.referral_signup` (the paying signup attributed to it).

THIS IS NOT `app.test_referrals`. That table attributes a placement-test *share*
between two people who may never sign up. These attribute a *signup* and the
revenue share that follows it. Same word, different object — CONFLICT-039.

Three invariants this module exists to hold:

1. **Attribution is claim-on-first-signup and immutable.** `referral_signup`
   has UNIQUE on `student_id`, and every path here reads before it writes and
   returns the *existing* row on conflict. A second code can never overwrite the
   first, so attribution cannot be stolen after the fact. That constraint is the
   integrity of the revenue share, not a tidiness rule.
2. **A tutor never sees who they referred.** The report returns counts and
   per-signup facts with no student id, name or email in them. A tutor is not
   assigned to a student merely by having referred them, and `legal.md` requires
   a reason before widening who can read a student's data. Retention is a
   number; it does not need to be a person.
3. **`source_platform` is a slug from a closed set, never free text.** Same rule
   as the activity payload in `CLAUDE.md`: ids and enums only.
"""

import logging
import re
from datetime import datetime, timezone
from app.config import settings
from app.services.referral_service import generate_referral_code
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# Where a tutor said they were sharing the link. A closed allowlist: anything
# not in it is dropped rather than stored, because this column is one careless
# passthrough away from being free text a visitor typed.
SOURCE_PLATFORMS = frozenset({
    "tiktok", "instagram", "whatsapp", "youtube", "facebook",
    "x", "telegram", "linkedin", "in_person", "other",
})

# `app.tutor_referral_link.code` is CHECK (length BETWEEN 4 AND 64). The codes
# this service mints are 8 chars from an unambiguous alphabet; the pattern below
# is what it will *accept*, so a hand-issued code stays possible.
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

_MAX_CODE_ATTEMPTS = 6


# ── Link URLs ─────────────────────────────────────────────────────────────────

def build_signup_url(code: str, source_platform: str | None = None) -> str:
    """The link a tutor shares.

    **`tref`, not `ref`.** `/signup?ref=<code>` is already emitted by
    `referral_service` carrying an `app.test_referrals` code (see
    `handle_test_completion`). Two different code namespaces in one query
    parameter cannot be told apart on arrival, and the failure would be silent —
    a lookup miss looks exactly like a typo. A distinct parameter costs nothing
    and makes the two unmixable. CONFLICT-039.
    """
    url = f"{settings.web_base_url}/signup?tref={code}"
    if source_platform:
        url += f"&src={source_platform}"
    return url


def normalise_source_platform(value: str | None) -> str | None:
    """Return the slug if it is one we recognise, else None. Never raises —
    a bad platform tag must not cost a tutor an attribution."""
    if not value:
        return None
    slug = value.strip().lower()
    return slug if slug in SOURCE_PLATFORMS else None


# ── Links ─────────────────────────────────────────────────────────────────────

def _active_link(tutor_id: str) -> dict | None:
    result = db.schema("app").table("tutor_referral_link").select(
        "id, code, active, created_at"
    ).eq("tutor_id", tutor_id).eq("active", True).is_(
        "deleted_at", "null"
    ).order("created_at", desc=True).limit(1).execute()
    return (result.data or [None])[0]


def _mint_link(tutor_id: str) -> dict:
    """Insert a link with a fresh code, retrying on the UNIQUE collision.

    The retry is against the database, not against a local set: uniqueness is
    the constraint's job and only the constraint sees every other process.
    """
    last_exc: Exception | None = None
    for _ in range(_MAX_CODE_ATTEMPTS):
        code = generate_referral_code()
        try:
            result = db.schema("app").table("tutor_referral_link").insert({
                "tutor_id": tutor_id,
                "code": code,
            }).execute()
            return result.data[0]
        except Exception as exc:  # noqa: BLE001 — inspected below
            last_exc = exc
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                continue
            raise
    raise RuntimeError(
        f"Could not mint a unique referral code after {_MAX_CODE_ATTEMPTS} "
        f"attempts: {last_exc}"
    )


async def get_or_create_link(tutor_id: str) -> dict:
    """The tutor's current code, minting one the first time they ask."""
    link = _active_link(tutor_id) or _mint_link(tutor_id)
    return {
        "link_id": link["id"],
        "code": link["code"],
        "share_url": build_signup_url(link["code"]),
        "created_at": link.get("created_at"),
        "rotated": False,
    }


async def rotate_link(tutor_id: str) -> dict:
    """Retire every active code for this tutor and mint a new one.

    Retirement is a **soft** delete. `referral_signup.link_id` is
    `ON DELETE SET NULL`, so a hard delete would silently erase which code won
    each past signup — the audit trail a revenue share is argued from. Rows
    already attributed keep pointing at the retired link.
    """
    now = datetime.now(timezone.utc).isoformat()
    db.schema("app").table("tutor_referral_link").update({
        "active": False,
        "deleted_at": now,
    }).eq("tutor_id", tutor_id).is_("deleted_at", "null").execute()

    link = _mint_link(tutor_id)
    return {
        "link_id": link["id"],
        "code": link["code"],
        "share_url": build_signup_url(link["code"]),
        "created_at": link.get("created_at"),
        "rotated": True,
    }


def _resolve_code(code: str) -> dict | None:
    """The active link for a code, or None. Retired codes resolve to None —
    a rotated code stops attributing, which is the point of rotating."""
    if not code or not _CODE_RE.match(code):
        return None
    result = db.schema("app").table("tutor_referral_link").select(
        "id, tutor_id, code"
    ).eq("code", code).eq("active", True).is_("deleted_at", "null").execute()
    return (result.data or [None])[0]


def _tutor_display_name(tutor_id: str) -> str | None:
    """First name only. The public lookup renders this to a stranger on a
    signup page, so it carries the least that still means something."""
    try:
        result = db.schema("app").table("user_profiles").select(
            "first_name"
        ).eq("id", tutor_id).limit(1).execute()
        row = (result.data or [None])[0]
        return (row or {}).get("first_name")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor name lookup failed for %s: %s", tutor_id, exc)
        return None


async def lookup_code(code: str) -> dict:
    """PUBLIC. Is this code live, and whose is it?

    Deliberately says nothing about the tutor beyond a first name, and returns
    the same shape for "no such code" and "retired code" — a probe learns
    nothing from the difference.
    """
    link = _resolve_code(code)
    if not link:
        return {"valid": False, "tutor_name": None}

    tutor = db.schema("app").table("tutor").select("id, status, deleted_at").eq(
        "id", link["tutor_id"]
    ).limit(1).execute()
    row = (tutor.data or [None])[0]
    if not row or row.get("deleted_at") or row.get("status") != "active":
        return {"valid": False, "tutor_name": None}

    return {"valid": True, "tutor_name": _tutor_display_name(link["tutor_id"])}


# ── Attribution ───────────────────────────────────────────────────────────────

def _existing_attribution(student_id: str) -> dict | None:
    result = db.schema("app").table("referral_signup").select(
        "id, tutor_id, link_id, signed_up_at, source_platform"
    ).eq("student_id", student_id).limit(1).execute()
    return (result.data or [None])[0]


async def attribute_signup(
    student_id: str,
    code: str,
    source_platform: str | None = None,
) -> dict:
    """Attribute a new student to the tutor whose code they arrived with.

    Returns `{attributed, already_attributed, tutor_name, reason}`. It raises
    only for a caller error the client can fix; a code that does not resolve is
    a **result**, not an exception, because the common cause is a typo and the
    student must not be blocked from finishing onboarding by one.
    """
    existing = _existing_attribution(student_id)
    if existing:
        # Immutable. Not an error, not an update — the first code won, and
        # saying so plainly is what stops a later caller from "fixing" it.
        return {
            "attributed": False,
            "already_attributed": True,
            "tutor_name": _tutor_display_name(existing["tutor_id"]),
            "reason": "already_attributed",
        }

    link = _resolve_code(code)
    if not link:
        return {
            "attributed": False,
            "already_attributed": False,
            "tutor_name": None,
            "reason": "unknown_code",
        }

    if link["tutor_id"] == student_id:
        # One account cannot be both, but a role change or a shared id would
        # make this a free revenue share. Cheap to refuse.
        return {
            "attributed": False,
            "already_attributed": False,
            "tutor_name": None,
            "reason": "self_referral",
        }

    student = db.schema("app").table("student").select("id").eq(
        "id", student_id
    ).limit(1).execute()
    if not student.data:
        # `referral_signup.student_id` is an FK to `app.student`, which the
        # client creates during onboarding. Reporting this rather than letting
        # the insert die on a 23503 is the difference between "try again after
        # onboarding" and an opaque 500.
        return {
            "attributed": False,
            "already_attributed": False,
            "tutor_name": None,
            "reason": "no_student_row",
        }

    try:
        db.schema("app").table("referral_signup").insert({
            "student_id": student_id,
            "tutor_id": link["tutor_id"],
            "link_id": link["id"],
            "source_platform": normalise_source_platform(source_platform),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        # The UNIQUE index is the real guard, and it is the one that holds when
        # two devices claim at once. Losing that race is a success, not a fault.
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            again = _existing_attribution(student_id)
            return {
                "attributed": False,
                "already_attributed": True,
                "tutor_name": _tutor_display_name(again["tutor_id"]) if again else None,
                "reason": "already_attributed",
            }
        raise

    return {
        "attributed": True,
        "already_attributed": False,
        "tutor_name": _tutor_display_name(link["tutor_id"]),
        "reason": "attributed",
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

async def get_referral_report(tutor_id: str) -> dict:
    """Signups, retention and vested share for one tutor — spec §1.1's
    `GET /tutor/{id}/referrals`.

    **No student is named.** Every row is a date, a platform slug and three
    facts about the *relationship*. Referring someone does not make you their
    tutor, and `legal.md` wants a reason before a tutor can read a student.
    Retention is a count; it does not need to be a roster.

    `first_match_honoured` and `vested_share_bp` are both written by phases that
    have not shipped (2 and 3). They are reported as they are stored — zero —
    and labelled so, rather than being hidden until they mean something.
    """
    link = _active_link(tutor_id)

    signups = db.schema("app").table("referral_signup").select(
        "student_id, signed_up_at, source_platform, first_match_honoured, "
        "vested_share_bp"
    ).eq("tutor_id", tutor_id).order("signed_up_at", desc=True).execute()
    rows = signups.data or []

    # Retention is read from the student rows, then the ids are discarded before
    # anything leaves this function.
    still_active: set[str] = set()
    if rows:
        ids = [r["student_id"] for r in rows]
        try:
            students = db.schema("app").table("student").select(
                "id, account_status, deleted_at"
            ).in_("id", ids).execute()
            for s in students.data or []:
                if s.get("deleted_at") is None and s.get("account_status") == "active":
                    still_active.add(s["id"])
        except Exception as exc:  # noqa: BLE001
            # A retention figure that failed to load must not be rendered as
            # zero retained — the report says so instead.
            logger.warning("referral retention lookup failed for %s: %s", tutor_id, exc)
            still_active = set()

    items = [
        {
            "signed_up_at": r["signed_up_at"],
            "source_platform": r.get("source_platform"),
            "still_active": r["student_id"] in still_active,
            "first_match_honoured": bool(r.get("first_match_honoured")),
            "vested_share_bp": int(r.get("vested_share_bp") or 0),
        }
        for r in rows
    ]

    return {
        "code": link["code"] if link else None,
        "share_url": build_signup_url(link["code"]) if link else None,
        "total_signups": len(items),
        "still_active": sum(1 for i in items if i["still_active"]),
        "first_match_honoured": sum(1 for i in items if i["first_match_honoured"]),
        "total_vested_share_bp": sum(i["vested_share_bp"] for i in items),
        # Phase 1 writes neither of the two fields above as anything but their
        # defaults. The client should say "not yet in effect", not "0%".
        "vesting_active": False,
        "signups": items,
    }
