"""Enrolment domain, first slice: the waitlist and signup consents (§95, §97).

Enrolment plan Track H2 (waitlist v0) and H8 (consents v0). Flow:
`shared-features/flows/enrolment.md`. tauka-python owns both tables and is
their only writer; clients get SELECT under RLS and nothing else (§95b).

THE WAITLIST IS A PUBLIC FORM, SO IT IS BUILT TO TELL NOBODY ANYTHING.
`POST /waitlist` answers `{ok: true}` whether the email was new, already there,
or the write failed. A form that said "you're already on the list" would let
anyone test whether a given address had shown interest in Tauka.

DOUBLE OPT-IN (§97). Anyone can type anyone's address into a public form, so an
entry means nothing until the inbox owner clicks the link we mail them:
  * A new or still-unconfirmed address gets a confirmation email with a random
    token; only its sha256 is stored. Unconfirmed entries expire after 7 days.
  * Confirming sets `confirmed_at` and extends retention to 12 months. **Mail an
    entry only when `confirmed_at IS NOT NULL AND email_opt_in`.**
  * A CONFIRMED entry is never changed by the public form, because the form
    cannot tell its owner from a stranger. A re-submission is a silent no-op.
  * An unconfirmed entry merges (blank fields keep the old answer, an opt-in
    once given stays given) and gets a new link, at most once an hour, so the
    form cannot be used to flood someone's inbox.
  * The same token removes the entry ("not you?"), and keeps working after
    confirmation.
The links open pages on the website that POST the token, never a GET here:
mail scanners prefetch GET links, which would confirm or delete entries with
no human involved.

CONSENTS ARE ANSWERS, NOT SWITCHES. Recording `model_training = granted` here
does not make any existing recording usable for training: `train_ok` /
`consented_to_training` stay per row (L-007), and nothing reads these rows yet.
What this guarantees is that the question was asked, prospectively, at signup,
with the wording version on record.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# The wording the signup checkboxes showed. Bump it whenever the text in
# tauka-react-web's SignupConsents changes: a consent is per wording, and a
# learner who agreed to one sentence has not agreed to its replacement.
CONSENT_POLICY_VERSION = "2026-10-18"

# The SIGNUP purposes (each answered, ticked or not, at signup). The CHECK also
# allows 'tutor_voice_review' (§107), asked in the app at a learner's first Fidel
# reading (fidel_service) — deliberately not a signup checkbox.
CONSENT_PURPOSES = ("session_recording", "pronunciation_profile", "model_training")

WAITLIST_RETENTION = timedelta(days=365)
UNCONFIRMED_RETENTION = timedelta(days=7)
RESEND_COOLDOWN = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _waitlist():
    return db.schema("app").table("waitlist_entry")


# ── Waitlist ──────────────────────────────────────────────────────────────────

_FIELDS = "id, confirmed_at, confirm_sent_at, email_opt_in"


def join_waitlist(
    *,
    email: str,
    goal: str | None = None,
    band: str | None = None,
    pace_hours: int | None = None,
    email_opt_in: bool = False,
    source: str | None = None,
    child_age_band: str | None = None,
    banner_arm: str | None = None,
) -> str | None:
    """Add or refresh an UNCONFIRMED entry. Never raises.

    `child_age_band` (plan 1.8, behind the family_waitlist flag): the entry is a
    parent's, for a child of that age. Nothing else about the child is taken.

    Returns the plaintext confirmation token when a confirmation email should go
    out now, else None (confirmed already, inside the resend cooldown, or a
    failed write). The caller sends the mail in the background, so every
    outcome takes the same time to answer.
    """
    email = email.strip().lower()
    now = _now()
    token = secrets.token_urlsafe(32)
    fresh = {
        "confirm_token_hash": _hash(token),
        "confirm_sent_at": now.isoformat(),
        "delete_after": (now + UNCONFIRMED_RETENTION).isoformat(),
    }

    try:
        existing = (_waitlist().select(_FIELDS).eq("email", email).limit(1).execute()).data or []

        if not existing:
            try:
                _waitlist().insert({
                    "email": email,
                    "goal": goal,
                    "band": band,
                    "pace_hours": pace_hours,
                    "email_opt_in": email_opt_in,
                    "source": source,
                    # Sent only when set, so the plain waitlist works before §131 is applied.
                    **({"child_age_band": child_age_band} if child_age_band else {}),
                    **({"banner_arm": banner_arm} if banner_arm else {}),
                    **fresh,
                }).execute()
                return token
            except Exception:  # noqa: BLE001
                # Lost a race with a concurrent submission of the same address.
                # Fall through and treat the row that won as existing.
                existing = (_waitlist().select(_FIELDS).eq("email", email).limit(1).execute()).data or []
                if not existing:
                    raise

        row = existing[0]
        if row.get("confirmed_at"):
            # The form can't tell the owner from a stranger, so it may not edit
            # a confirmed entry. Nothing is written and nothing is mailed.
            return None

        update: dict = {"email_opt_in": bool(row.get("email_opt_in")) or email_opt_in}
        for key, value in (("goal", goal), ("band", band), ("pace_hours", pace_hours),
                           ("child_age_band", child_age_band)):
            if value is not None:
                update[key] = value

        last_sent = _parse(row.get("confirm_sent_at"))
        resend = last_sent is None or now - last_sent >= RESEND_COOLDOWN
        if resend:
            update.update(fresh)  # a new token; the old link stops working
        _waitlist().update(update).eq("id", row["id"]).execute()
        return token if resend else None
    except Exception as exc:  # noqa: BLE001
        # Logged without the address: a waitlist email is personal data from
        # someone with no account and no privacy relationship with us yet.
        logger.error("join_waitlist failed: %s", exc)
        return None


async def send_confirmation(email: str, token: str) -> None:
    """The double opt-in email. Best-effort, like every send in this service."""
    from app.services.email_service import send_template_email

    # send_template_email never raises and logs its own failures.
    await send_template_email(
        email.strip().lower(),
        "waitlist_confirm",
        {"subject": "Confirm your place on the Tauka waitlist", "token": token},
        tags=["waitlist_confirm"],
    )


def confirm_waitlist(token: str) -> bool:
    """Confirm the entry this token was mailed for. Idempotent. True if valid."""
    try:
        rows = (
            _waitlist().select("id, confirmed_at")
            .eq("confirm_token_hash", _hash(token)).limit(1).execute()
        ).data or []
        if not rows:
            return False
        if not rows[0].get("confirmed_at"):
            now = _now()
            _waitlist().update({
                "confirmed_at": now.isoformat(),
                "delete_after": (now + WAITLIST_RETENTION).isoformat(),
            }).eq("id", rows[0]["id"]).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("confirm_waitlist failed: %s", exc)
        return False


def remove_waitlist(token: str) -> bool:
    """Delete the entry this token was mailed for ("not you?" / "remove me")."""
    try:
        result = _waitlist().delete().eq("confirm_token_hash", _hash(token)).execute()
        return bool(result.data)
    except Exception as exc:  # noqa: BLE001
        logger.error("remove_waitlist failed: %s", exc)
        return False


def link_waitlist_conversion(student_id: str) -> bool:
    """Mark the waitlist entry with this account's email as converted.

    Called once, when provisioning creates the `app.student` row. Non-fatal:
    a missed link only means the entry is swept like any other.
    """
    try:
        user = db.auth.admin.get_user_by_id(student_id).user
        email = (getattr(user, "email", None) or "").strip().lower()
        if not email:
            return False
        result = (
            _waitlist()
            .update({"converted_student_id": student_id})
            .eq("email", email)
            .is_("converted_student_id", "null")
            .execute()
        )
        return bool(result.data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("link_waitlist_conversion failed for %s: %s", student_id, exc)
        return False


def sweep_waitlist() -> int:
    """Delete unconverted entries past `delete_after` (7 days unconfirmed,
    12 months confirmed). Returns the count."""
    result = (
        _waitlist()
        .delete()
        .lt("delete_after", _now().isoformat())
        .is_("converted_student_id", "null")
        .execute()
    )
    return len(result.data or [])


# ── Consents ──────────────────────────────────────────────────────────────────

def record_signup_consents(subject_id: str, consents: dict[str, bool]) -> int:
    """Append one row per purpose (granted or not) for the current wording.

    Idempotent per (subject, purpose, policy version): `/auth/provision` runs
    on every sign-in until it succeeds, and the partial unique index
    `idx_consent_record_one_signup_answer` backs this check up under a race.
    A purpose the client did not send is recorded as NOT granted, because an
    unticked box and a missing one mean the same thing. Returns rows written.
    """
    table = lambda: db.schema("app").table("consent_record")  # noqa: E731

    already = {
        r["purpose"]
        for r in (
            table().select("purpose")
            .eq("subject_id", subject_id)
            .eq("policy_version", CONSENT_POLICY_VERSION)
            .eq("source", "signup")
            .execute()
        ).data or []
    }
    rows = [
        {
            "subject_id": subject_id,
            "purpose": purpose,
            "granted": bool(consents.get(purpose, False)),
            "policy_version": CONSENT_POLICY_VERSION,
            "source": "signup",
        }
        for purpose in CONSENT_PURPOSES
        if purpose not in already
    ]
    if rows:
        table().insert(rows).execute()
    return len(rows)
