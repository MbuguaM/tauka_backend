"""Account provisioning — the rows that turn an `auth.users` row into an account.

**Signup moved to tauka-react-web on 2026-08-28 (developer decision).** The
Flutter clients sign in only; a visitor without an account is sent to the
website. This module is the half of that change that lives in tauka-python.

WHY THE BACKEND OWNS THIS AND NOT THE WEB.

`app.user_profiles` and `app.student` are tauka-flutter's tables. Root
`CLAUDE.md` forbids tauka-react-web writing either, and `app.student` carries the
Stripe columns and `tier`, which only tauka-python may ever write. Giving the web
INSERT policies on both would have put profile creation next to the one column
the web must never touch. So the web creates the *auth* user through Supabase —
where the password stays between the browser and Supabase and never reaches us —
and then asks this service to create the rows, with the service role.

WHY IT IS IDEMPOTENT AND AUTHENTICATED RATHER THAN PUBLIC.

Email confirmation is enabled on this project, so `supabase.auth.signUp()` in the
browser does not reliably return a session: the user may confirm by mail minutes
later and arrive back signed in. A public endpoint taking an email would have to
trust the caller about who they are. Instead this runs off a verified JWT, and it
is safe to call repeatedly — the web calls it after signup when a session
exists, and again on first sign-in when one did not.

THE ROLE IS NOT A PARAMETER. `user_type` is hardcoded to `'student'`. The
decision on record is that tutors are created by an admin, because tutor vetting,
the agreement (O-03/O-04) and payout onboarding all assume a human approved
them. Accepting a caller-supplied role on an endpoint any signed-in user can
reach would be self-promotion into the tutor role, and `user_type = 'tutor'` is
read by RLS (`db.sql:720`) as well as by the UI.

`app.student.tier` IS NEVER WRITTEN HERE. The column defaults to `'free_tier'`
and tauka-python's billing path owns every subsequent value. That default was
itself broken until §81.1 the same morning — it was `'free'`, a value its own
CHECK rejects, so every profile INSERT failed and no account could be created at
all (CONFLICT-045). This module could not have worked before that fix.
"""

import logging
from datetime import datetime, timezone

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# The only role this endpoint will ever write. See the module docstring.
_STUDENT = "student"


def _profile(user_id: str) -> dict | None:
    result = (
        db.schema("app").table("user_profiles")
        .select("id, user_type, first_name, age_confirmed_at, completed_at, learning_goal")
        .eq("id", user_id).limit(1).execute()
    )
    return (result.data or [None])[0]


def _student_row_exists(user_id: str) -> bool:
    result = (
        db.schema("app").table("student")
        .select("id").eq("id", user_id).limit(1).execute()
    )
    return bool(result.data)


