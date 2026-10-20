"""Tutor applications (enrolment plan §2j, migration §120; CONFLICT-057; L-017).

J2 — the public half. A person applies on /teach:

  * The application is STORED (`app.tutor_application`). A second submission
    from an email with an open application updates it rather than piling up.
  * The applicant gets a RECEIPT with a withdraw link (a token; only its hash
    is kept). The team still gets the application by email, as under J0.
  * The answer is `{ok: true}` whatever happened to the email, so the form
    can't be used to learn who has applied. Only a failure to store is an
    error: the page must not say "received" when nothing was kept.

Retention (L-017): rejected and withdrawn applications get `delete_after` =
decision + 12 months, swept daily; approved ones stay with the tutor account.

The review half (J3) and tutor status (J4) live here too as they are built.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import email_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=365)
OPEN = ("submitted", "under_review")
# The /teach form's wording -> the stored band.
EXPERIENCE = {"0 — never taught": "none", "1–2 years": "1_2", "3–5 years": "3_5", "5+ years": "5_plus"}


class ApplicationRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t():
    return db.schema("app").table("tutor_application")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(fields: dict) -> dict:
    return {
        "full_name": fields["name"].strip()[:120],
        "location": (fields.get("location") or "").strip()[:120] or None,
        "languages": fields["languages"].strip()[:300],
        "experience_band": EXPERIENCE.get(fields.get("experience") or ""),
        "specialisations": list(fields.get("specialisations") or []),
        "demo_video_url": (fields.get("video") or "").strip() or None,
        "motivation": (fields.get("why") or "").strip()[:3000] or None,
        "source": (fields.get("source") or "").strip()[:120] or None,
    }


def submit(fields: dict) -> tuple[str, str]:
    """Store or refresh the open application. Returns (application id, withdraw
    token for the receipt). Raises only when nothing could be stored."""
    email = str(fields["email"]).strip().lower()
    token = secrets.token_urlsafe(32)
    row = {**_row(fields), "withdraw_token_hash": _hash(token)}
    existing = (_t().select("id").eq("email", email).in_("status", list(OPEN)).limit(1).execute()).data or []
    if existing:
        _t().update(row).eq("id", existing[0]["id"]).execute()
        return existing[0]["id"], token
    try:
        made = (_t().insert({"email": email, **row}).execute()).data or []
        return made[0]["id"], token
    except Exception as exc:  # noqa: BLE001
        # Lost a race with another submission from the same email: update that one.
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            again = (_t().select("id").eq("email", email).in_("status", list(OPEN)).limit(1).execute()).data or []
            if again:
                _t().update(row).eq("id", again[0]["id"]).execute()
                return again[0]["id"], token
        raise


async def notify(fields: dict, token: str) -> None:
    """The receipt to the applicant and the copy to the team. Best effort."""
    email = str(fields["email"]).strip().lower()
    name = fields["name"].strip()
    try:
        await email_service.send_template_email(email, "tutor_application_receipt", {
            "subject": "We've received your application to teach with Tauka",
            "name": name.split(" ")[0] or name,
            "withdraw_url": f"{settings.web_base_url}/teach/withdraw?token={token}",
        }, tags=["tutor_application", "receipt"])
    except Exception as exc:  # noqa: BLE001
        logger.error("tutor application receipt failed: %s", exc)
    if settings.admin_email:
        try:
            await email_service.send_template_email(settings.admin_email, "tutor_application", {
                "subject": f"Tutor application: {name[:80]}",
                "name": name, "email": email,
                "location": (fields.get("location") or "").strip(),
                "languages": fields["languages"].strip(),
                "experience": fields.get("experience"),
                "specialisations": fields.get("specialisations") or [],
                "video": (fields.get("video") or "").strip(),
                "why": (fields.get("why") or "").strip(),
            }, tags=["tutor_application"], reply_to=email)
        except Exception as exc:  # noqa: BLE001
            logger.error("tutor application team copy failed: %s", exc)


def withdraw(token: str) -> bool:
    """The receipt's link. True if an open application was withdrawn."""
    now = _now()
    got = (_t().update({"status": "withdrawn", "withdraw_token_hash": None,
                        "delete_after": (now + RETENTION).isoformat()})
           .eq("withdraw_token_hash", _hash(token)).in_("status", list(OPEN)).execute()).data or []
    return bool(got)


def sweep(now: datetime | None = None) -> int:
    """L-017: delete rejected and withdrawn applications 12 months after the decision."""
    now = now or _now()
    gone = (_t().delete().lt("delete_after", now.isoformat()).in_("status", ["rejected", "withdrawn"])
            .execute()).data or []
    return len(gone)

# ── J3: the admin review (plan §2j; CONFLICT-057) ───────────────────────────
#
# Ops (`ops` capability) lists, opens, marks under review, rejects with a code,
# or approves. APPROVAL MAKES AN ONBOARDING TUTOR, as the service role:
#   * no account with that email -> Supabase invites them (the link lands on
#     the web's /set-password), then the profile is created as a tutor;
#   * an existing learner account -> promoted in place (J1, decided), but only
#     once any live subscription has ended;
#   * user_profiles.user_type = 'tutor', and app.tutor with status 'onboarding'.
# Approval is idempotent. Nothing that meets learners or earns money runs for
# 'onboarding' (everything filters on 'active'); J4 activates.

REJECT_CODES = ("language_not_needed", "fluency", "demo", "capacity", "other")
LIVE_STATUSES = ("active", "trialing", "past_due")


def _require_ops(actor: str) -> None:
    from app.services import hierarchy_service as hs
    if not hs.admin_can(actor, "ops"):
        raise ApplicationRefused("not_allowed")