async def provision(
    user_id: str,
    *,
    first_name: str | None,
    age_confirmed: bool,
    referral_code: str | None = None,
    source_platform: str | None = None,
    consents: dict[str, bool] | None = None,
    learning_goal: str | None = None,
    banner_arm: str | None = None,
    partner_code: str | None = None,
) -> dict:
    """Create the profile and student rows for an already-authenticated user.

    Returns a dict describing what it found and what it did. Nothing here raises
    for a state the caller cannot fix: an account that already exists is a
    successful no-op, not an error, because the web calls this again on every
    sign-in until a profile is present.
    """
    existing = _profile(user_id)

    if existing is None:
        # `first_name` is nullable in production (the declaration claimed NOT
        # NULL until §81.5 corrected it), so a missing name is not a reason to
        # refuse an account.
        payload: dict = {"id": user_id, "user_type": _STUDENT}
        if first_name:
            payload["first_name"] = first_name.strip()[:120]
        if age_confirmed:
            # L-005. This is the first time the attestation is EVIDENCE rather
            # than notice: it is written here because this is now where the
            # account is created. The web's checkbox recorded nothing while the
            # web created nothing, which was correct then and is not now.
            payload["age_confirmed_at"] = datetime.now(timezone.utc).isoformat()
        if learning_goal:
            # Enrolment plan 1.7: the placement result's goal (closed vocabulary,
            # validated by the request model).
            payload["learning_goal"] = learning_goal

        try:
            db.schema("app").table("user_profiles").insert(payload).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("provision: user_profiles insert failed for %s: %s", user_id, exc)
            raise

        created_profile = True
    else:
        created_profile = False
        # Backfill the attestation for an account provisioned before the
        # checkbox existed, but never clear one that is already set.
        if age_confirmed and not existing.get("age_confirmed_at"):
            try:
                db.schema("app").table("user_profiles").update(
                    {"age_confirmed_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", user_id).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning("provision: age backfill failed for %s: %s", user_id, exc)
        # Plan 1.7: a goal from the placement result fills an empty one, never
        # overwrites one the learner chose in the app.
        if learning_goal and not existing.get("learning_goal"):
            try:
                db.schema("app").table("user_profiles").update({"learning_goal": learning_goal}) \
                    .eq("id", user_id).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning("provision: goal backfill failed for %s: %s", user_id, exc)

    # Plan 7.10 (§136): the banner arm, once per account. Never fatal.
    if banner_arm in ("dated", "static"):
        try:
            db.schema("app").table("acquisition_arm").insert({"user_id": user_id, "arm": banner_arm}).execute()
        except Exception as exc:  # noqa: BLE001
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                logger.warning("provision: banner arm not recorded for %s: %s", user_id, exc)

    # `app.student` is separate and separately idempotent: a profile can exist
    # without one if an earlier provision was interrupted between the two
    # writes. Checking rather than upserting keeps this readable in the logs.
    created_student = False
    if not _student_row_exists(user_id):
        try:
            # NO `tier` KEY. The column default is the only thing that should
            # ever set an initial tier, and tauka-python's billing path owns it
            # from then on.
            db.schema("app").table("student").insert({"id": user_id}).execute()
            created_student = True
        except Exception as exc:  # noqa: BLE001
            logger.error("provision: student insert failed for %s: %s", user_id, exc)
            raise

    # Track 10 (§139): a community partner's code, attributed once. Never fatal.
    if partner_code:
        try:
            from app.services.commerce_service import attribute
            attribute(user_id, partner_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("provision: partner attribution failed for %s: %s", user_id, exc)

    from app.services import enrolment_service

    # Enrolment plan H8. Non-fatal, and safe to lose: every purpose with no row
    # reads as NOT granted (app.consent_granted), so a failure here can only
    # under-record permission, never invent it. The account matters more.
    consents_recorded = 0
    if consents is not None:
        try:
            consents_recorded = enrolment_service.record_signup_consents(user_id, consents)
        except Exception as exc:  # noqa: BLE001
            logger.error("provision: consent record failed for %s: %s", user_id, exc)

    # A waitlist entry with this email is now an account (enrolment plan H2).
    # Only when the student row is new: later sign-ins have nothing to link.
    if created_student:
        enrolment_service.link_waitlist_conversion(user_id)

    referral = None
    if referral_code:
        # Deliberately last, and deliberately non-fatal. Attribution is
        # claim-on-first-signup and immutable, so a failure here costs a tutor
        # their share — but failing the whole provision would cost the student
        # their account, which is worse. It is logged loudly and reported back.
        from app.services import tutor_referral_service
        try:
            referral = await tutor_referral_service.attribute_signup(
                student_id=user_id,
                code=referral_code,
                source_platform=source_platform,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "provision: referral attribution failed for %s (code=%s): %s",
                user_id, referral_code, exc,
            )
            referral = {"attributed": False, "reason": "attribution_failed"}

    logger.info(
        "Provisioned %s — profile_created=%s student_created=%s referral=%s",
        user_id, created_profile, created_student,
        (referral or {}).get("reason") or (referral or {}).get("attributed"),
    )

    return {
        "user_id": user_id,
        "profile_created": created_profile,
        "student_created": created_student,
        "already_provisioned": not created_profile and not created_student,
        "user_type": _STUDENT,
        "referral": referral,
        "consents_recorded": consents_recorded,
    }