def _app(app_id: str) -> dict:
    rows = (_t().select("*").eq("id", app_id).limit(1).execute()).data or []
    if not rows:
        raise ApplicationRefused("no_such_application")
    return rows[0]


def _account(email: str) -> dict:
    """Whether the email has an account, what it is, and whether a subscription
    is live — what the admin sees before approving."""
    uid = db.schema("app").rpc("user_id_by_email", {"p_email": email}).execute().data
    if not uid:
        return {"exists": False}
    prof = (db.schema("app").table("user_profiles").select("user_type, first_name").eq("id", uid)
            .limit(1).execute()).data or [{}]
    st = (db.schema("app").table("student").select("tier, subscription_status").eq("id", uid)
          .limit(1).execute()).data or [{}]
    tier = st[0].get("tier") or "free_tier"
    live = tier != "free_tier" or (st[0].get("subscription_status") or "") in LIVE_STATUSES
    return {"exists": True, "user_id": uid, "user_type": prof[0].get("user_type"),
            "live_subscription": live}


def admin_list(actor: str, status: str | None = None) -> list[dict]:
    _require_ops(actor)
    q = _t().select("id, email, full_name, location, languages, experience_band, status, created_at, reviewed_at")
    if status:
        q = q.eq("status", status)
    return sorted((q.execute()).data or [], key=lambda r: r["created_at"], reverse=True)


def admin_detail(actor: str, app_id: str) -> dict:
    _require_ops(actor)
    a = _app(app_id)
    a.pop("withdraw_token_hash", None)
    return {**a, "account": _account(a["email"])}


def mark_under_review(actor: str, app_id: str) -> dict:
    _require_ops(actor)
    got = (_t().update({"status": "under_review", "reviewed_by": actor, "reviewed_at": _now().isoformat()})
           .eq("id", app_id).eq("status", "submitted").execute()).data or []
    if not got:
        raise ApplicationRefused("not_submitted")
    return got[0]


async def reject(actor: str, app_id: str, code: str, note: str | None = None) -> dict:
    _require_ops(actor)
    if code not in REJECT_CODES:
        raise ApplicationRefused("bad_reason")
    a = _app(app_id)
    now = _now()
    got = (_t().update({"status": "rejected", "reject_reason_code": code, "admin_note": (note or "").strip()[:2000] or None,
                        "reviewed_by": actor, "reviewed_at": now.isoformat(), "withdraw_token_hash": None,
                        "delete_after": (now + RETENTION).isoformat()})
           .eq("id", app_id).in_("status", list(OPEN)).execute()).data or []
    if not got:
        raise ApplicationRefused("decided")
    try:
        await email_service.send_template_email(a["email"], "tutor_application_decision", {
            "subject": "Your application to teach with Tauka", "kind": "rejected", "code": code,
            "name": a["full_name"].split(" ")[0], "web_base_url": settings.web_base_url,
        }, tags=["tutor_application", "rejected"])
    except Exception as exc:  # noqa: BLE001
        logger.error("rejection email failed: %s", exc)
    return got[0]


async def approve(actor: str, app_id: str, note: str | None = None) -> dict:
    _require_ops(actor)
    a = _app(app_id)
    if a["status"] == "approved":
        return a                                  # idempotent
    if a["status"] not in OPEN:
        raise ApplicationRefused("decided")
    acct = _account(a["email"])
    invited = False
    if acct["exists"]:
        uid = acct["user_id"]
        # Admins are app.admin_users rows (user_type is only tutor | student);
        # an admin account isn't turned into a tutor from an application.
        if (db.schema("app").table("admin_users").select("id").eq("id", uid).limit(1).execute()).data:
            raise ApplicationRefused("admin_account")
        if acct.get("user_type") != "tutor" and acct.get("live_subscription"):
            # J1: the same account, but not while they are paying as a learner.
            raise ApplicationRefused("live_subscription")
        # CONFLICT-057: service role; §94's guard lets the service role through.
        db.schema("app").table("user_profiles").update({"user_type": "tutor"}).eq("id", uid).execute()
    else:
        res = db.auth.admin.invite_user_by_email(a["email"], {
            "redirect_to": f"{settings.web_base_url}/set-password",
            "data": {"first_name": a["full_name"].split(" ")[0]},
        })
        uid = res.user.id
        invited = True
        db.schema("app").table("user_profiles").insert({
            "id": uid, "user_type": "tutor", "first_name": a["full_name"].split(" ")[0][:120],
        }).execute()
    if not (db.schema("app").table("tutor").select("id").eq("id", uid).limit(1).execute()).data:
        db.schema("app").table("tutor").insert({"id": uid, "status": "onboarding"}).execute()
        # §122: the status trail starts with the approval.
        from app.services.tutor_status_service import record
        record(uid, None, "onboarding", "approved", actor)
    got = (_t().update({"status": "approved", "tutor_id": uid, "reviewed_by": actor,
                        "reviewed_at": _now().isoformat(), "withdraw_token_hash": None,
                        "admin_note": (note or "").strip()[:2000] or a.get("admin_note")})
           .eq("id", app_id).in_("status", list(OPEN)).execute()).data or []
    try:
        await email_service.send_template_email(a["email"], "tutor_application_decision", {
            "subject": "Welcome to Tauka: your application is approved", "kind": "approved",
            "invited": invited, "name": a["full_name"].split(" ")[0], "web_base_url": settings.web_base_url,
        }, tags=["tutor_application", "approved"])
    except Exception as exc:  # noqa: BLE001
        logger.error("approval email failed: %s", exc)
    return got[0] if got else {**a, "status": "approved", "tutor_id": uid}
